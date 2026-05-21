"""
Tests for GAP 7 — semantic episodic memory.

Database isolation: conftest.py handles PostgreSQL test DB rollback.
LLM isolation: embed_text is mocked everywhere; no real embedding calls.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import text

from app.database import upsert_project, create_task, get_episodic_summary_job


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _settings(enabled: bool = True, llm_id: int = 1, half_life: int = 90, keepalive: int = 14, k: int = 3):
    return SimpleNamespace(
        EPISODIC_MEMORY_ENABLED=enabled,
        EPISODIC_MEMORY_EMBEDDING_LLM_ID=llm_id,
        EPISODIC_MEMORY_DECAY_HALF_LIFE_DAYS=half_life,
        EPISODIC_MEMORY_KEEPALIVE_EXTENSION_DAYS=keepalive,
        EPISODIC_MEMORY_AUTO_INJECT_K=k,
    )


def _fake_embedding(dim: int = 1536) -> list[float]:
    """Return a unit vector embedding for mocking (must match the vector(1536) column)."""
    return [1.0 / dim] * dim


def _make_project(name: str = "_ep_test") -> "Project":
    from app.database import Project
    p = upsert_project(name, path="/tmp/ep_test")
    assert p is not None, "upsert_project returned None"
    return p


# ---------------------------------------------------------------------------
# 1. insert_episode — expires_at is ~5 years from now
# ---------------------------------------------------------------------------

class TestInsertEpisode:

    def test_expires_at_five_years(self):
        from app.agent.episodic_memory import insert_episode

        project = _make_project()
        cfg = _settings()

        with patch("app.agent.episodic_memory.embed_text", return_value=_fake_embedding()):
            ep_id = insert_episode(
                project_id=project.id,
                task_id=None,
                episode_type="failure",
                content="Something went wrong during implementation.",
                metadata={"stage_key": "indev"},
                settings=cfg,
            )

        assert ep_id is not None

        from app.database import SessionLocal as _SL
        with _SL() as db:
            row = db.execute(
                text("SELECT created_at, expires_at FROM episodic_memory WHERE id = :id"),
                {"id": ep_id},
            ).fetchone()

        assert row is not None
        created_at = row[0]
        expires_at = row[1]
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)

        delta_days = (expires_at - created_at).days
        # Should be approximately 5 * 365 = 1825 days (±5 days tolerance)
        assert 1820 <= delta_days <= 1830, f"expires_at delta is {delta_days} days, expected ~1825"

    def test_disabled_returns_none(self):
        from app.agent.episodic_memory import insert_episode

        project = _make_project()
        cfg = _settings(enabled=False)

        result = insert_episode(
            project_id=project.id,
            task_id=None,
            episode_type="failure",
            content="Should not be stored.",
            metadata={},
            settings=cfg,
        )
        assert result is None

    def test_no_llm_id_returns_none(self):
        from app.agent.episodic_memory import insert_episode

        project = _make_project()
        cfg = _settings(llm_id=None)

        result = insert_episode(
            project_id=project.id,
            task_id=None,
            episode_type="document",
            content="Also should not be stored.",
            metadata={},
            settings=cfg,
        )
        assert result is None


# ---------------------------------------------------------------------------
# 2. query_episodes — results ordered by cosine × recency
# ---------------------------------------------------------------------------

class TestQueryEpisodes:

    def _insert_rows(self, project_id: int, rows: list[dict], cfg) -> list[int]:
        from app.agent.episodic_memory import insert_episode

        ids = []
        for row in rows:
            with patch("app.agent.episodic_memory.embed_text", return_value=row["embedding"]):
                ep_id = insert_episode(
                    project_id=project_id,
                    task_id=None,
                    episode_type=row.get("type", "failure"),
                    content=row["content"],
                    metadata={},
                    settings=cfg,
                )
            assert ep_id is not None
            ids.append(ep_id)
        return ids

    def test_returns_top_k_ordered_by_relevance(self):
        from app.agent.episodic_memory import query_episodes

        project = _make_project()
        cfg = _settings(k=2)

        # Two embeddings: one close to query, one far (padded to 1536 dims)
        dim = 1536
        close_emb = [0.9] + [0.0] * (dim - 1)
        far_emb   = [0.0] * (dim - 1) + [0.9]
        query_emb = [1.0] + [0.0] * (dim - 1)

        self._insert_rows(project.id, [
            {"content": "close match episode", "embedding": close_emb},
            {"content": "far mismatch episode", "embedding": far_emb},
        ], cfg)

        with patch("app.agent.episodic_memory.embed_text", return_value=query_emb):
            results = query_episodes(
                project_id=project.id,
                question="close match question",
                k=2,
                settings=cfg,
            )

        assert len(results) >= 1
        # The first result should be the one with embedding closer to query
        assert "close" in results[0]["content"]

    def test_episode_type_filter(self):
        from app.agent.episodic_memory import insert_episode, query_episodes

        project = _make_project()
        cfg = _settings()
        emb = _fake_embedding()

        with patch("app.agent.episodic_memory.embed_text", return_value=emb):
            insert_episode(project.id, None, "failure",  "a failure event",  {}, cfg)
            insert_episode(project.id, None, "document", "a document entry", {}, cfg)

        with patch("app.agent.episodic_memory.embed_text", return_value=emb):
            results = query_episodes(project.id, "anything", k=10, settings=cfg, episode_type="failure")

        assert all(ep["episode_type"] == "failure" for ep in results)

    def test_keepalive_extended_on_retrieval(self):
        from app.agent.episodic_memory import insert_episode, query_episodes

        project = _make_project()
        cfg = _settings(keepalive=14)
        emb = _fake_embedding()

        with patch("app.agent.episodic_memory.embed_text", return_value=emb):
            ep_id = insert_episode(project.id, None, "failure", "keepalive test", {}, cfg)

        # Record expires_at before retrieval — lazy import so conftest's patched SessionLocal is used
        from app.database import SessionLocal as _SL
        with _SL() as db:
            before = db.execute(
                text("SELECT expires_at FROM episodic_memory WHERE id = :id"), {"id": ep_id}
            ).fetchone()[0]
            if before.tzinfo is None:
                before = before.replace(tzinfo=timezone.utc)

        with patch("app.agent.episodic_memory.embed_text", return_value=emb):
            query_episodes(project.id, "keepalive test", k=5, settings=cfg)

        from app.database import SessionLocal as _SL2
        with _SL2() as db:
            after = db.execute(
                text("SELECT expires_at FROM episodic_memory WHERE id = :id"), {"id": ep_id}
            ).fetchone()[0]
            if after.tzinfo is None:
                after = after.replace(tzinfo=timezone.utc)

        # expires_at must not have decreased
        assert after >= before

    def test_expired_episode_not_returned(self):
        from app.agent.episodic_memory import query_episodes

        project = _make_project()
        cfg = _settings()
        emb = _fake_embedding()

        # Directly insert an already-expired row — lazy import picks up conftest's patched SessionLocal
        emb_str = "[" + ",".join(str(f) for f in emb) + "]"
        from app.database import SessionLocal as _SL
        with _SL() as db:
            db.execute(
                text("""
                INSERT INTO episodic_memory
                    (project_id, task_id, episode_type, content, embedding, metadata, created_at, expires_at)
                VALUES
                    (:pid, NULL, 'failure', 'expired episode', CAST(:emb AS vector),
                     '{}', now() - INTERVAL '6 years', now() - INTERVAL '1 second')
                """),
                {"pid": project.id, "emb": emb_str},
            )
            db.commit()

        with patch("app.agent.episodic_memory.embed_text", return_value=emb):
            results = query_episodes(project.id, "expired", k=10, settings=cfg)

        contents = [ep["content"] for ep in results]
        assert "expired episode" not in contents


# ---------------------------------------------------------------------------
# 3. Recency weight
# ---------------------------------------------------------------------------

class TestRecencyWeight:

    def test_today_has_weight_one(self):
        from app.agent.episodic_memory import _recency_weight

        now = datetime.now(timezone.utc)
        w = _recency_weight(now, half_life_days=90, now=now)
        assert abs(w - 1.0) < 1e-9

    def test_half_life_halves_weight(self):
        from app.agent.episodic_memory import _recency_weight

        now = datetime.now(timezone.utc)
        past = now - timedelta(days=90)
        w = _recency_weight(past, half_life_days=90, now=now)
        assert abs(w - 0.5) < 1e-6

    def test_double_half_life_quarter_weight(self):
        from app.agent.episodic_memory import _recency_weight

        now = datetime.now(timezone.utc)
        past = now - timedelta(days=180)
        w = _recency_weight(past, half_life_days=90, now=now)
        assert abs(w - 0.25) < 1e-6


# ---------------------------------------------------------------------------
# 4. Failure write path — demotion triggers insert_episode
# ---------------------------------------------------------------------------

class TestDemotionWritePath:

    def test_demotion_inserts_failure_episode(self, monkeypatch):
        """_record_demotion should call insert_episode with episode_type='failure'."""
        from app.database import create_task

        project = _make_project()
        task = create_task(
            title="test demotion task",
            task_type="indev",
            description="",
            owner="user",
            llm_id=None,
            budget_id=None,
            project=project.name,
        )
        assert task is not None

        inserted: list[dict] = []

        def fake_insert(project_id, task_id, episode_type, content, metadata, settings):
            inserted.append({"project_id": project_id, "episode_type": episode_type, "content": content})
            return 999

        monkeypatch.setattr("app.agent.episodic_memory.insert_episode", fake_insert)

        import app.agent.config as _cfg
        monkeypatch.setattr(_cfg, "EPISODIC_MEMORY_ENABLED", True)

        from app.main import _record_demotion
        _record_demotion(task.id, from_stage="indev", to_stage="planning", reason="tests failed")

        assert len(inserted) == 1
        ep = inserted[0]
        assert ep["episode_type"] == "failure"
        assert "tests failed" in ep["content"]


# ---------------------------------------------------------------------------
# 5. Document write path — store_document triggers insert_episode for len > 100
# ---------------------------------------------------------------------------

class TestDocumentWritePath:

    def test_long_document_triggers_episode(self, monkeypatch):
        from app.database.crud_documents import store_document

        project = _make_project()

        inserted: list[dict] = []

        def fake_insert(project_id, task_id, episode_type, content, metadata, settings):
            inserted.append({"episode_type": episode_type, "content": content})
            return 42

        monkeypatch.setattr("app.agent.episodic_memory.insert_episode", fake_insert)

        import app.agent.config as _cfg
        monkeypatch.setattr(_cfg, "EPISODIC_MEMORY_ENABLED", True)

        long_content = "A" * 200
        store_document(project.id, "test-doc", long_content)

        assert len(inserted) == 1
        assert inserted[0]["episode_type"] == "document"

    def test_short_document_skips_episode(self, monkeypatch):
        from app.database.crud_documents import store_document

        project = _make_project()

        inserted: list[dict] = []

        def fake_insert(*args, **kwargs):
            inserted.append(True)
            return 43

        monkeypatch.setattr("app.agent.episodic_memory.insert_episode", fake_insert)

        import app.agent.config as _cfg
        monkeypatch.setattr(_cfg, "EPISODIC_MEMORY_ENABLED", True)

        short_content = "A" * 50  # ≤ 100 chars
        store_document(project.id, "short-doc", short_content)

        assert len(inserted) == 0


# ---------------------------------------------------------------------------
# 6. EpisodicSummaryJob CRUD round-trip
# ---------------------------------------------------------------------------

class TestEpisodicSummaryJobCRUD:

    def test_create_and_fetch(self):
        from app.database import create_episodic_summary_job, get_pending_episodic_summary_jobs, EpisodicSummaryJob

        project = _make_project()
        task = create_task(
            title="summary job task",
            task_type="indev",
            description="",
            owner="user",
            llm_id=None,
            budget_id=None,
            project=project.name,
        )

        job = create_episodic_summary_job(task_id=task.id, final_status="ACCEPTED")
        assert job is not None
        assert job.status == "pending"
        assert job.final_status == "ACCEPTED"

        pending = get_pending_episodic_summary_jobs(limit=10)
        ids = [j.id for j in pending]
        assert job.id in ids

    def test_update_sets_completed_at(self):
        from app.database import create_episodic_summary_job, update_episodic_summary_job, EpisodicSummaryJob

        project = _make_project()
        task = create_task(
            title="update job task",
            task_type="indev",
            description="",
            owner="user",
            llm_id=None,
            budget_id=None,
            project=project.name,
        )

        job = create_episodic_summary_job(task_id=task.id, final_status="REJECTED")
        assert job is not None

        update_episodic_summary_job(job.id, status="completed")

        updated = get_episodic_summary_job(job.id)
        assert updated is not None
        assert updated.status == "completed"
        assert updated.completed_at is not None
