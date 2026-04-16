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
from .models import AgentSession

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
        session = AgentSession(
            task_id=task_id,
            agent_type=agent_type,
            started_at=_now_iso(),
            scheduler_reason=scheduler_reason,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
        )
        db.add(session)
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
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error closing agent_session %s: %s", session_id, exc)
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
