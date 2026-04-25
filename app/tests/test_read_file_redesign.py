"""
Tests for the tiered file reading redesign:
  - read_file() returns structural summary + marks file as prepped
  - read_file_harder() requires prep, then returns source lines
  - _prepped_files ContextVar isolation across async tasks
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def patch_project_root(tmp_path, monkeypatch):
    """Redirect PROJECT_ROOT to tmp_path so all file ops are allowed."""
    monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive"))
    return tmp_path


@pytest.fixture(autouse=True)
def reset_prepped_files():
    """Clear the _prepped_files ContextVar before each test."""
    from app.agent.tools import _prepped_files
    _prepped_files.set(None)
    yield
    _prepped_files.set(None)


def _make_file(tmp_path, content: str, name: str = "sample.py") -> str:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return str(p)


# ---------------------------------------------------------------------------
# read_file returns summary, not raw content
# ---------------------------------------------------------------------------

def test_read_file_returns_summary(tmp_path):
    from app.agent.tools import read_file

    # File must be > 25 lines to trigger structural summary (not inline path)
    lines = ["def hello():", "    return 42"] + [f"# comment {i}" for i in range(30)]
    path = _make_file(tmp_path, "\n".join(lines) + "\n")
    result = read_file(path)

    assert "== FILE:" in result
    assert "hello" in result
    # Should NOT be raw source
    assert "return 42" not in result


def test_read_file_nonpython_returns_summary(tmp_path):
    from app.agent.tools import read_file

    # File must be > 25 lines to trigger structural summary (not inline path)
    content = "\n".join(f"line {i}" for i in range(30)) + "\n"
    path = _make_file(tmp_path, content, name="notes.txt")
    result = read_file(path)

    assert "== FILE:" in result
    assert "read_file_harder" in result


def test_read_file_missing(tmp_path):
    from app.agent.tools import read_file

    result = read_file(str(tmp_path / "does_not_exist.py"))
    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# _mark_file_prepped / _is_file_prepped
# ---------------------------------------------------------------------------

def test_read_file_marks_prepped(tmp_path):
    from app.agent.tools import read_file, _is_file_prepped

    path = _make_file(tmp_path, "x = 1\n")

    assert not _is_file_prepped(path)
    read_file(path)
    assert _is_file_prepped(path)


# ---------------------------------------------------------------------------
# read_file_harder without prep - returns summary, not source
# ---------------------------------------------------------------------------

def test_read_file_harder_without_prep(tmp_path):
    from app.agent.tools import read_file_harder

    # File must be > 25 lines so auto-prep returns the structural summary header
    lines = ["def foo():", "    pass"] + [f"# comment {i}" for i in range(30)]
    path = _make_file(tmp_path, "\n".join(lines) + "\n")
    result = read_file_harder(path, start=1)

    # Should have returned the summary (auto-prep path)
    assert "== FILE:" in result


def test_read_file_harder_with_prep_using_end(tmp_path):
    from app.agent.tools import read_file, read_file_harder

    # Use a large file (>25 lines) so read_file() returns structural summary,
    # not inline content - lines are NOT pre-served on the first call.
    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)  # prep step - structural summary only, no lines served
    result = read_file_harder(path, start=2, end=4)

    assert "2: line2" in result
    assert "3: line3" in result
    assert "4: line4" in result
    assert "line1" not in result
    assert "line5" not in result


def test_read_file_harder_start_count(tmp_path):
    from app.agent.tools import read_file, read_file_harder

    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)
    result = read_file_harder(path, start=2, count=3)

    assert "2: line2" in result
    assert "3: line3" in result
    assert "4: line4" in result
    assert "line5" not in result


def test_read_file_harder_no_args_serves_next_chunk(tmp_path):
    """read_file_harder(path) with no start/end serves the next unserved chunk."""
    from app.agent.tools import read_file, read_file_harder

    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)  # structural summary only
    result = read_file_harder(path)  # no args -> serve lines 1..N

    assert "1: line1" in result
    assert "ERROR" not in result


def test_read_file_harder_start_only_serves_250_from_start(tmp_path):
    """Providing only start (no end/count) serves up to 250 lines from that start."""
    from app.agent.tools import read_file, read_file_harder

    content = "\n".join(f"x{i}" for i in range(1, 400)) + "\n"
    path = _make_file(tmp_path, content, name="big.txt")

    read_file(path)
    result = read_file_harder(path, start=10)

    assert "10: x10" in result
    content_lines = [l for l in result.splitlines() if l and not l.startswith("==")]
    assert len(content_lines) <= 250
    assert "ERROR" not in result


def test_read_file_harder_rejects_both_end_and_count(tmp_path):
    from app.agent.tools import read_file, read_file_harder

    path = _make_file(tmp_path, "a\nb\n", name="data.txt")
    read_file(path)

    result = read_file_harder(path, start=1, end=2, count=2)
    assert "ERROR" in result


# ---------------------------------------------------------------------------
# ContextVar isolation - separate prepped sets per async task
# ---------------------------------------------------------------------------

def test_prepped_files_context_isolation(tmp_path):
    """Two concurrent async tasks must not share their prepped-files sets."""
    from app.agent.tools import read_file, _is_file_prepped, _prepped_files

    path_a = _make_file(tmp_path, "class A: pass\n", name="a.py")
    path_b = _make_file(tmp_path, "class B: pass\n", name="b.py")

    async def task_a():
        _prepped_files.set(None)  # fresh context for this task
        read_file(path_a)
        return _is_file_prepped(path_a), _is_file_prepped(path_b)

    async def task_b():
        _prepped_files.set(None)  # fresh context for this task
        read_file(path_b)
        return _is_file_prepped(path_a), _is_file_prepped(path_b)

    async def run():
        return await asyncio.gather(task_a(), task_b())

    (a_sees_a, a_sees_b), (b_sees_a, b_sees_b) = asyncio.run(run())

    assert a_sees_a is True   # task A prepped file A
    assert a_sees_b is False  # task A did NOT prep file B
    assert b_sees_a is False  # task B did NOT prep file A
    assert b_sees_b is True   # task B prepped file B


# ---------------------------------------------------------------------------
# 250-line cap on read_file_harder
# ---------------------------------------------------------------------------

def _make_nline_file(tmp_path, n: int) -> str:
    content = "\n".join(f"line{i}" for i in range(1, n + 1)) + "\n"
    return _make_file(tmp_path, content, name=f"big_{n}.txt")


def _content_lines(result: str) -> list[str]:
    """Strip the == FILE == header line(s) from _serve_file_lines output."""
    return [l for l in result.strip().splitlines() if l and not l.startswith("==")]


def test_250_line_cap_with_end(tmp_path):
    """Requesting more than 250 lines via 'end' is silently clamped."""
    from app.agent.tools import read_file, read_file_harder

    path = _make_nline_file(tmp_path, 400)
    read_file(path)

    result = read_file_harder(path, start=1, end=400)
    returned_lines = _content_lines(result)
    assert len(returned_lines) == 250
    assert returned_lines[0].startswith("1:")
    assert returned_lines[-1].startswith("250:")


def test_250_line_cap_with_count(tmp_path):
    """Requesting more than 250 lines via 'count' is silently clamped."""
    from app.agent.tools import read_file, read_file_harder

    path = _make_nline_file(tmp_path, 400)
    read_file(path)

    result = read_file_harder(path, start=1, count=300)
    returned_lines = _content_lines(result)
    assert len(returned_lines) == 250


def test_sequential_calls_independent(tmp_path):
    """Two sequential read_file_harder calls return their own ranges, no merging."""
    from app.agent.tools import read_file, read_file_harder

    path = _make_nline_file(tmp_path, 600)
    read_file(path)

    first = read_file_harder(path, start=1, count=250)
    second = read_file_harder(path, start=251, count=250)

    first_lines  = _content_lines(first)
    second_lines = _content_lines(second)

    assert len(first_lines) == 250
    assert len(second_lines) == 250
    assert first_lines[0].startswith("1:")
    assert first_lines[-1].startswith("250:")
    assert second_lines[0].startswith("251:")


def test_small_file_inline(tmp_path):
    """read_file on a file ≤ 25 lines returns raw content, not a summary header."""
    from app.agent.tools import read_file

    content = "\n".join(f"x = {i}" for i in range(10)) + "\n"  # 10 lines
    path = _make_file(tmp_path, content, name="tiny.py")

    result = read_file(path)

    # Inline path uses a different header
    assert "full content" in result
    # Raw source present
    assert "x = 0" in result
    assert "x = 9" in result


# ---------------------------------------------------------------------------
# async_build_file_summary
# ---------------------------------------------------------------------------

def _make_py_file(tmp_path, name="sample.py"):
    """Write a >25-line Python file so build_file_summary gives a structural header."""
    lines = ["def hello():", "    return 42"] + [f"# comment {i}" for i in range(30)]
    p = tmp_path / name
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(p)


@pytest.fixture(autouse=False)
def clean_session_cache():
    """Clear the in-memory file_summary_cache before and after each test."""
    from app.agent import project_snapshot
    project_snapshot._file_summary_cache.clear()
    yield
    project_snapshot._file_summary_cache.clear()


def test_abfs_summary_length_none_skips_enqueue(tmp_path, monkeypatch, clean_session_cache):
    """summary_length='none' returns structural immediately - enqueue never called."""
    import asyncio
    called = []

    def fake_enqueue(*args, **kwargs):
        called.append(True)
        return ("key", "sha1", 0)

    monkeypatch.setattr("app.agent.project_snapshot.enqueue_file_summary", fake_enqueue, raising=False)
    monkeypatch.setattr("app.agent.file_summary_agent.enqueue_file_summary", fake_enqueue, raising=False)

    path = _make_py_file(tmp_path)
    from app.agent.project_snapshot import async_build_file_summary
    result = asyncio.run(async_build_file_summary(path, summary_length="none"))

    assert "== FILE:" in result
    assert called == []


def test_abfs_session_cache_hit(tmp_path, clean_session_cache):
    """In-memory session cache hit returns stored result, never calls enqueue."""
    import asyncio
    from app.agent import project_snapshot

    path = _make_py_file(tmp_path)
    abs_path = __import__("os").path.normpath(__import__("os").path.abspath(path))
    stat = __import__("os").stat(abs_path)
    # Must use the "llm" prefix - async_build_file_summary uses ("llm", path, mtime, size)
    # to avoid colliding with structural entries written by build_file_summary.
    session_key = ("llm", abs_path, stat.st_mtime, stat.st_size)

    project_snapshot._file_summary_cache[session_key] = "CACHED_RESULT"

    called = []

    async def fake_enqueue(*args, **kwargs):
        called.append(True)

    # Patch to detect if enqueue is ever reached
    original_enqueue = None
    import app.agent.file_summary_agent as fsa
    original_enqueue = fsa.enqueue_file_summary

    def spy_enqueue(*args, **kwargs):
        called.append(True)
        return original_enqueue(*args, **kwargs)

    import importlib
    result = asyncio.run(project_snapshot.async_build_file_summary(path, summary_length="short"))

    assert result == "CACHED_RESULT"
    assert called == []


def test_abfs_db_cache_hit(tmp_path, monkeypatch, clean_session_cache):
    """DB cache hit: enqueue returns '' -> reads from DB, prepends '## Summary'."""
    import asyncio
    import hashlib

    path = _make_py_file(tmp_path)
    raw = open(path, "rb").read()
    sha1 = hashlib.sha1(raw).hexdigest()
    filesize = len(raw)

    # Stub enqueue to report cache hit (empty completion_key)
    monkeypatch.setattr(
        "app.agent.file_summary_agent.enqueue_file_summary",
        lambda *a, **kw: ("", sha1, filesize),
    )

    # Stub DB lookup to return a fake cached summary
    class FakeSummary:
        summary = "This file greets the world."

    import app.database as db_mod
    monkeypatch.setattr(db_mod, "get_file_summary", lambda s, f: FakeSummary())

    from app.agent.project_snapshot import async_build_file_summary
    result = asyncio.run(async_build_file_summary(path, summary_length="short"))

    assert result.startswith("## Summary\nThis file greets the world.")
    assert "== FILE:" in result


def test_abfs_db_cache_hit_populates_session_cache(tmp_path, monkeypatch, clean_session_cache):
    """After a DB cache hit the result is stored in the in-memory session cache."""
    import asyncio, hashlib
    from app.agent import project_snapshot

    path = _make_py_file(tmp_path)
    raw = open(path, "rb").read()
    sha1 = hashlib.sha1(raw).hexdigest()
    filesize = len(raw)

    monkeypatch.setattr(
        "app.agent.file_summary_agent.enqueue_file_summary",
        lambda *a, **kw: ("", sha1, filesize),
    )

    class FakeSummary:
        summary = "Cached summary text."

    import app.database as db_mod
    monkeypatch.setattr(db_mod, "get_file_summary", lambda s, f: FakeSummary())

    result = asyncio.run(project_snapshot.async_build_file_summary(path, summary_length="short"))

    # Session cache should now hold this result under the "llm" prefix key
    abs_path = __import__("os").path.normpath(__import__("os").path.abspath(path))
    stat = __import__("os").stat(abs_path)
    session_key = ("llm", abs_path, stat.st_mtime, stat.st_size)
    assert project_snapshot._file_summary_cache.get(session_key) == result


def test_abfs_cache_miss_waits_and_reads(tmp_path, monkeypatch, clean_session_cache):
    """Cache miss: enqueue returns a key, wait_for_completion returns True, DB read succeeds."""
    import asyncio, hashlib

    path = _make_py_file(tmp_path)
    raw = open(path, "rb").read()
    sha1 = hashlib.sha1(raw).hexdigest()
    filesize = len(raw)
    key = f"file_summary:{sha1}:{filesize}"

    monkeypatch.setattr(
        "app.agent.file_summary_agent.enqueue_file_summary",
        lambda *a, **kw: (key, sha1, filesize),
    )
    # No need to patch wait_for_completion: the real implementation returns True
    # immediately when the key isn't in the registry (our fake enqueue doesn't
    # register an event, so _pending_completions.get(key) is None -> True).

    class FakeSummary:
        summary = "LLM-generated summary."

    import app.database as db_mod
    monkeypatch.setattr(db_mod, "get_file_summary", lambda s, f: FakeSummary())

    from app.agent.project_snapshot import async_build_file_summary
    result = asyncio.run(async_build_file_summary(path, summary_length="short"))

    assert "## Summary" in result
    assert "LLM-generated summary." in result
    assert "== FILE:" in result


def test_abfs_timeout_falls_back_to_structural(tmp_path, monkeypatch, clean_session_cache):
    """If wait_for_completion times out, structural-only result is returned."""
    import asyncio, hashlib

    path = _make_py_file(tmp_path)
    raw = open(path, "rb").read()
    sha1 = hashlib.sha1(raw).hexdigest()
    filesize = len(raw)
    key = f"file_summary:{sha1}:{filesize}"

    monkeypatch.setattr(
        "app.agent.file_summary_agent.enqueue_file_summary",
        lambda *a, **kw: (key, sha1, filesize),
    )
    monkeypatch.setattr(
        "app.agent.scheduler.wait_for_completion",
        lambda k, timeout: False,  # timeout
    )

    from app.agent.project_snapshot import async_build_file_summary
    result = asyncio.run(async_build_file_summary(path, summary_length="short"))

    assert "## Summary" not in result
    assert "== FILE:" in result


def test_abfs_enqueue_error_falls_back_to_structural(tmp_path, monkeypatch, clean_session_cache):
    """If enqueue_file_summary raises, structural-only result is returned."""
    import asyncio

    path = _make_py_file(tmp_path)

    def boom(*a, **kw):
        raise RuntimeError("disk read failed")

    monkeypatch.setattr("app.agent.file_summary_agent.enqueue_file_summary", boom)

    from app.agent.project_snapshot import async_build_file_summary
    result = asyncio.run(async_build_file_summary(path, summary_length="short"))

    assert "## Summary" not in result
    assert "== FILE:" in result


# ---------------------------------------------------------------------------
# Completion registry (scheduler)
# ---------------------------------------------------------------------------

def test_completion_registry_basic():
    """Signal then wait returns True."""
    from app.agent.scheduler import get_or_create_completion_event, signal_completion, wait_for_completion
    import threading

    key = "test:registry:basic"
    ev, created = get_or_create_completion_event(key)
    assert created is True

    # Signal from a thread to simulate scheduler worker
    threading.Timer(0.05, signal_completion, args=(key,)).start()
    result = wait_for_completion(key, timeout=2.0)
    assert result is True


def test_completion_registry_timeout():
    """No signal - wait returns False."""
    from app.agent.scheduler import get_or_create_completion_event, wait_for_completion, signal_completion

    key = "test:registry:timeout"
    _ev, _created = get_or_create_completion_event(key)
    result = wait_for_completion(key, timeout=0.05)
    assert result is False
    # Cleanup
    signal_completion(key)


def test_completion_registry_dedup():
    """Same key twice - same Event, created=False on second call."""
    from app.agent.scheduler import get_or_create_completion_event, signal_completion

    key = "test:registry:dedup"
    ev1, created1 = get_or_create_completion_event(key)
    ev2, created2 = get_or_create_completion_event(key)
    assert created1 is True
    assert created2 is False
    assert ev1 is ev2
    # Cleanup
    signal_completion(key)


# ---------------------------------------------------------------------------
# enqueue_file_summary
# ---------------------------------------------------------------------------

def _make_db_patch(monkeypatch, tmp_path):
    """Redirect database to a temp SQLite file for isolation."""
    import os
    db_path = str(tmp_path / "test_fsj.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)
    # Re-import database with new path
    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(bind=db_mod.engine)
    return db_mod


def test_enqueue_cache_hit(tmp_path, monkeypatch):
    """File already in file_summaries - enqueue returns empty completion_key."""
    db_mod = _make_db_patch(monkeypatch, tmp_path)

    # Write file and pre-populate cache
    p = tmp_path / "cached.py"
    p.write_bytes(b"x = 1\n")
    import hashlib
    sha1 = hashlib.sha1(b"x = 1\n").hexdigest()
    filesize = len(b"x = 1\n")
    db_mod.create_file_summary(sha1, filesize, str(p), "A cached summary")

    from app.agent.file_summary_agent import enqueue_file_summary
    completion_key, ret_sha1, ret_size = enqueue_file_summary(str(p))

    assert completion_key == ""
    assert ret_sha1 == sha1
    assert ret_size == filesize


def test_enqueue_creates_job(tmp_path, monkeypatch):
    """Uncached file - creates a DB job, returns completion_key."""
    db_mod = _make_db_patch(monkeypatch, tmp_path)

    p = tmp_path / "new_file.py"
    p.write_bytes(b"def foo(): pass\n")
    import hashlib
    sha1 = hashlib.sha1(b"def foo(): pass\n").hexdigest()
    filesize = len(b"def foo(): pass\n")

    from app.agent.file_summary_agent import enqueue_file_summary
    from app.agent.scheduler import signal_completion

    # llm_id/budget_id None avoids FK constraint in empty test DB
    completion_key, ret_sha1, _size = enqueue_file_summary(
        str(p), task_id="t1", llm_id=None, budget_id=None
    )

    assert completion_key == f"file_summary:{sha1}:{filesize}"
    assert ret_sha1 == sha1

    # Job should be in DB
    job = db_mod.get_file_summary_job_by_sha1(sha1, filesize)
    assert job is not None
    assert job.status == "pending"

    # Cleanup event
    signal_completion(completion_key)


def test_enqueue_dedup_shared_event(tmp_path, monkeypatch):
    """Two calls for same uncached file - one job, shared completion event."""
    db_mod = _make_db_patch(monkeypatch, tmp_path)

    p = tmp_path / "dup_file.py"
    p.write_bytes(b"class Foo: pass\n")
    import hashlib
    sha1 = hashlib.sha1(b"class Foo: pass\n").hexdigest()
    filesize = len(b"class Foo: pass\n")

    from app.agent.file_summary_agent import enqueue_file_summary
    from app.agent.scheduler import get_or_create_completion_event, signal_completion

    # None avoids FK constraint in empty test DB
    key1, _, _ = enqueue_file_summary(str(p), llm_id=None, budget_id=None)
    key2, _, _ = enqueue_file_summary(str(p), llm_id=None, budget_id=None)

    assert key1 == key2 == f"file_summary:{sha1}:{filesize}"

    # Both calls share the same event object
    ev1, created1 = get_or_create_completion_event(key1)
    assert created1 is False  # already exists

    # Only one job in DB
    jobs = db_mod.get_pending_file_summary_jobs()
    matching = [j for j in jobs if j.sha1_hash == sha1]
    assert len(matching) == 1

    signal_completion(key1)


def test_execute_stores_result(tmp_path, monkeypatch):
    """Mock call_llm - execute_file_summary stores result in file_summaries."""
    import asyncio
    db_mod = _make_db_patch(monkeypatch, tmp_path)

    import hashlib
    content = "def bar(): return 1\n"
    raw = content.encode()
    sha1 = hashlib.sha1(raw).hexdigest()
    filesize = len(raw)

    mock_response = {
        "choices": [{"message": {"content": "Bar returns 1."}}],
        "usage": {"prompt_tokens": 50, "completion_tokens": 10},
    }

    async def mock_call_llm(*args, **kwargs):
        return mock_response

    # Patch call_llm on its home module - the lazy import in execute_file_summary
    # resolves the attribute at call time, so this intercepts it.
    import app.agent.llm_client as lc
    monkeypatch.setattr(lc, "call_llm", mock_call_llm)

    from app.agent.file_summary_agent import execute_file_summary
    result = asyncio.run(execute_file_summary(
        sha1=sha1,
        filesize=filesize,
        file_path="bar.py",
        file_content=content,
    ))

    assert result["prompt_tokens"] == 50
    assert result["completion_tokens"] == 10

    cached = db_mod.get_file_summary(sha1, filesize)
    assert cached is not None
    assert cached.summary == "Bar returns 1."


# ---------------------------------------------------------------------------
# write_file / append_file cache invalidation (Bug #1 fix)
# ---------------------------------------------------------------------------

def test_write_file_invalidates_prepped_cache(tmp_path):
    """After write_file, the next read_file must return fresh content, not ALREADY IN CONTEXT."""
    from app.agent.tools import read_file, write_file, _prepped_files

    # Create a small file (≤25 lines → inlined) and serve it via read_file
    p = tmp_path / "target.txt"
    p.write_text("original content\n", encoding="utf-8")

    first = read_file(str(p))
    assert "original" in first or "ALREADY IN CONTEXT" not in first

    # Now overwrite it
    write_result = write_file(str(p), "updated content\n")
    assert write_result.startswith("OK:")

    # Cache must be cleared — a second read should NOT return ALREADY IN CONTEXT
    second = read_file(str(p))
    assert "ALREADY IN CONTEXT" not in second, (
        "write_file must invalidate the _prepped_files cache so read_file returns fresh content"
    )
    assert "updated" in second


def test_append_file_invalidates_prepped_cache(tmp_path):
    """After append_file, the next read_file must not return ALREADY IN CONTEXT."""
    from app.agent.tools import read_file, append_file

    p = tmp_path / "log.txt"
    p.write_text("line one\n", encoding="utf-8")

    first = read_file(str(p))
    assert "ALREADY IN CONTEXT" not in first

    append_result = append_file(str(p), "line two\n")
    assert append_result.startswith("OK:")

    second = read_file(str(p))
    assert "ALREADY IN CONTEXT" not in second, (
        "append_file must invalidate the _prepped_files cache so read_file returns fresh content"
    )


# ---------------------------------------------------------------------------
# STATUS_TO_TYPE mapping (Bug #2 fix)
# ---------------------------------------------------------------------------

def test_status_to_type_uses_canonical_column_names():
    """ACTIVE→indev and VERIFYING→conceptual_review must be scheduler-dispatchable."""
    from app.agent.config import SCHEDULER_DISPATCHABLE_TYPES

    STATUS_TO_TYPE = {
        "PENDING": "planning",
        "ACTIVE": "indev",
        "VERIFYING": "conceptual_review",
        "ACCEPTED": "completed",
        "REJECTED": "planning",
    }

    assert STATUS_TO_TYPE["ACTIVE"] == "indev"
    assert STATUS_TO_TYPE["VERIFYING"] == "conceptual_review"

    for status, col_type in STATUS_TO_TYPE.items():
        if col_type == "completed":
            continue  # completed is terminal, not dispatchable
        assert col_type in SCHEDULER_DISPATCHABLE_TYPES, (
            f"STATUS_TO_TYPE['{status}'] = '{col_type}' is not in SCHEDULER_DISPATCHABLE_TYPES; "
            "tasks set to this type would become invisible to the scheduler"
        )


def test_update_task_status_maps_to_indev(tmp_path, monkeypatch):
    """update_task_status('ACTIVE') must write type='indev', not 'development'."""
    from unittest.mock import MagicMock, patch

    mock_task = MagicMock()
    mock_task.id = "task-123"
    mock_db = MagicMock()
    mock_db.update_task.return_value = mock_task

    with patch("app.agent.tools._import_db", return_value=mock_db):
        from app.agent.tools import write_task_status
        result = write_task_status("task-123", "ACTIVE")

    mock_db.update_task.assert_called_once_with("task-123", type="indev")
    assert "indev" in result


def test_update_task_status_maps_to_conceptual_review(tmp_path, monkeypatch):
    """update_task_status('VERIFYING') must write type='conceptual_review', not 'review'."""
    from unittest.mock import MagicMock, patch

    mock_task = MagicMock()
    mock_task.id = "task-456"
    mock_db = MagicMock()
    mock_db.update_task.return_value = mock_task

    with patch("app.agent.tools._import_db", return_value=mock_db):
        from app.agent.tools import write_task_status
        result = write_task_status("task-456", "VERIFYING")

    mock_db.update_task.assert_called_once_with("task-456", type="conceptual_review")
    assert "conceptual_review" in result
