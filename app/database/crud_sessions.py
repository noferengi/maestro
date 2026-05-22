"""
app/database/crud_sessions.py
------------------------------
CRUD for the agent_sessions table.

One row per agent invocation: opened at dispatch time, closed when the
agent exits with its exit_reason and optional summary text.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import AgentSession, Task, ToolBugReport

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_agent_session(
    task_id: str,
    agent_type: str,
    llm_id: int | None = None,
    budget_id: int | None = None,
    scheduler_reason: str = "scheduler",
    max_turns: int | None = None,
) -> int | None:
    """Insert an open agent session row.

    Returns the new session id, or None on error.
    """
    db = SessionLocal()
    try:
        now = _now_iso()
        session = AgentSession(
            task_id=task_id,
            agent_type=agent_type,
            started_at=now,
            last_activity_at=now,
            scheduler_reason=scheduler_reason,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
        )
        db.add(session)
        db.query(Task).filter(Task.id == task_id).update(
            {"last_progress_at": datetime.now(timezone.utc).replace(tzinfo=None)},
            synchronize_session=False,
        )
        db.commit()
        db.refresh(session)
        return session.id
    except Exception as exc:
        db.rollback()
        logger.error("Error creating agent_session (task=%s type=%s): %s", task_id, agent_type, exc)
        return None
    finally:
        db.close()


def close_agent_session(
    session_id: int | None,
    exit_reason: str,
    exit_summary: str = "",
    turn_count: int | None = None,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Fill ended_at and outcome fields on an existing agent session row.

    Safe to call with session_id=None (no-op) so callers don't need to
    guard against create_agent_session failures.
    """
    if session_id is None:
        return
    db = SessionLocal()
    try:
        row = db.query(AgentSession).filter(AgentSession.id == session_id).first()
        if not row:
            return
        row.ended_at = _now_iso()
        row.exit_reason = exit_reason
        row.exit_summary = (exit_summary or "")[:4000]   # cap to avoid huge blobs
        if turn_count is not None:
            row.turn_count = turn_count
        row.prompt_tokens = prompt_tokens or 0
        row.completion_tokens = completion_tokens or 0
        if row.task_id:
            db.query(Task).filter(Task.id == row.task_id).update(
                {"last_progress_at": datetime.now(timezone.utc).replace(tzinfo=None)},
                synchronize_session=False,
            )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error closing agent_session %s: %s", session_id, exc)
    finally:
        db.close()


def close_zombie_sessions() -> int:
    """Mark all open agent_sessions as closed on server startup.

    Returns count of rows updated.
    """
    db = SessionLocal()
    try:
        result = db.execute(
            __import__("sqlalchemy").text(
                "UPDATE agent_sessions SET ended_at=:now, exit_reason='shutdown', "
                "exit_summary='Closed on server startup (zombie session)' "
                "WHERE ended_at IS NULL"
            ),
            {"now": _now_iso()},
        )
        db.commit()
        return result.rowcount
    except Exception as exc:
        db.rollback()
        logger.error("Error closing zombie sessions: %s", exc)
        return 0
    finally:
        db.close()


def close_zombie_sessions_for_tasks(exclude_task_ids: set[str]) -> list[str]:
    """Close open sessions for tasks whose threads are no longer alive.

    Called periodically by _cleanup_finished() in scheduler.py to reconcile
    DB state with in-memory thread state after threads die unexpectedly.
    exclude_task_ids: task IDs that are known-alive; all others are closed.
    
    Returns the list of task_ids that were closed.
    """
    import sqlalchemy as _sa
    db = SessionLocal()
    try:
        # 1. Identify all open task_ids in the DB
        open_rows = db.execute(
            _sa.text(
                "SELECT DISTINCT task_id FROM agent_sessions "
                "WHERE ended_at IS NULL AND task_id IS NOT NULL"
            )
        ).fetchall()
        db_open_ids = {r[0] for r in open_rows}
        logger.info("[sessions] Reconciling DB: %d open task sessions in DB, %d alive threads.",
                    len(db_open_ids), len(exclude_task_ids))

        # 2. Find IDs that are in DB but NOT in the exclusion set
        zombie_ids = [tid for tid in db_open_ids if tid not in exclude_task_ids]
        if not zombie_ids:
            return []

        logger.info("[sessions] Found %d zombie task(s) to close: %s", len(zombie_ids), zombie_ids)

        # 3. Batch close them
        # Note: task_id IN (...) is safe here as IDs are generated slugs/uuids
        placeholders = ",".join(f"'{t}'" for t in zombie_ids)
        db.execute(
            _sa.text(
                f"UPDATE agent_sessions SET ended_at=:now, exit_reason='shutdown', "
                f"exit_summary='Closed by scheduler cleanup: thread no longer alive' "
                f"WHERE ended_at IS NULL AND task_id IN ({placeholders})"
            ),
            {"now": _now_iso()},
        )
        db.commit()
        logger.info("[sessions] Successfully closed zombie DB session(s) for %d tasks.", len(zombie_ids))
        return zombie_ids

    except Exception as exc:
        db.rollback()
        logger.error("Error in close_zombie_sessions_for_tasks: %s", exc)
        return []
    finally:
        db.close()


