"""
app/database/crud_documents.py
-------------------------------
CRUD for project_documents — the per-project shared knowledge store.

All keys are normalised to lowercase on write and lookup so that retrieval
is always case-insensitive without requiring ILIKE or function-based indexes.
Fuzzy search uses PostgreSQL pg_trgm similarity() via a raw SQL query.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text

from .session import SessionLocal
from .models import ProjectDocument, Project

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _doc_to_dict(doc: ProjectDocument) -> dict:
    return {
        "id": doc.id,
        "project_id": doc.project_id,
        "key": doc.key,
        "content": doc.content,
        "tags": doc.tags,
        "written_by_task_id": doc.written_by_task_id,
        "created_at": doc.created_at.isoformat() if doc.created_at else None,
        "updated_at": doc.updated_at.isoformat() if doc.updated_at else None,
        "deleted_at": doc.deleted_at.isoformat() if doc.deleted_at else None,
    }


def _doc_to_meta(doc: ProjectDocument) -> dict:
    """Return metadata only — no content field."""
    d = _doc_to_dict(doc)
    size = len((doc.content or "").encode("utf-8"))
    d["content_size_bytes"] = size
    del d["content"]
    return d


# ---------------------------------------------------------------------------
# Project ID helpers
# ---------------------------------------------------------------------------

def _get_project_id(db, project_name: str) -> int | None:
    row = db.query(Project).filter(Project.name == project_name).first()
    return row.id if row else None


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

def store_document(
    project_id: int,
    key: str,
    content: str,
    tags: list[str] | None = None,
    written_by_task_id: str | None = None,
) -> dict:
    """
    Upsert a document. Key is normalised to lowercase.
    Returns the created/updated row as a dict.
    """
    key = key.lower().strip()
    now = datetime.now(timezone.utc)

    with SessionLocal() as db:
        existing = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.key == key,
            )
            .first()
        )
        if existing:
            existing.content = content
            existing.tags = tags
            if written_by_task_id is not None:
                existing.written_by_task_id = written_by_task_id
            existing.updated_at = now
            existing.deleted_at = None  # un-delete if previously soft-deleted
            db.commit()
            db.refresh(existing)
            return _doc_to_dict(existing)
        else:
            doc = ProjectDocument(
                project_id=project_id,
                key=key,
                content=content,
                tags=tags,
                written_by_task_id=written_by_task_id,
                created_at=now,
                updated_at=now,
            )
            db.add(doc)
            db.commit()
            db.refresh(doc)
            return _doc_to_dict(doc)


# ---------------------------------------------------------------------------
# Read — exact key
# ---------------------------------------------------------------------------

def get_document(project_id: int, key: str) -> dict | None:
    """Exact key lookup. Returns None if not found or soft-deleted."""
    key = key.lower().strip()
    with SessionLocal() as db:
        doc = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.key == key,
                ProjectDocument.deleted_at.is_(None),
            )
            .first()
        )
        return _doc_to_dict(doc) if doc else None


# ---------------------------------------------------------------------------
# Read — fuzzy key (pg_trgm)
# ---------------------------------------------------------------------------

def fuzzy_get_document(
    project_id: int,
    key: str,
    threshold: float = 0.3,
) -> list[dict]:
    """
    Return documents whose key is similar to `key` via pg_trgm similarity().
    Results sorted by similarity descending, up to 10.
    Each result includes a 'similarity' float field (0.0–1.0).
    """
    key = key.lower().strip()
    with SessionLocal() as db:
        rows = db.execute(
            text("""
                SELECT id, project_id, key, content, tags,
                       written_by_task_id, created_at, updated_at, deleted_at,
                       similarity(key, :q) AS sim
                FROM project_documents
                WHERE project_id = :pid
                  AND deleted_at IS NULL
                  AND similarity(key, :q) >= :thresh
                ORDER BY sim DESC
                LIMIT 10
            """),
            {"pid": project_id, "q": key, "thresh": threshold},
        ).fetchall()

    results = []
    for row in rows:
        m = dict(row._mapping)
        sim = m.pop("sim")
        for field in ("created_at", "updated_at", "deleted_at"):
            if m[field] is not None and hasattr(m[field], "isoformat"):
                m[field] = m[field].isoformat()
        m["similarity"] = round(float(sim), 4)
        results.append(m)
    return results


# ---------------------------------------------------------------------------
# List — metadata only
# ---------------------------------------------------------------------------

def list_documents(
    project_id: int,
    tag: str | None = None,
) -> list[dict]:
    """
    List all non-deleted documents in a project (metadata only, no content).
    Optional tag filter: returns only docs whose tags JSON array contains `tag`.
    Sorted by key ascending.
    """
    with SessionLocal() as db:
        q = db.query(ProjectDocument).filter(
            ProjectDocument.project_id == project_id,
            ProjectDocument.deleted_at.is_(None),
        )
        if tag is not None:
            # JSON array containment: tags @> '["tag"]'::jsonb
            q = q.filter(
                text("tags @> cast(:tag_json as jsonb)").bindparams(
                    tag_json=json.dumps([tag])
                )
            )
        docs = q.order_by(ProjectDocument.key).all()
        return [_doc_to_meta(d) for d in docs]


# ---------------------------------------------------------------------------
# Delete — soft
# ---------------------------------------------------------------------------

def delete_document(
    project_id: int,
    key: str,
    deleted_by_task_id: str | None = None,
) -> bool:
    """
    Soft-delete by setting deleted_at. Returns True if the document existed.
    The deleted_by_task_id is logged but not stored (no column for it).
    """
    key = key.lower().strip()
    with SessionLocal() as db:
        doc = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.project_id == project_id,
                ProjectDocument.key == key,
                ProjectDocument.deleted_at.is_(None),
            )
            .first()
        )
        if doc is None:
            return False
        doc.deleted_at = datetime.now(timezone.utc)
        db.commit()
        if deleted_by_task_id:
            logger.info(
                "document deleted: project=%d key=%r by task=%s",
                project_id, key, deleted_by_task_id,
            )
        return True


# ---------------------------------------------------------------------------
# Convenience: project-name–scoped wrappers
# ---------------------------------------------------------------------------

def store_document_by_project(
    project_name: str,
    key: str,
    content: str,
    tags: list[str] | None = None,
    written_by_task_id: str | None = None,
) -> dict | None:
    with SessionLocal() as db:
        pid = _get_project_id(db, project_name)
    if pid is None:
        return None
    return store_document(pid, key, content, tags, written_by_task_id)


def get_document_by_project(project_name: str, key: str) -> dict | None:
    with SessionLocal() as db:
        pid = _get_project_id(db, project_name)
    if pid is None:
        return None
    return get_document(pid, key)


def fuzzy_get_document_by_project(
    project_name: str,
    key: str,
    threshold: float = 0.3,
) -> list[dict]:
    with SessionLocal() as db:
        pid = _get_project_id(db, project_name)
    if pid is None:
        return []
    return fuzzy_get_document(pid, key, threshold)


def list_documents_by_project(
    project_name: str,
    tag: str | None = None,
) -> list[dict]:
    with SessionLocal() as db:
        pid = _get_project_id(db, project_name)
    if pid is None:
        return []
    return list_documents(pid, tag)


def delete_document_by_project(
    project_name: str,
    key: str,
    deleted_by_task_id: str | None = None,
) -> bool:
    with SessionLocal() as db:
        pid = _get_project_id(db, project_name)
    if pid is None:
        return False
    return delete_document(pid, key, deleted_by_task_id)


def list_documents_written_by_task(task_id: str) -> list[dict]:
    """Return all non-deleted documents written by a specific task (metadata only)."""
    with SessionLocal() as db:
        docs = (
            db.query(ProjectDocument)
            .filter(
                ProjectDocument.written_by_task_id == task_id,
                ProjectDocument.deleted_at.is_(None),
            )
            .order_by(ProjectDocument.key)
            .all()
        )
        return [_doc_to_meta(d) for d in docs]
