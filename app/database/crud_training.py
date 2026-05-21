"""
CRUD operations for the training data pipeline (GAP 11).

Responsibilities:
  score_session        — qualify/score a single agent session for training export
  get_unscored_sessions — discover sessions not yet scored
  upsert_training_score — persist a score record
  get_qualified_unexported_sessions / count_qualified_unexported — drive export threshold
  mark_sessions_exported — bulk-mark after writing JSONL
  get_training_status  — summary dict for the status API
  create/list training_checkpoints — model deployment event log
  get_training_metrics — performance metrics segmented by checkpoint
"""

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, text

from .session import SessionLocal
from .models import (
    BudgetEntry,
    AgentSession,
    Task,
    Project,
    TrainingSessionScore,
    TrainingCheckpoint,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_session_start(session_id: str, db) -> datetime | None:
    """Earliest BudgetEntry.created_at for the given session_id."""
    result = (
        db.query(func.min(BudgetEntry.created_at))
        .filter(BudgetEntry.session_id == session_id)
        .scalar()
    )
    if result is None:
        return None
    if isinstance(result, datetime):
        return result.replace(tzinfo=timezone.utc) if result.tzinfo is None else result
    return None


def _is_failure_recovery(session_id: str, task: Task, db) -> bool:
    """True if this session ran after the task's most recent demotion and the task completed."""
    history = task.demotion_history or []
    if not history:
        return False
    try:
        last_demotion_ts = max(
            datetime.fromisoformat(d["timestamp"]).replace(tzinfo=timezone.utc)
            for d in history
            if d.get("timestamp")
        )
    except (KeyError, ValueError):
        return False
    session_start = _get_session_start(session_id, db)
    if session_start is None:
        return False
    if session_start.tzinfo is None:
        session_start = session_start.replace(tzinfo=timezone.utc)
    return session_start > last_demotion_ts


def _has_length_truncation(session_id: str, db) -> bool:
    """True if any LLM response in this session was cut off due to max_tokens."""
    entries = (
        db.query(BudgetEntry.response_data)
        .filter(
            BudgetEntry.session_id == session_id,
            BudgetEntry.response_data.isnot(None),
        )
        .all()
    )
    for (resp_data,) in entries:
        try:
            resp = json.loads(resp_data)
            finish = resp.get("choices", [{}])[0].get("finish_reason", "")
            if finish == "length":
                return True
        except (json.JSONDecodeError, IndexError, AttributeError):
            continue
    return False


def _has_accepted_submit(session_id: str, task_id: str, db) -> bool:
    """True if any AgentSession for this task has exit_summary indicating accepted work."""
    sessions = (
        db.query(AgentSession)
        .filter(AgentSession.task_id == task_id)
        .all()
    )
    for s in sessions:
        summary = (s.exit_summary or "").lower()
        if "submit_work" in summary and "accepted" in summary:
            return True
        if s.exit_reason and "accepted" in s.exit_reason.lower():
            return True
    return False


def _is_mechanical_session(session_id: str, db) -> bool:
    """True if all entries in this session are from mechanical agents (file summaries, etc)."""
    mechanical_prefixes = ("file summary", "filesummary", "file_summary")
    entries = (
        db.query(BudgetEntry.agent_name)
        .filter(
            BudgetEntry.session_id == session_id,
            BudgetEntry.agent_name.isnot(None),
        )
        .all()
    )
    if not entries:
        return False
    return all(
        any((name or "").lower().startswith(p) for p in mechanical_prefixes)
        for (name,) in entries
    )


# ---------------------------------------------------------------------------
# Session scoring
# ---------------------------------------------------------------------------

def score_session(session_id: str) -> dict | None:
    """
    Qualify and score a single agent session for training export.

    Returns a score record dict, or None if the session does not qualify.
    A session qualifies when:
      - Its task reached 'completed' stage
      - The project is not excluded from training
      - No LLM response was truncated (finish_reason != 'length')
      - It is not a purely mechanical session (file summaries etc.)
    """
    db = SessionLocal()
    try:
        # Find the task for this session
        entry = (
            db.query(BudgetEntry.task_id)
            .filter(BudgetEntry.session_id == session_id)
            .first()
        )
        if not entry or not entry.task_id:
            return None
        task_id = entry.task_id

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None

        # Task must be completed
        stage = task.stage_key or task.type or ""
        if stage != "completed":
            return None

        # Project must not be excluded
        if task.project_id:
            project = db.query(Project).filter(Project.id == task.project_id).first()
            if project and getattr(project, "exclude_from_training", False):
                return None

        # Exclude truncated sessions
        if _has_length_truncation(session_id, db):
            return None

        # Exclude purely mechanical sessions
        if _is_mechanical_session(session_id, db):
            return None

        tags: list[str] = []
        score = 1.0

        if _has_accepted_submit(session_id, task_id, db):
            tags.append("accepted")
            score += 0.5

        if _is_failure_recovery(session_id, task, db):
            tags.append("failure_recovery")
            score += 1.0

        # Proof-verified: math tasks with a passing verification record
        if (stage == "completed" and task.type in ("math", "completed")
                and task.stage_key == "completed"):
            # Check if there's any PipVerification or stage config indicating proof
            # Simple heuristic: if the task has 'proof' in title/description
            desc = (task.description or "") + (task.title or "")
            if "proof" in desc.lower() or "theorem" in desc.lower():
                tags.append("proof_verified")
                score += 0.5

        return {
            "session_id": session_id,
            "task_id": task_id,
            "score": score,
            "tags": tags,
            "qualified": True,
        }

    except Exception as exc:
        logger.error("Error scoring session %r: %s", session_id, exc)
        return None
    finally:
        db.close()


def upsert_training_score(result: dict, db=None) -> bool:
    """Insert or update a TrainingSessionScore row."""
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        existing = db.query(TrainingSessionScore).filter(
            TrainingSessionScore.session_id == result["session_id"]
        ).first()
        if existing:
            existing.score = result["score"]
            existing.tags = result["tags"]
            existing.qualified = result["qualified"]
            existing.scored_at = datetime.now(timezone.utc)
        else:
            row = TrainingSessionScore(
                session_id=result["session_id"],
                task_id=result["task_id"],
                score=result["score"],
                tags=result["tags"],
                qualified=result["qualified"],
                scored_at=datetime.now(timezone.utc),
            )
            db.add(row)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error upserting training score for %r: %s", result.get("session_id"), exc)
        return False
    finally:
        if _own_db:
            db.close()


def get_unscored_sessions(db=None, limit: int = 500) -> list[str]:
    """Return session_ids present in budget_entries but not yet in training_session_scores."""
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        # Subquery: already scored
        scored_ids = db.query(TrainingSessionScore.session_id).subquery()
        rows = (
            db.query(BudgetEntry.session_id)
            .filter(
                BudgetEntry.session_id.isnot(None),
                BudgetEntry.task_id.isnot(None),
                ~BudgetEntry.session_id.in_(scored_ids),
            )
            .distinct()
            .limit(limit)
            .all()
        )
        return [r.session_id for r in rows]
    except Exception as exc:
        logger.error("Error getting unscored sessions: %s", exc)
        return []
    finally:
        if _own_db:
            db.close()


def score_new_sessions() -> int:
    """Score all unscored sessions. Returns number of sessions newly qualified."""
    db = SessionLocal()
    try:
        unscored = get_unscored_sessions(db=db)
        qualified_count = 0
        for sid in unscored:
            result = score_session(sid)
            if result:
                upsert_training_score(result, db=db)
                if result["qualified"]:
                    qualified_count += 1
            else:
                # Record as not-qualified so we don't re-examine it
                entry = (
                    db.query(BudgetEntry.task_id)
                    .filter(BudgetEntry.session_id == sid)
                    .first()
                )
                if entry and entry.task_id:
                    upsert_training_score(
                        {"session_id": sid, "task_id": entry.task_id,
                         "score": 0.0, "tags": [], "qualified": False},
                        db=db,
                    )
        return qualified_count
    except Exception as exc:
        logger.error("Error in score_new_sessions: %s", exc)
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Export tracking
# ---------------------------------------------------------------------------

def count_qualified_unexported(db=None) -> int:
    """Count sessions that are qualified and not yet exported."""
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        return (
            db.query(TrainingSessionScore)
            .filter(
                TrainingSessionScore.qualified == True,
                TrainingSessionScore.exported_at.is_(None),
            )
            .count()
        )
    except Exception as exc:
        logger.error("Error counting qualified unexported: %s", exc)
        return 0
    finally:
        if _own_db:
            db.close()


def get_qualified_unexported_sessions(db=None, limit: int = 1000) -> list[TrainingSessionScore]:
    """Return qualified, not-yet-exported sessions ordered by score descending."""
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        return (
            db.query(TrainingSessionScore)
            .filter(
                TrainingSessionScore.qualified == True,
                TrainingSessionScore.exported_at.is_(None),
            )
            .order_by(TrainingSessionScore.score.desc())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.error("Error fetching qualified unexported sessions: %s", exc)
        return []
    finally:
        if _own_db:
            db.close()


def mark_sessions_exported(session_ids: list[str], db=None) -> bool:
    """Bulk-set exported_at = now() for the given session_ids."""
    if not session_ids:
        return True
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        db.query(TrainingSessionScore).filter(
            TrainingSessionScore.session_id.in_(session_ids)
        ).update({"exported_at": now}, synchronize_session=False)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error marking sessions exported: %s", exc)
        return False
    finally:
        if _own_db:
            db.close()


# ---------------------------------------------------------------------------
# Training status
# ---------------------------------------------------------------------------

def get_training_status(export_dir: str = "data/training_exports") -> dict:
    """Return a summary suitable for the GET /api/training/status response."""
    db = SessionLocal()
    try:
        qualified_unexported = count_qualified_unexported(db=db)

        # Find the most recent export from the training_session_scores table
        last_exported_row = (
            db.query(func.max(TrainingSessionScore.exported_at))
            .filter(TrainingSessionScore.exported_at.isnot(None))
            .scalar()
        )
        last_export_at = last_exported_row.isoformat() if last_exported_row else None

        # Scan disk for export files
        exports = []
        export_path = Path(export_dir)
        if export_path.exists():
            for f in sorted(export_path.glob("training_*.jsonl")):
                try:
                    size_mb = round(f.stat().st_size / (1024 * 1024), 2)
                    # Count lines = record count
                    with open(f, encoding="utf-8") as fh:
                        count = sum(1 for _ in fh)
                    exports.append({
                        "path": str(f),
                        "count": count,
                        "size_mb": size_mb,
                    })
                except OSError:
                    pass

        last_export_count = exports[-1]["count"] if exports else 0

        return {
            "qualified_unexported": qualified_unexported,
            "last_export_at": last_export_at,
            "last_export_count": last_export_count,
            "exports": exports,
        }
    except Exception as exc:
        logger.error("Error getting training status: %s", exc)
        return {"qualified_unexported": 0, "last_export_at": None,
                "last_export_count": 0, "exports": []}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Training checkpoints
# ---------------------------------------------------------------------------

def create_training_checkpoint(name: str, notes: str | None = None) -> TrainingCheckpoint | None:
    """Record a model deployment event."""
    db = SessionLocal()
    try:
        cp = TrainingCheckpoint(
            checkpoint_name=name,
            model_notes=notes,
            recorded_at=datetime.now(timezone.utc),
        )
        db.add(cp)
        db.commit()
        db.refresh(cp)
        return cp
    except Exception as exc:
        db.rollback()
        logger.error("Error creating training checkpoint: %s", exc)
        return None
    finally:
        db.close()


def list_training_checkpoints() -> list[TrainingCheckpoint]:
    """Return all checkpoints, newest first."""
    db = SessionLocal()
    try:
        return (
            db.query(TrainingCheckpoint)
            .order_by(TrainingCheckpoint.recorded_at.desc())
            .all()
        )
    except Exception as exc:
        logger.error("Error listing training checkpoints: %s", exc)
        return []
    finally:
        db.close()


def checkpoint_to_dict(cp: TrainingCheckpoint) -> dict:
    return {
        "id": cp.id,
        "checkpoint_name": cp.checkpoint_name,
        "model_notes": cp.model_notes,
        "recorded_at": cp.recorded_at.isoformat() if cp.recorded_at else None,
    }


# ---------------------------------------------------------------------------
# Performance metrics
# ---------------------------------------------------------------------------

def get_training_metrics(after_checkpoint_id: int | None = None) -> dict:
    """
    Compute key performance metrics from existing tables.
    If after_checkpoint_id is provided, metrics are filtered to tasks/sessions
    created after that checkpoint's recorded_at timestamp.
    """
    db = SessionLocal()
    try:
        after_ts = None
        if after_checkpoint_id:
            cp = db.query(TrainingCheckpoint).filter(
                TrainingCheckpoint.id == after_checkpoint_id
            ).first()
            if cp:
                after_ts = cp.recorded_at

        task_q = db.query(Task).filter(Task.is_active == True)
        if after_ts:
            task_q = task_q.filter(Task.created_at >= after_ts)
        all_tasks = task_q.all()

        total_tasks = len(all_tasks)
        if total_tasks == 0:
            return {
                "demotion_rate": 0.0,
                "completion_rate": 0.0,
                "avg_tokens_to_completion": 0,
                "length_finish_rate": 0.0,
            }

        completed_count = sum(
            1 for t in all_tasks
            if (t.stage_key or t.type or "") == "completed"
        )
        total_demotions = sum(t.demotion_count or 0 for t in all_tasks)

        # avg tokens to completion
        completed_task_ids = [
            t.id for t in all_tasks
            if (t.stage_key or t.type or "") == "completed"
        ]
        avg_tokens = 0
        if completed_task_ids:
            tokens_result = (
                db.query(
                    func.avg(
                        BudgetEntry.prompt_cost + BudgetEntry.generation_cost
                    )
                )
                .filter(BudgetEntry.task_id.in_(completed_task_ids))
                .scalar()
            )
            avg_tokens = int(tokens_result or 0)

        # length_finish_rate: fraction of scored sessions with length truncation
        scored_count = db.query(TrainingSessionScore).count()
        if scored_count > 0 and after_ts:
            length_sessions = (
                db.query(TrainingSessionScore)
                .join(Task, TrainingSessionScore.task_id == Task.id)
                .filter(Task.created_at >= after_ts)
                .filter(TrainingSessionScore.qualified == False)
                .count()
            )
        else:
            length_sessions = 0

        return {
            "demotion_rate": round(total_demotions / max(total_tasks, 1), 4),
            "completion_rate": round(completed_count / max(total_tasks, 1), 4),
            "avg_tokens_to_completion": avg_tokens,
            "length_finish_rate": round(length_sessions / max(scored_count, 1), 4),
        }
    except Exception as exc:
        logger.error("Error getting training metrics: %s", exc)
        return {
            "demotion_rate": 0.0,
            "completion_rate": 0.0,
            "avg_tokens_to_completion": 0,
            "length_finish_rate": 0.0,
        }
    finally:
        db.close()