def close_zombie_sessions_by_session_id(exclude_ids: set[int]) -> list[str]:
    """Close all open sessions whose PK is not in exclude_ids.

    Unlike close_zombie_sessions_for_tasks() (which excludes entire tasks),
    this function excludes individual session rows by PK. A task that retried
    planning 50 times will have 50 stale open sessions and 1 current one; only
    the current session's ID is in exclude_ids, so all 50 stale ones get closed.

    Returns the distinct task_ids that had sessions closed.
    """
    import sqlalchemy as _sa
    # Normalise to ints; filter out any None values from failed creates.
    safe_exclude = {int(i) for i in exclude_ids if i is not None}

    db = SessionLocal()
    try:
        open_rows = db.execute(
            _sa.text(
                "SELECT id, task_id FROM agent_sessions WHERE ended_at IS NULL"
            )
        ).fetchall()

        to_close = [r for r in open_rows if r[0] not in safe_exclude]
        if not to_close:
            return []

        close_ids = [r[0] for r in to_close]
        task_ids = list({r[1] for r in to_close if r[1] is not None})

        logger.info(
            "[sessions] Found %d zombie session(s) to close by session ID (tasks: %s)",
            len(close_ids), task_ids,
        )

        placeholders = ",".join(str(i) for i in close_ids)
        db.execute(
            _sa.text(
                f"UPDATE agent_sessions SET ended_at=:now, exit_reason='shutdown', "
                f"exit_summary='Closed by scheduler cleanup: session no longer active' "
                f"WHERE id IN ({placeholders})"
            ),
            {"now": _now_iso()},
        )
        db.commit()
        logger.info(
            "[sessions] Successfully closed %d zombie session(s) for %d task(s).",
            len(close_ids), len(task_ids),
        )
        return task_ids

    except Exception as exc:
        db.rollback()
        logger.error("Error in close_zombie_sessions_by_session_id: %s", exc)
        return []
    finally:
        db.close()


def get_agent_sessions_for_task(task_id: str) -> list[AgentSession]:
    """Return all sessions for a task, oldest first."""
    db = SessionLocal()
    try:
        return (
            db.query(AgentSession)
            .filter(AgentSession.task_id == task_id)
            .order_by(AgentSession.started_at.asc())
            .all()
        )
    except Exception as exc:
        logger.error("Error fetching agent_sessions for task %s: %s", task_id, exc)
        return []
    finally:
        db.close()


def get_open_sessions() -> list[AgentSession]:
    """Return all active (open) agent sessions."""
    db = SessionLocal()
    try:
        return (
            db.query(AgentSession)
            .filter(AgentSession.ended_at.is_(None))
            .all()
        )
    except Exception as exc:
        logger.error("Error fetching open agent_sessions: %s", exc)
        return []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Tool bug reports
# ---------------------------------------------------------------------------

def create_tool_bug_report(
    task_id: str,
    tool_name: str,
    trying_to: str,
    expected: str,
    actual: str,
    session_id: int | None = None,
) -> int | None:
    """Insert an agent-filed tool bug report. Returns the new row id or None on error."""
    db = SessionLocal()
    try:
        row = ToolBugReport(
            task_id=task_id,
            session_id=session_id,
            tool_name=tool_name,
            trying_to=trying_to[:4000],
            expected=expected[:2000],
            actual=actual[:2000],
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row.id
    except Exception as exc:
        db.rollback()
        logger.error("Error creating tool_bug_report (task=%s tool=%s): %s", task_id, tool_name, exc)
        return None
    finally:
        db.close()


def get_tool_bug_reports(
    task_id: str | None = None,
    tool_name: str | None = None,
    unread_only: bool = False,
    limit: int = 50,
) -> list[ToolBugReport]:
    """Fetch tool bug reports, optionally filtered by task, tool name, or unread status."""
    db = SessionLocal()
    try:
        q = db.query(ToolBugReport)
        if task_id:
            q = q.filter(ToolBugReport.task_id == task_id)
        if tool_name:
            q = q.filter(ToolBugReport.tool_name == tool_name)
        if unread_only:
            q = q.filter(ToolBugReport.viewed_at.is_(None))
        return q.order_by(ToolBugReport.created_at.desc()).limit(limit).all()
    except Exception as exc:
        logger.error("Error fetching tool_bug_reports: %s", exc)
        return []
    finally:
        db.close()


def mark_tool_bug_reports_viewed(report_ids: list[int] | None = None) -> int:
    """Mark bug reports as viewed. Pass None to mark all unread reports viewed.

    Returns count of rows updated.
    """
    db = SessionLocal()
    try:
        import sqlalchemy as _sa
        now = datetime.now(timezone.utc).isoformat()
        q = db.query(ToolBugReport).filter(ToolBugReport.viewed_at.is_(None))
        if report_ids is not None:
            q = q.filter(ToolBugReport.id.in_(report_ids))
        count = q.update({"viewed_at": now}, synchronize_session=False)
        db.commit()
        return count
    except Exception as exc:
        db.rollback()
        logger.error("Error marking tool_bug_reports viewed: %s", exc)
        return 0
    finally:
        db.close()
