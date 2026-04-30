"""
Tests for app/agent/project_snapshot.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import time
import textwrap
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_temp_project(files: dict[str, str]) -> str:
    """Create a temp directory with the given files. Returns the dir path."""
    d = tempfile.mkdtemp()
    for rel, content in files.items():
        full = os.path.join(d, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        Path(full).write_text(content, encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# build_project_snapshot
# ---------------------------------------------------------------------------

def test_build_project_snapshot_basic():
    from app.agent.project_snapshot import build_project_snapshot, clear_snapshot_cache
    clear_snapshot_cache()

    d = _make_temp_project({
        "app/__init__.py": "",
        "app/main.py": "def hello(): pass\n",
        "app/sub/util.py": "x = 1\n",
        "README.md": "# Test",
    })
    result = build_project_snapshot(project_root=d)

    assert "== PROJECT STRUCTURE ==" in result
    assert "app/" in result
    assert "main.py" in result
    assert "README.md" in result


def test_snapshot_caching():
    from app.agent.project_snapshot import build_project_snapshot, clear_snapshot_cache
    clear_snapshot_cache()

    d = _make_temp_project({"app/main.py": "def foo(): pass\n"})

    first = build_project_snapshot(project_root=d)
    second = build_project_snapshot(project_root=d)
    assert first == second  # Returned from cache

    clear_snapshot_cache()
    third = build_project_snapshot(project_root=d)
    # Should still produce same result (same files)
    assert "== PROJECT STRUCTURE ==" in third


def test_snapshot_token_budget():
    from app.agent.project_snapshot import build_project_snapshot, clear_snapshot_cache
    clear_snapshot_cache()

    # Create a project with many files to trigger budget logic
    files = {f"app/module_{i}.py": f"def f{i}(): pass\n" for i in range(50)}
    d = _make_temp_project(files)

    result = build_project_snapshot(project_root=d, max_depth=3)
    # Estimated tokens should not far exceed the limit (1500 tokens = ~6000 chars)
    # The snapshot may truncate with the ellipsis message
    assert len(result) < 50_000  # sanity upper bound


def test_snapshot_treesitter_fallback():
    """Snapshot still works if tree-sitter is unavailable."""
    from app.agent.project_snapshot import build_project_snapshot, clear_snapshot_cache
    clear_snapshot_cache()

    d = _make_temp_project({"app/main.py": "def foo(): pass\n"})

    with patch.dict(sys.modules, {"app.agent.static_analysis": None}):
        result = build_project_snapshot(project_root=d)

    assert "== PROJECT STRUCTURE ==" in result
    assert "main.py" in result


def test_snapshot_excludes_hidden_and_venv():
    from app.agent.project_snapshot import build_project_snapshot, clear_snapshot_cache
    clear_snapshot_cache()

    d = _make_temp_project({
        "app/main.py": "x = 1\n",
        "venv/lib/site.py": "# should be excluded",
        ".git/config": "[core]",
    })

    result = build_project_snapshot(project_root=d)
    assert "venv/" not in result
    assert ".git/" not in result


# ---------------------------------------------------------------------------
# build_file_summary
# ---------------------------------------------------------------------------

def test_build_file_summary_python():
    from app.agent.project_snapshot import build_file_summary

    d = _make_temp_project({
        "mymodule.py": textwrap.dedent("""\
            import os
            import sys

            GLOBAL_VAR = 42

            class MyClass:
                def method_a(self):
                    pass

                def method_b(self):
                    pass

            def standalone_func(x, y):
                return x + y
        """),
    })

    path = os.path.join(d, "mymodule.py")
    result = build_file_summary(path)

    assert "== FILE:" in result
    assert "MyClass" in result
    assert "standalone_func" in result
    assert "read_file" in result  # hint at bottom


def test_build_file_summary_nonpython():
    from app.agent.project_snapshot import build_file_summary

    d = _make_temp_project({"notes.txt": "hello world\n" * 10})
    path = os.path.join(d, "notes.txt")
    result = build_file_summary(path)

    assert "== FILE:" in result
    assert "notes.txt" in result
    assert "read_file" in result


def test_build_file_summary_missing():
    from app.agent.project_snapshot import build_file_summary

    result = build_file_summary("/nonexistent/path/file.py")
    assert result.startswith("ERROR:")


def test_build_file_summary_cached():
    from app.agent.project_snapshot import build_file_summary, clear_snapshot_cache
    clear_snapshot_cache()

    d = _make_temp_project({"mod.py": "def f(): pass\n"})
    path = os.path.join(d, "mod.py")

    first = build_file_summary(path)
    second = build_file_summary(path)
    assert first == second
