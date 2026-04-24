"""
CRUD operations for file and search cache tables.

FileSummary   — DB-cached natural-language LLM summaries of source files,
                keyed by (sha1_hash, file_size_bytes).  Path is informational.
                create_file_summary uses INSERT-then-catch semantics so
                concurrent agents summarising the same file don't crash.

SearchCache   — cached web search results, keyed by (query, provider).
                Prevents redundant Brave/DuckDuckGo API calls.
"""

import logging
import os

from .session import SessionLocal
from .models import FileSummary, SearchCache

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
    # SQLite LIKE is case-insensitive on Windows; use a trailing separator so we
    # don't accidentally match a sibling directory with the same prefix.
    prefix = root.replace("\\", "/") + "/"
    prefix_back = root + "\\"
    db = SessionLocal()
    try:
        # Match both slash styles since file_path values may use either separator.
        from sqlalchemy import or_
        return (
            db.query(FileSummary)
            .filter(
                or_(
                    FileSummary.file_path.like(prefix + "%"),
                    FileSummary.file_path.like(prefix_back + "%"),
                )
            )
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

