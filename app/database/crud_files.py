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
