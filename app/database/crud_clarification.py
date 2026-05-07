"""
app/database/crud_clarification.py
------------------------------------
CRUD for the intake_drafts table (IDEA card clarification working drafts).
"""

import json
import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import IntakeDraft

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_intake_draft(task_id: str) -> IntakeDraft:
    """Create a blank intake draft row for the given task."""
    db = SessionLocal()
    try:
        now = _now_iso()
        draft = IntakeDraft(
            task_id=task_id,
            conversation_history=json.dumps([]),
            created_at=now,
            updated_at=now,
        )
        db.add(draft)
        db.commit()
        db.refresh(draft)
        return draft
    finally:
        db.close()


def get_intake_draft(task_id: str) -> IntakeDraft | None:
    db = SessionLocal()
    try:
        return db.query(IntakeDraft).filter(IntakeDraft.task_id == task_id).first()
    finally:
        db.close()


def update_intake_draft(task_id: str, **kwargs) -> IntakeDraft | None:
    """Update fields on the intake draft for the given task.

    JSON-serializable fields (acceptance_criteria, open_questions,
    suggested_prerequisites, suggested_subtasks, conversation_history) are
    accepted as either raw objects or already-serialized strings.
    """
    db = SessionLocal()
    try:
        draft = db.query(IntakeDraft).filter(IntakeDraft.task_id == task_id).first()
        if not draft:
            return None
        _json_fields = {
            "acceptance_criteria", "open_questions",
            "suggested_prerequisites", "suggested_subtasks",
            "conversation_history",
        }
        for field, value in kwargs.items():
            if field in _json_fields and not isinstance(value, str):
                value = json.dumps(value)
            setattr(draft, field, value)
        draft.updated_at = _now_iso()
        db.commit()
        db.refresh(draft)
        return draft
    finally:
        db.close()


def append_conversation_message(task_id: str, role: str, content: str) -> bool:
    """Append one message to the conversation_history JSON array."""
    db = SessionLocal()
    try:
        draft = db.query(IntakeDraft).filter(IntakeDraft.task_id == task_id).first()
        if not draft:
            return False
        history = json.loads(draft.conversation_history or "[]")
        history.append({"role": role, "content": content, "timestamp": _now_iso()})
        draft.conversation_history = json.dumps(history)
        draft.updated_at = _now_iso()
        db.commit()
        return True
    finally:
        db.close()


def intake_draft_to_dict(draft: IntakeDraft) -> dict:
    """Serialize an IntakeDraft for the API response."""
    def _parse(field):
        if field is None:
            return None
        if isinstance(field, str):
            try:
                return json.loads(field)
            except (json.JSONDecodeError, ValueError):
                return field
        return field

    return {
        "id": draft.id,
        "task_id": draft.task_id,
        "rewritten_description": draft.rewritten_description,
        "design_rationale": draft.design_rationale,
        "acceptance_criteria": _parse(draft.acceptance_criteria),
        "out_of_scope": draft.out_of_scope,
        "open_questions": _parse(draft.open_questions),
        "suggested_prerequisites": _parse(draft.suggested_prerequisites),
        "suggested_subtasks": _parse(draft.suggested_subtasks),
        "conversation_history": _parse(draft.conversation_history),
        "agent_token_cost": draft.agent_token_cost,
        "created_at": draft.created_at,
        "updated_at": draft.updated_at,
    }
