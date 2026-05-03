"""
Tests for the tiered file reading redesign:
  - read_file() returns structural summary (first call) or source lines (subsequent or targeted)
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
    assert "Use read_file" in result


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
# read_file range logic
# ---------------------------------------------------------------------------

def test_read_file_with_prep_using_end(tmp_path):
    from app.agent.tools import read_file

    # Use a large file (>25 lines) so read_file() returns structural summary,
    # not inline content - lines are NOT pre-served on the first call.
    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)  # prep step - structural summary only, no lines served
    result = read_file(path, start=2, end=4)

    assert "2: line2" in result
    assert "3: line3" in result
    assert "4: line4" in result
    assert "line1" not in result
    assert "line5" not in result


def test_read_file_start_count(tmp_path):
    from app.agent.tools import read_file

    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)
    result = read_file(path, start=2, count=3)

    assert "2: line2" in result
    assert "3: line3" in result
    assert "4: line4" in result
    assert "line5" not in result


def test_read_file_no_args_serves_next_chunk(tmp_path):
    """read_file(path) with no start/end on a prepped file serves the next unserved chunk."""
    from app.agent.tools import read_file

    content = "\n".join(f"line{i}" for i in range(1, 40)) + "\n"
    path = _make_file(tmp_path, content, name="data.txt")

    read_file(path)  # first call: structural summary only
    result = read_file(path)  # second call: no args -> serve lines 1..N

    assert "1: line1" in result
    assert "ERROR" not in result


def test_read_file_start_only_serves_250_from_start(tmp_path):
    """Providing only start (no end/count) serves up to 250 lines from that start."""
    from app.agent.tools import read_file

    content = "\n".join(f"x{i}" for i in range(1, 400)) + "\n"
    path = _make_file(tmp_path, content, name="big.txt")

    read_file(path)
    result = read_file(path, start=10)

    assert "10: x10" in result
    content_lines = [l for l in result.splitlines() if l and not l.startswith("==")]
    assert len(content_lines) <= 250
    assert "ERROR" not in result


def test_read_file_rejects_both_end_and_count(tmp_path):
    from app.agent.tools import read_file

    path = _make_file(tmp_path, "a\nb\n", name="data.txt")
    read_file(path)

    result = read_file(path, start=1, end=2, count=2)
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
# 250-line cap on read_file
# ---------------------------------------------------------------------------

def _make_nline_file(tmp_path, n: int) -> str:
    content = "\n".join(f"line{i}" for i in range(1, n + 1)) + "\n"
    return _make_file(tmp_path, content, name=f"big_{n}.txt")


def _content_lines(result: str) -> list[str]:
    """Strip the == FILE == header line(s) from _serve_file_lines output."""
    return [l for l in result.strip().splitlines() if l and not l.startswith("==")]


def test_250_line_cap_with_end(tmp_path):
    """Requesting more than 250 lines via 'end' is silently clamped."""
    from app.agent.tools import read_file

    path = _make_nline_file(tmp_path, 400)
    read_file(path)

    result = read_file(path, start=1, end=400)
    returned_lines = _content_lines(result)
    assert len(returned_lines) == 250
    assert returned_lines[0].startswith("1:")
    assert returned_lines[-1].startswith("250:")


def test_250_line_cap_with_count(tmp_path):
    """Requesting more than 250 lines via 'count' is silently clamped."""
    from app.agent.tools import read_file

    path = _make_nline_file(tmp_path, 400)
    read_file(path)

    result = read_file(path, start=1, count=300)
    returned_lines = _content_lines(result)
    assert len(returned_lines) == 250


def test_sequential_calls_independent(tmp_path):
    """Two sequential read_file calls return their own ranges, no merging."""
    from app.agent.tools import read_file

    path = _make_nline_file(tmp_path, 600)
    read_file(path)

    first = read_file(path, start=1, count=250)
    second = read_file(path, start=251, count=250)

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
