"""
app/database/crud_autopilot.py
-------------------------------
CRUD for autopilot_objectives — the mission table that drives autonomous card creation.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .session import SessionLocal
from .models import AutopilotObjective, Task, Project, RevertVote, SelfModMergeLog

logger = logging.getLogger(__name__)

_TERMINAL_STAGES = {"completed", "failed"}

# Sentinel so list_objectives callers that omit parent_id get all objectives.
_UNSET = object()


def create_objective(
    project_id: int,
    description: str,
    *,
    priority: int = 5,
    time_box_hours: "int | None" = None,
    parent_id: "int | None" = None,
    created_by: str = "human",
) -> "AutopilotObjective | None":
    db = SessionLocal()
    try:
        expires_at = None
        if time_box_hours is not None:
            expires_at = datetime.now(timezone.utc) + timedelta(hours=time_box_hours)
        obj = AutopilotObjective(
            project_id=project_id,
            description=description,
            priority=priority,
            time_box_hours=time_box_hours,
            expires_at=expires_at,
            parent_id=parent_id,
            created_by=created_by,
        )
        db.add(obj)
        db.commit()
        db.refresh(obj)
        return obj
    except Exception as exc:
        db.rollback()
        logger.error("Error creating autopilot objective: %s", exc)
        return None
    finally:
        db.close()


def list_objectives(
    project_id: int,
    status: "str | None" = "active",
    parent_id: "int | None | object" = _UNSET,
) -> "list[AutopilotObjective]":
    db = SessionLocal()
    try:
        q = db.query(AutopilotObjective).filter(
            AutopilotObjective.project_id == project_id
        )
        if status is not None:
            q = q.filter(AutopilotObjective.status == status)
        if parent_id is not _UNSET:
            q = q.filter(AutopilotObjective.parent_id == parent_id)
        return q.order_by(AutopilotObjective.priority.desc(), AutopilotObjective.created_at.asc()).all()
    finally:
        db.close()


def get_objective(obj_id: int) -> "AutopilotObjective | None":
    db = SessionLocal()
    try:
        return db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
    finally:
        db.close()


def complete_objective(obj_id: int) -> None:
    """Mark an objective complete and cascade to parent when all siblings are done."""
    db = SessionLocal()
    cascade_to: "int | None" = None
    try:
        obj = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
        if not obj or obj.status == "complete":
            return
        parent_id = obj.parent_id  # capture before commit expires obj
        obj.status = "complete"
        obj.completed_at = datetime.now(timezone.utc)
        db.commit()
        # Cascade check in the same session — sees the just-released savepoint
        if parent_id is not None:
            remaining = db.query(AutopilotObjective).filter(
                AutopilotObjective.parent_id == parent_id,
                AutopilotObjective.status != "complete",
            ).count()
            if remaining == 0:
                cascade_to = parent_id
    except Exception as exc:
        db.rollback()
        logger.error("complete_objective %d: %s", obj_id, exc)
    finally:
        db.close()

    if cascade_to is not None:
        complete_objective(cascade_to)  # tail-recursive, fresh session


def get_objective_tree(project_id: int) -> "list[dict]":
    """Return all objectives as a nested list (children embedded under parents)."""
    all_objs = list_objectives(project_id, status=None)
    by_id: dict[int, dict] = {o.id: {**objective_to_dict(o), "children": []} for o in all_objs}
    roots: list[dict] = []
    for o in all_objs:
        if o.parent_id and o.parent_id in by_id:
            by_id[o.parent_id]["children"].append(by_id[o.id])
        else:
            roots.append(by_id[o.id])
    return roots


def append_objective_evidence(obj_id: int, entry: str) -> bool:
    """Append a timestamped note to the objective's evidence document in the doc store."""
    from app.database import store_document, get_document
    obj = get_objective(obj_id)
    if not obj:
        return False
    key = f"objective:{obj_id}:evidence"
    existing_doc = get_document(obj.project_id, key)
    existing = (existing_doc or {}).get("content", "")
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    new_content = (existing + f"\n\n## {timestamp}\n{entry.strip()}").strip()
    store_document(obj.project_id, key, new_content, tags=["evidence", f"objective:{obj_id}"])
    return True


def get_objective_evidence(obj_id: int) -> str:
    """Return the full evidence log for an objective, or a placeholder if none exists."""
    from app.database import get_document
    obj = get_objective(obj_id)
    if not obj:
        return "(objective not found)"
    key = f"objective:{obj_id}:evidence"
    doc = get_document(obj.project_id, key)
    return (doc or {}).get("content") or "(no evidence recorded yet)"


