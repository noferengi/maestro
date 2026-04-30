"""
Tests for safety mechanisms in app/agent/tools.py.

Covers: path containment, shell blocklist, dispatch_tool error handling,
archive_file behaviour, and git_checkout allowlist.

Uses tmp_path for real filesystem ops; patches _task_git_cwd ContextVar
where needed.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.tools import (
    _assert_safe_path,
    _assert_safe_write_path,
    dispatch_tool,
    write_archive,
    write_git_checkout,
    set_task_git_cwd,
    _task_git_cwd,
)
from app.agent.config import PROJECT_ROOT


# ---------------------------------------------------------------------------
# Path safety — reads (_assert_safe_path) are now globally permissive;
#               writes (_assert_safe_write_path) are restricted to project root.
# ---------------------------------------------------------------------------

class TestAssertSafePath:
    def test_accepts_path_inside_project_root(self):
        """A path inside PROJECT_ROOT is accepted and its resolved form returned."""
        result = _assert_safe_path(PROJECT_ROOT)
        assert os.path.isabs(result)

    def test_read_allows_paths_outside_project_root(self):
        """_assert_safe_path (reads) now allows global navigation — no ValueError for outside paths."""
        # Reads are unrestricted except for .git internals and .archive.
        # A path outside PROJECT_ROOT should resolve without raising.
        outside = os.path.dirname(PROJECT_ROOT)  # parent dir — valid filesystem path
        result = _assert_safe_path(outside)
        assert os.path.isabs(result)

    def test_read_still_blocks_git_internals(self):
        """_assert_safe_path blocks .git directory access regardless of location."""
        git_path = os.path.join(PROJECT_ROOT, ".git", "config")
        with pytest.raises(ValueError, match="git"):
            _assert_safe_path(git_path)

    def test_contextvar_override_accepts_inside(self, tmp_path):
        """When _task_git_cwd is set, paths inside that dir are accepted for reads."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            inner = str(tmp_path / "subdir")
            os.makedirs(inner, exist_ok=True)
            result = _assert_safe_path(inner)
            assert result.startswith(str(tmp_path))
        finally:
            _task_git_cwd.reset(token)

    def test_contextvar_override_read_allows_outside(self, tmp_path):
        """_assert_safe_path blocks reads outside _task_git_cwd (RC4 strict isolation)."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            # RC4: Strict Isolation. When a task-specific root is set,
            # reading outside it is blocked.
            with pytest.raises(ValueError, match="Strict Isolation violation"):
                _assert_safe_path(PROJECT_ROOT)
        finally:
            _task_git_cwd.reset(token)


class TestAssertSafeWritePath:
    def test_accepts_path_inside_project_root(self, tmp_path):
        """A path inside the effective root is accepted."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            inner = str(tmp_path / "src" / "main.py")
            result = _assert_safe_write_path(inner)
            assert result.startswith(os.path.realpath(str(tmp_path)))
        finally:
            _task_git_cwd.reset(token)

    def test_write_rejects_outside_project_root(self, tmp_path):
        """Writes outside the project root are rejected."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            outside = os.path.join(PROJECT_ROOT, "some_file.py")
            with pytest.raises(ValueError, match="outside"):
                _assert_safe_write_path(outside)
        finally:
            _task_git_cwd.reset(token)

    def test_write_rejects_venv_segment(self, tmp_path):
        """Writes into venv/ are rejected even when inside the project root."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            venv_path = str(tmp_path / "venv" / "lib" / "site.py")
            with pytest.raises(ValueError, match="venv"):
                _assert_safe_write_path(venv_path)
        finally:
            _task_git_cwd.reset(token)

    def test_write_rejects_git_internals(self, tmp_path):
        """Writes to .git are rejected (inherited from _assert_safe_path)."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            git_path = str(tmp_path / ".git" / "config")
            with pytest.raises(ValueError):
                _assert_safe_write_path(git_path)
        finally:
            _task_git_cwd.reset(token)


# ---------------------------------------------------------------------------
# dispatch_tool
# ---------------------------------------------------------------------------

class TestDispatchTool:
    def test_unknown_tool_returns_error_string(self):
        result = dispatch_tool("nonexistent_tool_xyz", {})
        assert "Unknown tool" in result
        assert isinstance(result, str)

    def test_bad_args_returns_error_string(self):
        """Calling read_file with wrong kwarg should return an error, not raise."""
        result = dispatch_tool("read_file", {"wrong_arg": "value"})
        assert "ERROR" in result.upper()
        assert isinstance(result, str)

    def test_no_exception_escapes(self):
        """dispatch_tool must always return a string, never raise."""
        result = dispatch_tool("read_file", {"path": "/nonexistent/path/xyz/abc.txt"})
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# archive_file
# ---------------------------------------------------------------------------

class TestArchiveFile:
    def test_archive_creates_archive_structure(self, tmp_path, monkeypatch):
        """archive_file moves the file into .archive/<timestamp>/<rel_path>."""
        # Point the effective root at tmp_path
        monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR",
                            str(tmp_path / ".archive"))

        # Create a file to archive
        target = tmp_path / "to_archive.txt"
        target.write_text("content")

        result = write_archive(str(target))

        assert "OK" in result
        assert not target.exists()  # original gone

    def test_archive_file_not_found_returns_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR",
                            str(tmp_path / ".archive"))

        result = write_archive(str(tmp_path / "does_not_exist.txt"))
        assert "ERROR" in result or "does not exist" in result


# ---------------------------------------------------------------------------
# git_checkout allowlist
# ---------------------------------------------------------------------------

class TestGitCheckoutAllowlist:
    def test_main_branch_blocked(self, tmp_path):
        """'main' is no longer in the allowlist — only maestro/task-* is permitted."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = write_git_checkout("main")
            assert "not permitted" in result.lower() or "ERROR" in result
        finally:
            _task_git_cwd.reset(token)

    def test_maestro_task_branch_allowed(self, tmp_path):
        """maestro/task-42 is permitted by the prefix rule."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = write_git_checkout("maestro/task-42")
            assert "not permitted" not in result.lower()
        finally:
            _task_git_cwd.reset(token)

    def test_disallowed_branch_returns_error(self, tmp_path):
        """feature/foo is not in the allowlist and should be blocked."""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            result = write_git_checkout("feature/foo")
            assert "not permitted" in result.lower() or "ERROR" in result
        finally:
            _task_git_cwd.reset(token)
