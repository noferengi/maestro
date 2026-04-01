"""
Tests for file summary enrichment:
- list_directory with inline summaries
- async_dispatch_tool write interception
- prewarm_project_summaries
- build_snapshot_with_summaries
- Migration 0023 round-trip
- execute_file_summary prompt branching (update vs new)
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import types
from unittest.mock import MagicMock, patch, AsyncMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_summary_row(summary: str, short_summary: str | None = None) -> MagicMock:
    row = MagicMock()
    row.summary = summary
    row.short_summary = short_summary  # None → simulates pre-migration rows
    return row


# ===========================================================================
# 1. list_directory enrichment
# ===========================================================================

class TestListDirectoryEnrichment:
    def test_shows_cached_summary_inline(self, tmp_path):
        (tmp_path / "foo.py").write_text("x = 1")
        summary_row = _make_summary_row("Does something useful. Has one global.")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value="Does something useful. Has one global."),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "foo.py" in result
        assert "Does something useful" in result

    def test_cache_miss_shows_placeholder(self, tmp_path, caplog):
        (tmp_path / "bar.py").write_text("pass")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "SUMMARY NOT AVAILABLE" in result

    def test_git_dir_shown_as_protected(self, tmp_path):
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "[PROTECTED]" in result
        assert ".git" in result

    def test_gitignored_entry_shown_as_protected(self, tmp_path):
        secret = tmp_path / ".env"
        secret.write_text("SECRET=abc")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value={str(secret)}),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "gitignored" in result
        assert ".env" in result

    def test_symlink_escaping_shown_as_protected(self, tmp_path):
        # Create a regular file — we'll make the escape helper claim it's an escaping symlink
        link = tmp_path / "outside_link"
        link.write_text("content")

        def _fake_escape(abs_path, root):
            return os.path.basename(abs_path) == "outside_link"

        # Also patch os.path.islink so readlink fallback doesn't fail on non-symlink
        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", side_effect=_fake_escape),
            patch("os.readlink", return_value="/outside/path"),
            patch("os.path.islink", return_value=True),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "symlink escapes project" in result

    def test_excluded_dirs_hidden(self, tmp_path):
        (tmp_path / "venv").mkdir()
        (tmp_path / "visible.py").write_text("pass")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", {"venv"}),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
        ):
            from app.agent.tools import list_directory
            result = list_directory(str(tmp_path))

        assert "venv" not in result
        assert "hidden" in result

    def test_no_llm_calls_ever(self, tmp_path):
        """list_directory must never make LLM calls."""
        (tmp_path / "f.py").write_text("pass")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(tmp_path)),
            patch("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive")),
            patch("app.agent.tools.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.tools.LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.tools._get_cached_summary_for_listing", return_value=None),
            patch("app.agent.project_snapshot._is_git_ignored", return_value=set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=False),
            patch("app.agent.llm_client.call_llm") as mock_llm,
        ):
            from app.agent.tools import list_directory
            list_directory(str(tmp_path))

        mock_llm.assert_not_called()


# ===========================================================================
# 2. Write interception in async_dispatch_tool
# ===========================================================================

class TestWriteInterception:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_write_file_enqueues_at_high_priority(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("old content")

        enqueue_calls = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, previous_summary, priority):
            enqueue_calls.append({"path": path, "priority": priority, "prev": previous_summary})
            return ("key", "sha1", 100)

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: written"),
            patch("app.agent.tools.get_file_summary_by_path", return_value=None, create=True),
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.clear_snapshot_cache"),
        ):
            from app.agent.tools import async_dispatch_tool
            result = self._run(async_dispatch_tool(
                "write_file", {"path": str(f), "content": "new"},
                task_id="t1", llm_id=5, budget_id=1,
            ))

        assert result == "OK: written"
        assert len(enqueue_calls) == 1
        assert enqueue_calls[0]["priority"] == -2.0

    def test_captures_old_summary_as_previous_summary(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("existing")
        old_row = _make_summary_row("Old summary text.")

        enqueue_calls = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, previous_summary, priority):
            enqueue_calls.append(previous_summary)
            return ("key", "sha1", 100)

        def fake_get_summary(path):
            return old_row

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: written"),
            patch("app.database.get_file_summary_by_path", fake_get_summary),
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.clear_snapshot_cache"),
        ):
            from app.agent.tools import async_dispatch_tool
            self._run(async_dispatch_tool(
                "write_file", {"path": str(f), "content": "new"},
                task_id="t1", llm_id=5, budget_id=1,
            ))

        assert enqueue_calls[0] == "Old summary text."

    def test_skips_enqueue_when_llm_id_none(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("x")

        enqueue_calls = []

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: written"),
            patch(
                "app.agent.file_summary_agent.enqueue_file_summary",
                side_effect=lambda *a, **kw: enqueue_calls.append(1),
            ),
        ):
            from app.agent.tools import async_dispatch_tool
            self._run(async_dispatch_tool(
                "write_file", {"path": str(f), "content": "new"},
                task_id="t1", llm_id=None, budget_id=None,
            ))

        assert len(enqueue_calls) == 0

    def test_clears_snapshot_cache_on_write(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("x")

        cleared = []

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: written"),
            patch("app.database.get_file_summary_by_path", return_value=None),
            patch("app.agent.file_summary_agent.enqueue_file_summary", return_value=("k", "s", 1)),
            patch("app.agent.project_snapshot.clear_snapshot_cache", side_effect=lambda: cleared.append(1)),
        ):
            from app.agent.tools import async_dispatch_tool
            self._run(async_dispatch_tool(
                "write_file", {"path": str(f), "content": "new"},
                task_id="t1", llm_id=5, budget_id=1,
            ))

        assert len(cleared) == 1

    def test_append_file_same_pattern(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("x")

        enqueue_calls = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, previous_summary, priority):
            enqueue_calls.append(priority)
            return ("key", "sha1", 100)

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: appended"),
            patch("app.database.get_file_summary_by_path", return_value=None),
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.clear_snapshot_cache"),
        ):
            from app.agent.tools import async_dispatch_tool
            self._run(async_dispatch_tool(
                "append_file", {"path": str(f), "content": "more"},
                task_id="t1", llm_id=5, budget_id=1,
            ))

        assert enqueue_calls == [-2.0]

    def test_enqueue_failure_does_not_fail_write(self, tmp_path):
        f = tmp_path / "file.py"
        f.write_text("x")

        with (
            patch("app.agent.tools._assert_safe_path", return_value=str(f)),
            patch("app.agent.tools.dispatch_tool", return_value="OK: written"),
            patch("app.database.get_file_summary_by_path", return_value=None),
            patch("app.agent.file_summary_agent.enqueue_file_summary",
                  side_effect=RuntimeError("boom")),
            patch("app.agent.project_snapshot.clear_snapshot_cache"),
        ):
            from app.agent.tools import async_dispatch_tool
            result = self._run(async_dispatch_tool(
                "write_file", {"path": str(f), "content": "new"},
                task_id="t1", llm_id=5, budget_id=1,
            ))

        assert result == "OK: written"


# ===========================================================================
# 3. prewarm_project_summaries
# ===========================================================================

class TestPrewarm:
    def test_enqueues_uncached_files(self, tmp_path):
        (tmp_path / "a.py").write_text("x = 1")

        enqueued = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            enqueued.append(path)
            return ("key", "sha1", 100)  # non-empty key = new job

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", set()),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            count = prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert count == 1
        assert any("a.py" in p for p in enqueued)

    def test_skips_cached_files(self, tmp_path):
        (tmp_path / "cached.py").write_text("pass")

        enqueued = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            enqueued.append(path)
            return ("", "sha1", 10)  # empty key = cache hit

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", set()),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            count = prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert count == 0  # cache hit → not counted

    def test_skips_excluded_dirs(self, tmp_path):
        (tmp_path / "venv").mkdir()
        (tmp_path / "venv" / "lib.py").write_text("pass")

        enqueued = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            enqueued.append(path)
            return ("key", "sha1", 10)

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", {"venv"}),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert not any("venv" in p for p in enqueued)

    def test_skips_binary_files(self, tmp_path):
        binary = tmp_path / "data.bin"
        binary.write_bytes(b"\x00\x01\x02\x03" * 100)

        enqueued = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            enqueued.append(path)
            return ("key", "sha1", 10)

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", set()),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert not any("data.bin" in p for p in enqueued)

    def test_skips_symlink_escaping(self, tmp_path):
        # Create a regular file and treat it as an escaping symlink via patch
        fake_link = tmp_path / "escape"
        fake_link.write_text("pretend symlink")

        enqueued = []

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            enqueued.append(path)
            return ("key", "sha1", 10)

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", set()),
            patch("app.agent.project_snapshot._is_symlink_escaping", return_value=True),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert len(enqueued) == 0

    def test_returns_correct_count(self, tmp_path):
        for i in range(3):
            (tmp_path / f"f{i}.py").write_text("pass")

        calls = [0]

        def fake_enqueue(path, *, task_id, llm_id, budget_id, priority):
            calls[0] += 1
            return ("key", "sha1", 10)

        with (
            patch("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue),
            patch("app.agent.project_snapshot.TOOL_LISTING_EXCLUDED_DIRS", set()),
        ):
            from app.agent.project_snapshot import prewarm_project_summaries
            count = prewarm_project_summaries(str(tmp_path), llm_id=1, budget_id=1)

        assert count == 3


# ===========================================================================
# 4. build_snapshot_with_summaries
# ===========================================================================

class TestBuildSnapshotWithSummaries:
    def _clear_cache(self):
        from app.agent.project_snapshot import _snapshot_cache
        _snapshot_cache.clear()

    def test_summary_inline_for_file(self, tmp_path):
        self._clear_cache()
        (tmp_path / "main.py").write_text("x = 1\n" * 5)
        row = _make_summary_row("FastAPI entry point. Starts the scheduler on lifespan.")

        with (
            patch("app.database.get_file_summary_by_path", return_value=row),
            patch("app.agent.project_snapshot.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.project_snapshot._snapshot_max_depth", return_value=5),
            patch("app.agent.project_snapshot._snapshot_max_tokens", return_value=50000),
            patch("app.agent.project_snapshot._snapshot_cache_ttl", return_value=0),
        ):
            from app.agent.project_snapshot import build_snapshot_with_summaries
            result = build_snapshot_with_summaries(str(tmp_path))

        assert "FastAPI entry point" in result
        assert "main.py" in result

    def test_cache_miss_silent(self, tmp_path):
        self._clear_cache()
        (tmp_path / "unknown.py").write_text("pass\n")

        with (
            patch("app.database.get_file_summary_by_path", return_value=None),
            patch("app.agent.project_snapshot.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.project_snapshot._snapshot_max_depth", return_value=5),
            patch("app.agent.project_snapshot._snapshot_max_tokens", return_value=50000),
            patch("app.agent.project_snapshot._snapshot_cache_ttl", return_value=0),
        ):
            from app.agent.project_snapshot import build_snapshot_with_summaries
            result = build_snapshot_with_summaries(str(tmp_path))

        # No placeholder noise
        assert "SUMMARY NOT AVAILABLE" not in result
        assert "unknown.py" in result

    def test_cache_hit_on_second_call(self, tmp_path):
        self._clear_cache()
        (tmp_path / "f.py").write_text("pass\n")

        call_count = [0]
        orig_row = _make_summary_row("A summary.")

        def counting_get(path):
            call_count[0] += 1
            return orig_row

        with (
            patch("app.database.get_file_summary_by_path", side_effect=counting_get),
            patch("app.agent.project_snapshot.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.project_snapshot._snapshot_max_depth", return_value=5),
            patch("app.agent.project_snapshot._snapshot_max_tokens", return_value=50000),
            patch("app.agent.project_snapshot._snapshot_cache_ttl", return_value=300),
        ):
            from app.agent.project_snapshot import build_snapshot_with_summaries
            build_snapshot_with_summaries(str(tmp_path))
            before = call_count[0]
            build_snapshot_with_summaries(str(tmp_path))
            after = call_count[0]

        # Second call hit the in-memory cache — no new DB calls
        assert after == before

    def test_token_budget_respected(self, tmp_path):
        self._clear_cache()
        for i in range(20):
            (tmp_path / f"f{i:02d}.py").write_text("pass\n")

        row = _make_summary_row("X" * 200)

        with (
            patch("app.database.get_file_summary_by_path", return_value=row),
            patch("app.agent.project_snapshot.PROJECT_ROOT", str(tmp_path)),
            patch("app.agent.project_snapshot._snapshot_max_depth", return_value=5),
            patch("app.agent.project_snapshot._snapshot_max_tokens", return_value=50),
            patch("app.agent.project_snapshot._snapshot_cache_ttl", return_value=0),
        ):
            from app.agent.project_snapshot import build_snapshot_with_summaries
            result = build_snapshot_with_summaries(str(tmp_path))

        assert "truncated" in result


# ===========================================================================
# 5. Migration 0023
# ===========================================================================

class TestMigration0023:
    def _apply(self, conn):
        import importlib, sys
        mod_path = os.path.join(
            os.path.dirname(__file__), "..", "migrations", "versions",
            "0023_file_summary_previous_summary.py",
        )
        spec = importlib.util.spec_from_file_location("mig0023", mod_path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        # Need prerequisite tables
        conn.execute("""
            CREATE TABLE IF NOT EXISTS llms (
                id INTEGER PRIMARY KEY, address TEXT, port INTEGER, model TEXT,
                parallel_sessions INTEGER DEFAULT 1, notes TEXT, max_context INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS budgets (id INTEGER PRIMARY KEY, name TEXT)
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha1_hash TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                summary TEXT NOT NULL,
                static_analysis_json TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS file_summary_jobs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sha1_hash TEXT NOT NULL,
                file_size_bytes INTEGER NOT NULL,
                file_path TEXT NOT NULL,
                file_content TEXT NOT NULL,
                static_analysis_json TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                priority REAL NOT NULL DEFAULT -1.0,
                llm_id INTEGER REFERENCES llms(id),
                budget_id INTEGER REFERENCES budgets(id),
                task_id TEXT,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                error_message TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                completed_at DATETIME
            )
        """)
        conn.commit()
        mod.up(conn)
        return mod

    def test_previous_summary_column_exists(self):
        with sqlite3.connect(":memory:") as conn:
            self._apply(conn)
            cols = {row[1] for row in conn.execute("PRAGMA table_info(file_summary_jobs)")}
        assert "previous_summary" in cols

    def test_create_job_round_trips_previous_summary(self):
        with sqlite3.connect(":memory:") as conn:
            self._apply(conn)
            conn.execute("""
                INSERT INTO file_summary_jobs
                (sha1_hash, file_size_bytes, file_path, file_content, previous_summary)
                VALUES ('abc', 10, '/f.py', 'content', 'Old summary here.')
            """)
            conn.commit()
            row = conn.execute(
                "SELECT previous_summary FROM file_summary_jobs WHERE sha1_hash='abc'"
            ).fetchone()
        assert row[0] == "Old summary here."


# ===========================================================================
# 6. execute_file_summary prompt branching (added to match plan spec)
# ===========================================================================

class TestExecuteFileSummaryPromptBranching:
    def _run(self, coro):
        return asyncio.run(coro)

    def test_update_prompt_used_when_previous_summary_set(self):
        captured_prompts = []

        async def fake_call_llm(messages, **kw):
            captured_prompts.append(messages[0]["content"])
            return {
                "choices": [{"message": {"content": "New summary here."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        # call_llm is imported lazily inside execute_file_summary via
        # "from app.agent.llm_client import call_llm" — patch the source module
        with (
            patch("app.agent.llm_client.call_llm", fake_call_llm),
            patch("app.database.create_file_summary"),
        ):
            from app.agent.file_summary_agent import execute_file_summary
            self._run(execute_file_summary(
                sha1="abc", filesize=10,
                file_path="/tmp/f.py",
                file_content="def foo(): pass\n",
                previous_summary="Old summary.",
            ))

        assert "Previous summary" in captured_prompts[0]
        assert "Old summary." in captured_prompts[0]

    def test_standard_prompt_used_when_no_previous_summary(self):
        captured_prompts = []

        async def fake_call_llm(messages, **kw):
            captured_prompts.append(messages[0]["content"])
            return {
                "choices": [{"message": {"content": "A summary."}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5},
            }

        with (
            patch("app.agent.llm_client.call_llm", fake_call_llm),
            patch("app.database.create_file_summary"),
        ):
            from app.agent.file_summary_agent import execute_file_summary
            self._run(execute_file_summary(
                sha1="abc", filesize=10,
                file_path="/tmp/f.py",
                file_content="def foo(): pass\n",
                previous_summary=None,
            ))

        assert "Analyze this source file" in captured_prompts[0]
        assert "Previous summary" not in captured_prompts[0]
