"""
app/database/crud_inbox.py
--------------------------
CRUD functions for inbox_messages — persistent user notifications.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from .session import SessionLocal
from .models import InboxMessage


def _row_to_dict(msg: InboxMessage) -> dict:
    return {
        "id": msg.id,
        "subject": msg.subject,
        "source_type": msg.source_type,
        "task_id": msg.task_id,
        "project_id": msg.project_id,
        "task_title": msg.task_title,
        "outcome": msg.outcome,
        "data_json": msg.data_json,
        "read": bool(msg.read),
        "created_at": msg.created_at if isinstance(msg.created_at, str) else (msg.created_at.isoformat() if msg.created_at else None),
    }


def create_inbox_message(
    subject: str,
    source_type: str = "intake_result",
    task_id: str | None = None,
    project_id: str | None = None,
    task_title: str | None = None,
    outcome: str | None = None,
    data_json: str | None = None,
) -> dict:
    """Create a new inbox message and return it as a dict."""
    with SessionLocal() as db:
        msg = InboxMessage(
            id=str(uuid.uuid4()),
            subject=subject,
            source_type=source_type,
            task_id=task_id,
            project_id=project_id,
            task_title=task_title,
            outcome=outcome,
            data_json=data_json,
            read=False,
            created_at=datetime.now(timezone.utc),
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return _row_to_dict(msg)


def get_inbox_messages(
    unread_only: bool = False,
    source_type: str | None = None,
    project_name: str | None = None,
) -> list[dict]:
    """Return all inbox messages, newest first. Optionally filter to unread, by source_type, or by project."""
    with SessionLocal() as db:
        q = db.query(InboxMessage)
        if unread_only:
            q = q.filter(InboxMessage.read == False)  # noqa: E712
        if source_type is not None:
            q = q.filter(InboxMessage.source_type == source_type)
        if project_name is not None:
            q = q.filter(InboxMessage.project_id == project_name)
        msgs = q.order_by(InboxMessage.created_at.desc()).all()
        return [_row_to_dict(m) for m in msgs]


def get_inbox_message(msg_id: str) -> dict | None:
    with SessionLocal() as db:
        msg = db.query(InboxMessage).filter(InboxMessage.id == msg_id).first()
        return _row_to_dict(msg) if msg else None


def mark_inbox_read(msg_id: str, read: bool = True) -> dict | None:
    with SessionLocal() as db:
        msg = db.query(InboxMessage).filter(InboxMessage.id == msg_id).first()
        if msg is None:
            return None
        msg.read = read
        db.commit()
        db.refresh(msg)
        return _row_to_dict(msg)


def mark_all_inbox_read() -> int:
    """Mark all unread messages as read. Returns count updated."""
    with SessionLocal() as db:
        n = (
            db.query(InboxMessage)
            .filter(InboxMessage.read == False)  # noqa: E712
            .update({"read": True})
        )
        db.commit()
        return n


def delete_inbox_message(msg_id: str) -> bool:
    with SessionLocal() as db:
        msg = db.query(InboxMessage).filter(InboxMessage.id == msg_id).first()
        if msg is None:
            return False
        db.delete(msg)
        db.commit()
        return True


def count_unread_inbox(project_name: str | None = None) -> dict:
    with SessionLocal() as db:
        q = db.query(InboxMessage).filter(InboxMessage.read == False)  # noqa: E712
        if project_name is not None:
            q = q.filter(InboxMessage.project_id == project_name)
        
        count = q.count()
        has_needs_human = q.filter(InboxMessage.source_type == "needs_human").count() > 0
        
        return {
            "count": count,
            "has_needs_human": has_needs_human
        }
