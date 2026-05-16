"""
Tests for the project document store (Phase 8).

Covers: store, get, upsert semantics, soft-delete, tag filter, case normalization.
The fuzzy-match test (pg_trgm) is skipped on SQLite because PostgreSQL's
similarity() function is unavailable there.

get_document returns a dict {"key", "content", "tags", ...} or None.
"""

import pytest
from app.database.session import engine

PROJECT_NAME = "doctest_proj"


def _ensure_project():
    from app.database import upsert_project
    upsert_project(PROJECT_NAME)


class TestDocumentStore:
    @pytest.fixture(autouse=True)
    def setup_project(self):
        _ensure_project()

    def test_store_and_get_document(self):
        from app.agent.doc_store import store_document, get_document
        store_document(PROJECT_NAME, "test/doc", "hello world", tags=["a"], written_by_task_id=None)
        result = get_document(PROJECT_NAME, "test/doc")
        assert result is not None
        assert result["content"] == "hello world"

    def test_upsert_replaces_content(self):
        from app.agent.doc_store import store_document, get_document
        from app.database.session import SessionLocal
        from app.database.models import ProjectDocument

        store_document(PROJECT_NAME, "upsert/key", "v1", written_by_task_id=None)
        store_document(PROJECT_NAME, "upsert/key", "v2", written_by_task_id=None)

        result = get_document(PROJECT_NAME, "upsert/key")
        assert result is not None
        assert result["content"] == "v2"

        db = SessionLocal()
        try:
            rows = db.query(ProjectDocument).filter_by(key="upsert/key").all()
            assert len(rows) == 1, "Upsert must not create duplicate rows"
        finally:
            db.close()

    def test_store_normalizes_key_case(self):
        from app.agent.doc_store import store_document, get_document
        store_document(PROJECT_NAME, "Test/DOC", "case content", written_by_task_id=None)
        result = get_document(PROJECT_NAME, "test/doc")
        assert result is not None
        assert result["content"] == "case content"
        result2 = get_document(PROJECT_NAME, "TEST/Doc")
        assert result2 is not None
        assert result2["content"] == "case content"

    @pytest.mark.skipif(
        engine.dialect.name == "sqlite",
        reason="JSONB @> tag filter is PostgreSQL-only",
    )
    def test_list_with_tag_filter(self):
        from app.agent.doc_store import store_document, list_documents
        store_document(PROJECT_NAME, "tagged/a", "alpha", tags=["math"], written_by_task_id=None)
        store_document(PROJECT_NAME, "tagged/b", "beta", tags=["other"], written_by_task_id=None)
        docs = list_documents(PROJECT_NAME, tag="math")
        keys = [d["key"] for d in docs]
        assert "tagged/a" in keys
        assert "tagged/b" not in keys

    def test_soft_delete(self):
        from app.agent.doc_store import store_document, get_document, delete_document
        from app.database.session import SessionLocal
        from app.database.models import ProjectDocument

        store_document(PROJECT_NAME, "delete/me", "gone soon", written_by_task_id=None)
        deleted = delete_document(PROJECT_NAME, "delete/me", deleted_by_task_id=None)
        assert deleted is True
        assert get_document(PROJECT_NAME, "delete/me") is None

        db = SessionLocal()
        try:
            row = db.query(ProjectDocument).filter_by(key="delete/me").first()
            assert row is not None
            assert row.deleted_at is not None
        finally:
            db.close()

    @pytest.mark.skipif(
        engine.dialect.name == "sqlite",
        reason="pg_trgm similarity() is PostgreSQL-only",
    )
    def test_fuzzy_get_finds_close_key(self):
        from app.agent.doc_store import store_document, fuzzy_get_document
        store_document(PROJECT_NAME, "fuzzy/document", "needle", written_by_task_id=None)
        results = fuzzy_get_document(PROJECT_NAME, "fuzzy/docment", threshold=0.3)
        keys = [r["key"] for r in results]
        assert "fuzzy/document" in keys