def update_objective(obj_id: int, **kwargs) -> "AutopilotObjective | None":
    db = SessionLocal()
    try:
        obj = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
        if not obj:
            return None
        for key, value in kwargs.items():
            setattr(obj, key, value)
        db.commit()
        db.refresh(obj)
        return obj
    except Exception as exc:
        db.rollback()
        logger.error("Error updating autopilot objective %d: %s", obj_id, exc)
        return None
    finally:
        db.close()


def update_objective_status(
    obj_id: int,
    status: str,
    completed_at: "datetime | None" = None,
) -> None:
    db = SessionLocal()
    try:
        obj = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
        if not obj:
            return
        obj.status = status
        if completed_at is not None:
            obj.completed_at = completed_at
        elif status == "complete" and obj.completed_at is None:
            obj.completed_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error updating objective status %d: %s", obj_id, exc)
    finally:
        db.close()


def record_assessment(
    obj_id: int,
    notes: str,
    tick: int,
    *,
    appears_complete: bool,
) -> None:
    db = SessionLocal()
    try:
        obj = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
        if not obj:
            return
        obj.last_assessment = notes
        obj.assessment_tick = tick
        if appears_complete:
            if obj.appears_complete_since is None:
                obj.appears_complete_since = datetime.now(timezone.utc)
        else:
            obj.appears_complete_since = None
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error recording assessment for objective %d: %s", obj_id, exc)
    finally:
        db.close()


def get_in_flight_count(project_id: int) -> int:
    """Count tasks tagged with an autopilot objective that are still active and not terminal."""
    db = SessionLocal()
    try:
        return (
            db.query(Task)
            .join(AutopilotObjective, Task.autopilot_objective_id == AutopilotObjective.id)
            .filter(
                AutopilotObjective.project_id == project_id,
                Task.is_active == True,
                Task.stage_key.notin_(_TERMINAL_STAGES),
            )
            .count()
        )
    finally:
        db.close()


def delete_objective(obj_id: int) -> bool:
    db = SessionLocal()
    try:
        obj = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj_id).first()
        if not obj:
            return False
        db.delete(obj)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error deleting autopilot objective %d: %s", obj_id, exc)
        return False
    finally:
        db.close()


def objective_to_dict(obj: AutopilotObjective) -> dict:
    return {
        "id": obj.id,
        "project_id": obj.project_id,
        "description": obj.description,
        "priority": obj.priority,
        "status": obj.status,
        "time_box_hours": obj.time_box_hours,
        "parent_id": obj.parent_id,
        "created_by": obj.created_by,
        "created_at": obj.created_at.isoformat() if obj.created_at else None,
        "expires_at": obj.expires_at.isoformat() if obj.expires_at else None,
        "completed_at": obj.completed_at.isoformat() if obj.completed_at else None,
        "last_assessment": obj.last_assessment,
        "assessment_tick": obj.assessment_tick,
        "appears_complete_since": obj.appears_complete_since.isoformat() if obj.appears_complete_since else None,
    }


# ---------------------------------------------------------------------------
# Revert votes (Gap 5 — self-modification)
# ---------------------------------------------------------------------------

def cast_revert_vote(task_id: str, merge_commit: str, reason: str) -> int:
    """Insert a vote and return the total vote count for this merge_commit."""
    db = SessionLocal()
    try:
        db.add(RevertVote(task_id=task_id, merge_commit=merge_commit, reason=reason))
        db.commit()
        count = db.query(RevertVote).filter(RevertVote.merge_commit == merge_commit).count()
        return count
    finally:
        db.close()


def get_revert_votes(merge_commit: str) -> list[dict]:
    db = SessionLocal()
    try:
        rows = (
            db.query(RevertVote)
            .filter(RevertVote.merge_commit == merge_commit)
            .order_by(RevertVote.created_at)
            .all()
        )
        return [
            {"task_id": r.task_id, "reason": r.reason, "created_at": r.created_at.isoformat()}
            for r in rows
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Self-mod merge log (Gap 5 — self-modification)
# ---------------------------------------------------------------------------

def record_self_mod_merge(task_id: str, merge_commit: str) -> None:
    db = SessionLocal()
    try:
        db.add(SelfModMergeLog(task_id=task_id, merge_commit=merge_commit))
        db.commit()
    finally:
        db.close()


def get_latest_self_mod_merge() -> "str | None":
    db = SessionLocal()
    try:
        row = (
            db.query(SelfModMergeLog)
            .filter(SelfModMergeLog.reverted.is_(False))
            .order_by(SelfModMergeLog.created_at.desc())
            .first()
        )
        return row.merge_commit if row else None
    finally:
        db.close()


def mark_self_mod_reverted(merge_commit: str) -> None:
    db = SessionLocal()
    try:
        db.query(SelfModMergeLog).filter(
            SelfModMergeLog.merge_commit == merge_commit
        ).update({"reverted": True, "reverted_at": datetime.now(timezone.utc)})
        db.commit()
    finally:
        db.close()
