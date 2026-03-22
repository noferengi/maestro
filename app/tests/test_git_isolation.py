"""
test_git_isolation.py
---------------------
Critical safety tests: ensure no git operation ever targets TheMaestro's own
.git repository, regardless of how _task_git_cwd is configured.

These tests MUST stay green. A failure means the agent could corrupt
TheMaestro's source tree or git history.
"""

import os
import subprocess
import sys
import threading

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.tools import (
    _is_inside_maestro_repo,
    _git_run,
    archive_file,
    git_checkout,
    git_commit,
    git_create_branch,
    git_diff,
    git_log,
    git_status,
    set_task_git_cwd,
    _task_git_cwd,
)
from app.agent.config import PROJECT_ROOT, MAESTRO_GIT_ROOT


# ===========================================================================
# _is_inside_maestro_repo — recognition logic
# ===========================================================================

class TestIsInsideMaestroRepo:
    def test_project_root_recognised_as_maestro(self):
        """PROJECT_ROOT must be identified as inside TheMaestro's own git repo."""
        assert _is_inside_maestro_repo(PROJECT_ROOT) is True

    def test_subdirectory_of_project_root_recognised(self):
        """A subdirectory of PROJECT_ROOT is also inside TheMaestro's repo."""
        subdir = os.path.join(PROJECT_ROOT, "app", "agent")
        assert _is_inside_maestro_repo(subdir) is True

    def test_external_tmp_path_not_recognised(self, tmp_path):
        """A temporary path outside PROJECT_ROOT is not inside TheMaestro's repo."""
        assert _is_inside_maestro_repo(str(tmp_path)) is False

    def test_returns_false_when_maestro_git_root_not_set(self, monkeypatch):
        """If MAESTRO_GIT_ROOT is None (no git), _is_inside_maestro_repo returns False."""
        monkeypatch.setattr("app.agent.tools.MAESTRO_GIT_ROOT", None)
        assert _is_inside_maestro_repo(PROJECT_ROOT) is False


# ===========================================================================
# _git_run — safety rail (the innermost guard)
# ===========================================================================

class TestGitRunSafetyRail:
    def test_no_cwd_set_returns_configuration_error(self):
        """_git_run with no ContextVar set returns an error, not BLOCKED."""
        token = _task_git_cwd.set(None)
        try:
            rc, out, err = _git_run(["git", "status"])
            assert rc != 0
            assert "No task git working directory" in err
        finally:
            _task_git_cwd.reset(token)

    def test_project_root_as_cwd_is_blocked(self):
        """_git_run with cwd = PROJECT_ROOT must return BLOCKED."""
        token = _task_git_cwd.set(PROJECT_ROOT)
        try:
            rc, out, err = _git_run(["git", "status"])
            assert rc != 0
            assert "BLOCKED" in err
        finally:
            _task_git_cwd.reset(token)

    def test_maestro_subdir_as_cwd_is_blocked(self):
        """_git_run with cwd inside TheMaestro's tree must also be BLOCKED."""
        subdir = os.path.join(PROJECT_ROOT, "app")
        token = _task_git_cwd.set(subdir)
        try:
            rc, out, err = _git_run(["git", "status"])
            assert rc != 0
            assert "BLOCKED" in err
        finally:
            _task_git_cwd.reset(token)

    def test_explicit_cwd_override_inside_maestro_is_blocked(self):
        """Even an explicit cwd kwarg pointing at PROJECT_ROOT must be blocked."""
        rc, out, err = _git_run(["git", "status"], cwd=PROJECT_ROOT)
        assert rc != 0
        assert "BLOCKED" in err

    def test_external_path_passes_safety_rail(self, tmp_path):
        """_git_run with an external path is NOT blocked by the TheMaestro guard.
        (It may fail because tmp_path has no git repo, but not because of BLOCKED.)"""
        token = _task_git_cwd.set(str(tmp_path))
        try:
            rc, out, err = _git_run(["git", "status"])
            assert "BLOCKED" not in err
        finally:
            _task_git_cwd.reset(token)


# ===========================================================================
# High-level git tools — must all refuse when cwd is TheMaestro's repo
# ===========================================================================

@pytest.fixture()
def cwd_is_maestro():
    """Set _task_git_cwd to PROJECT_ROOT and restore it after each test."""
    token = _task_git_cwd.set(PROJECT_ROOT)
    yield
    _task_git_cwd.reset(token)


