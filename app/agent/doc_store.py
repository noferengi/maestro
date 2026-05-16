"""
app/agent/doc_store.py
----------------------
Agent-facing Python API for the project document store.

These functions are thin wrappers over crud_documents that resolve the
project_id from the current agent context (project name → project record).
They are the only entry point agents should use — direct CRUD calls bypass
key normalisation and context injection.
"""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

logger = logging.getLogger(__name__)


def _resolve_project_id(project_name: str | None) -> int | None:
    if not project_name:
        return None
    from app.database.session import SessionLocal
    from app.database.models import Project
    with SessionLocal() as db:
        row = db.query(Project).filter(Project.name == project_name).first()
        return row.id if row else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def store_document(
    project_name: str,
    key: str,
    content: str,
    tags: list[str] | None = None,
    written_by_task_id: str | None = None,
) -> dict | None:
    """
    Write content under key in the project store.
    Creates a new row or updates in place (last-write-wins).
    Returns the stored document as a dict, or None if project not found.
    """
    pid = _resolve_project_id(project_name)
    if pid is None:
        logger.warning("doc_store.store_document: project %r not found", project_name)
        return None
    from app.database.crud_documents import store_document as _store
    return _store(pid, key, content, tags, written_by_task_id)


def get_document(project_name: str, key: str) -> dict | None:
    """
    Exact key lookup. Returns the document dict or None.
    """
    pid = _resolve_project_id(project_name)
    if pid is None:
        return None
    from app.database.crud_documents import get_document as _get
    return _get(pid, key)


def fuzzy_get_document(
    project_name: str,
    key: str,
    threshold: float = 0.3,
) -> list[dict]:
    """
    Fuzzy key lookup via pg_trgm similarity. Returns up to 10 results
    sorted by similarity descending. Each result has a 'similarity' field.
    """
    pid = _resolve_project_id(project_name)
    if pid is None:
        return []
    from app.database.crud_documents import fuzzy_get_document as _fuzzy
    return _fuzzy(pid, key, threshold)


def list_documents(
    project_name: str,
    tag: str | None = None,
) -> list[dict]:
    """
    List all document metadata (no content) for the project.
    Optionally filter by tag.
    """
    pid = _resolve_project_id(project_name)
    if pid is None:
        return []
    from app.database.crud_documents import list_documents as _list
    return _list(pid, tag)


def delete_document(
    project_name: str,
    key: str,
    deleted_by_task_id: str | None = None,
) -> bool:
    """Soft-delete a document by key. Returns True if it existed."""
    pid = _resolve_project_id(project_name)
    if pid is None:
        return False
    from app.database.crud_documents import delete_document as _del
    return _del(pid, key, deleted_by_task_id)
