"""
CRUD operations for file and search cache tables.

FileSummary   — DB-cached natural-language LLM summaries of source files,
                keyed by (sha1_hash, file_size_bytes).  Path is informational.
                create_file_summary uses INSERT-then-catch semantics so
                concurrent agents summarising the same file don't crash.

SearchCache   — cached web search results, keyed by (query, provider).
                Prevents redundant Brave/DuckDuckGo API calls.

ArchivedFile  — registry of files moved to .archive/ by workspace.delete_file().
                Paths are stored relative to the project root for portability.
"""

import logging
import os
from datetime import datetime

from .session import SessionLocal
from .models import FileSummary, SearchCache, ArchivedFile

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# FileSummary CRUD
# ---------------------------------------------------------------------------

def get_file_summary(sha1: str, filesize: int) -> "FileSummary | None":
    """Return a cached FileSummary row for the given sha1+filesize, or None."""
    db = SessionLocal()
    try:
        return (
            db.query(FileSummary)
            .filter(FileSummary.sha1_hash == sha1, FileSummary.file_size_bytes == filesize)
            .first()
        )
    finally:
        db.close()


def create_file_summary(
    sha1: str,
    filesize: int,
    path: str,
    summary: str,
    static_analysis_json: "str | None" = None,
    *,
    short_summary: "str | None" = None,
) -> "FileSummary":
    """Insert a new FileSummary row.  Uses INSERT OR IGNORE semantics via
    try/except so concurrent agents summarising the same file don't crash.
    Returns the (possibly pre-existing) row.
    """
    db = SessionLocal()
    try:
        row = FileSummary(
            sha1_hash=sha1,
            file_size_bytes=filesize,
            file_path=path,
            summary=summary,
            short_summary=short_summary,
            static_analysis_json=static_analysis_json,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        # Race condition: another agent inserted the same sha1+filesize first.
        # Return the existing row.
        existing = (
            db.query(FileSummary)
            .filter(FileSummary.sha1_hash == sha1, FileSummary.file_size_bytes == filesize)
            .first()
        )
        return existing
    finally:
        db.close()


def get_file_summary_by_path(abs_path: str) -> "FileSummary | None":
    """Return the most recent cached summary for an absolute file path, or None."""
    abs_path = os.path.normpath(os.path.abspath(abs_path))
    db = SessionLocal()
    try:
        return (
            db.query(FileSummary)
            .filter(FileSummary.file_path == abs_path)
            .order_by(FileSummary.created_at.desc())
            .first()
        )
    finally:
        db.close()


def get_file_summaries_for_project_root(project_root: str) -> "list[FileSummary]":
    """Return all cached summaries whose file_path is under project_root, ordered by path."""
    root = os.path.normpath(os.path.abspath(project_root))
    # Normalize to lowercase forward-slashes so PostgreSQL LIKE works correctly:
    # backslashes are escape chars in PG LIKE, and PG LIKE is case-sensitive.
    prefix = root.replace("\\", "/").lower() + "/"
    db = SessionLocal()
    try:
        from sqlalchemy import func
        normalized = func.lower(func.replace(FileSummary.file_path, "\\", "/"))
        return (
            db.query(FileSummary)
            .filter(normalized.like(prefix + "%"))
            .order_by(FileSummary.file_path)
            .all()
        )
    finally:
        db.close()


def delete_file_summary(sha1: str, filesize: int) -> int:
    """Delete a FileSummary row by sha1+filesize.

    Returns the number of rows deleted (0 or 1).
    """
    db = SessionLocal()
    try:
        result = (
            db.query(FileSummary)
            .filter(FileSummary.sha1_hash == sha1, FileSummary.file_size_bytes == filesize)
            .delete(synchronize_session=False)
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SearchCache CRUD
# ---------------------------------------------------------------------------

def get_search_cache(query: str, provider: str = 'brave') -> "SearchCache | None":
    """Return a cached search result for the exact query and provider, or None."""
    db = SessionLocal()
    try:
        q = query.strip()
        return (
            db.query(SearchCache)
            .filter(SearchCache.query == q, SearchCache.provider == provider)
            .first()
        )
    finally:
        db.close()


def create_search_cache(query: str, result_json: str, provider: str = 'brave') -> "SearchCache":
    """Insert a new search cache entry. Returns the created (or existing) row."""
    db = SessionLocal()
    try:
        q = query.strip()
        row = SearchCache(
            query=q,
            result_json=result_json,
            provider=provider
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        # Race/Duplicate: return existing
        return db.query(SearchCache).filter(SearchCache.query == q, SearchCache.provider == provider).first()
    finally:
        db.close()


def delete_search_cache(query: str, provider: str = 'brave') -> int:
    """Delete a SearchCache row by query+provider.

    Returns the number of rows deleted (0 or 1).
    """
    db = SessionLocal()
    try:
        q = query.strip()
        result = (
            db.query(SearchCache)
            .filter(SearchCache.query == q, SearchCache.provider == provider)
            .delete(synchronize_session=False)
        )
        db.commit()
        return result
    except Exception:
        db.rollback()
        return 0
    finally:
        db.close()


def get_last_search_time() -> "datetime | None":
    """Return the created_at timestamp of the most recent SearchCache entry."""
    db = SessionLocal()
    try:
        from sqlalchemy import desc
        row = db.query(SearchCache).order_by(desc(SearchCache.created_at)).first()
        return row.created_at if row else None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ArchivedFile CRUD
# ---------------------------------------------------------------------------

def create_archived_file(task_id: str, original_path: str, archive_path: str) -> ArchivedFile:
    """Insert an ArchivedFile record. Paths should be relative to the project root."""
    db = SessionLocal()
    try:
        row = ArchivedFile(
            task_id=task_id,
            original_path=original_path,
            archive_path=archive_path,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def get_archived_files_for_task(task_id: str) -> "list[ArchivedFile]":
    """Return all archived files for a task, most recent first."""
    db = SessionLocal()
    try:
        return (
            db.query(ArchivedFile)
            .filter(ArchivedFile.task_id == task_id)
            .order_by(ArchivedFile.deleted_at.desc())
            .all()
        )
    finally:
        db.close()


def get_archived_file(archive_id: int) -> "ArchivedFile | None":
    """Return a single archived file record by ID, or None."""
    db = SessionLocal()
    try:
        return db.query(ArchivedFile).filter(ArchivedFile.id == archive_id).first()
    finally:
        db.close()


def mark_archived_file_restored(archive_id: int) -> bool:
    """Set restored_at to now for the given archive record. Returns True on success."""
    db = SessionLocal()
    try:
        row = db.query(ArchivedFile).filter(ArchivedFile.id == archive_id).first()
        if not row:
            return False
        row.restored_at = datetime.utcnow()
        db.commit()
        return True
    except Exception:
        db.rollback()
        return False
    finally:
        db.close()