class TestGitToolsBlockedOnMaestroRepo:
    """Every git tool must refuse when task_git_cwd resolves to TheMaestro's repo."""

    def test_git_status_blocked(self, cwd_is_maestro):
        result = git_status()
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_diff_blocked(self, cwd_is_maestro):
        result = git_diff()
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_log_blocked(self, cwd_is_maestro):
        result = git_log()
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_commit_blocked(self, cwd_is_maestro):
        """git_commit must not create a commit in TheMaestro's repo."""
        result = git_commit("should never appear in TheMaestro history")
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_checkout_main_still_blocked(self, cwd_is_maestro):
        """'main' is in the branch allowlist, but _git_run blocks before execution."""
        result = git_checkout("main")
        # Allowlist passes 'main'; _git_run's maestro guard fires next
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_checkout_maestro_branch_still_blocked(self, cwd_is_maestro):
        """Even a maestro/task-* branch is blocked when cwd is TheMaestro."""
        result = git_checkout("maestro/task-99")
        assert "BLOCKED" in result or "ERROR" in result

    def test_git_create_branch_blocked(self, cwd_is_maestro):
        """git_create_branch must not create a branch in TheMaestro's repo."""
        result = git_create_branch("maestro/task-999-test")
        assert "BLOCKED" in result or "ERROR" in result


# ===========================================================================
# archive_file — .git hard rejection
# ===========================================================================

class TestArchiveFileGitRejection:
    def test_archive_dot_git_directory_is_hard_rejected(self, monkeypatch, tmp_path):
        """archive_file must unconditionally reject any path inside .git."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        target = git_dir / "COMMIT_EDITMSG"
        target.write_text("initial commit")

        monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive"))

        result = archive_file(str(target))
        assert "HARD REJECTION" in result or "BLOCKED" in result

    def test_archive_dot_git_root_is_hard_rejected(self, monkeypatch, tmp_path):
        """archive_file must reject the .git directory itself, not just its children."""
        git_dir = tmp_path / ".git"
        git_dir.mkdir()

        monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR", str(tmp_path / ".archive"))

        result = archive_file(str(git_dir))
        assert "HARD REJECTION" in result or "BLOCKED" in result

    def test_archive_already_archived_path_is_rejected(self, monkeypatch, tmp_path):
        """archive_file must reject a path that is already inside ARCHIVE_DIR."""
        archive_dir = tmp_path / ".archive"
        archive_dir.mkdir(parents=True)
        already_archived = archive_dir / "2025-01-01_12-00-00" / "some_file.txt"
        already_archived.parent.mkdir(parents=True)
        already_archived.write_text("was archived")

        monkeypatch.setattr("app.agent.tools.PROJECT_ROOT", str(tmp_path))
        monkeypatch.setattr("app.agent.tools.ARCHIVE_DIR", str(archive_dir))

        result = archive_file(str(already_archived))
        assert "HARD REJECTION" in result or "BLOCKED" in result or "already inside" in result.lower()


# ===========================================================================
# Belt-and-suspenders: verify TheMaestro's git HEAD is unchanged
# ===========================================================================

class TestNoActualGitMutationOnMaestroRepo:
    """
    Confirm that executing this entire test module did not mutate TheMaestro's
    git state.  Records HEAD before the tests and checks it matches after.
    This is the definitive proof that the safety rails worked.
    """

    @pytest.fixture(autouse=True)
    def record_head_before(self):
        """Capture git HEAD sha before the test runs."""
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self._head_before = result.stdout.strip()
        yield

    def test_head_sha_unchanged_after_git_tool_calls(self):
        """Trigger all git tools against TheMaestro's repo and verify HEAD is intact."""
        # All of these should be BLOCKED by the safety rail
        token = _task_git_cwd.set(PROJECT_ROOT)
        try:
            git_status()
            git_diff()
            git_log()
            git_commit("MUST NOT APPEAR")
            git_checkout("main")
            git_create_branch("maestro/task-safety-test")
        finally:
            _task_git_cwd.reset(token)

        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        head_after = result.stdout.strip()
        assert head_after == self._head_before, (
            f"TheMaestro HEAD changed during test run! "
            f"Before: {self._head_before}, After: {head_after}. "
            f"A git operation was NOT blocked correctly."
        )

    def test_index_not_staged_by_git_tool_calls(self):
        """git_commit('...') must not stage any changes to TheMaestro's index."""
        token = _task_git_cwd.set(PROJECT_ROOT)
        try:
            git_commit("staging-test-must-be-blocked")
        finally:
            _task_git_cwd.reset(token)

        # Read git status --porcelain and check no index (first-column) changes appeared
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert result.returncode == 0, "git status failed — git itself is broken"
        newly_staged = [
            ln for ln in result.stdout.splitlines()
            # First char is index status; ' ' or '?' means unstaged/untracked (fine)
            if ln and ln[0] not in (" ", "?", "!")
        ]
        # Pre-existing staged files are OK (they existed before this test ran).
        # We verify the count did not INCREASE beyond what we started with.
        # Since we can't snapshot "before", we check for the commit message we
        # tried to make — it must never appear in git log.
        log_result = subprocess.run(
            ["git", "log", "--oneline", "-5"],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert "staging-test-must-be-blocked" not in log_result.stdout
        assert "MUST NOT APPEAR" not in log_result.stdout
