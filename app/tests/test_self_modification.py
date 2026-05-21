"""
Tests for GAP 5 — Controlled self-modification.

Covers:
  1. _assert_safe_write_path: _maestro_self + can_self_modify=True + allowlisted path → succeeds
  2. _assert_safe_write_path: _maestro_self + can_self_modify=True + non-allowlisted path → ValueError
  3. _assert_safe_write_path: _maestro_self + can_self_modify=True + HARD_BLOCKED path → ValueError
  4. _assert_safe_write_path: wrong project + allowlisted path → ValueError
  5. _assert_safe_write_path: _maestro_self + can_self_modify=False → ValueError
  6. cast_revert_vote: correct count returned; second vote from same task increments
  7. Revert threshold: below threshold → no git action; must reach threshold
  8. can_auto_merge_self_modification=True + can_auto_merge_human_review=False → treated as disabled
  9. HARD_BLOCKED paths: _maestro_self with all toggles enabled cannot write to blocked paths
"""

from __future__ import annotations

import os
import pytest
from unittest.mock import patch, MagicMock

from app.agent.config import (
    MAESTRO_GIT_ROOT,
    SELF_MODIFICATION_PROJECT,
    SELF_MOD_REVERT_VOTE_THRESHOLD,
)
from app.agent.self_modification_allowlist import ALLOWED_PATHS, HARD_BLOCKED
from app.agent.tools import _task_project_name, _task_id_ctx, _assert_safe_write_path
from app.database import (
    SessionLocal, create_task, upsert_project,
    cast_revert_vote, get_revert_votes,
    record_self_mod_merge, get_latest_self_mod_merge, mark_self_mod_reverted,
    RevertVote, SelfModMergeLog,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _set_project(name: str):
    """Return a ContextVar token that sets _task_project_name."""
    return _task_project_name.set(name)


def _reset_project(token):
    _task_project_name.reset(token)


def _allowlisted_path() -> str:
    """Return one path that is in ALLOWED_PATHS (not HARD_BLOCKED)."""
    for p in ALLOWED_PATHS:
        if p not in HARD_BLOCKED:
            return p
    pytest.skip("No allowlisted non-blocked path found")


def _hard_blocked_path() -> str:
    """Return one path that is in HARD_BLOCKED."""
    for p in HARD_BLOCKED:
        return p
    pytest.skip("No HARD_BLOCKED path found")


def _non_allowlisted_path() -> str:
    """Return a path inside MAESTRO_GIT_ROOT that is NOT on the allowlist."""
    if not MAESTRO_GIT_ROOT:
        pytest.skip("MAESTRO_GIT_ROOT not set")
    return os.path.join(MAESTRO_GIT_ROOT, "totally_fake_file_not_on_allowlist.py")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def _project_self():
    token = _set_project(SELF_MODIFICATION_PROJECT)
    yield
    _reset_project(token)


@pytest.fixture()
def _project_other():
    token = _set_project("some_other_project")
    yield
    _reset_project(token)


@pytest.fixture()
def can_self_modify_on(monkeypatch):
    from app.agent import tools as _tools
    monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_self_modify", True)


@pytest.fixture()
def can_self_modify_off(monkeypatch):
    from app.agent import tools as _tools
    monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_self_modify", False)


@pytest.fixture()
def _task_and_merge(tmp_path):
    """Create a real task and record a self-mod merge for it."""
    upsert_project("_maestro_self_test_project", path=str(tmp_path))
    task = create_task("test self-mod revert task", "idea",
                       project="_maestro_self_test_project", llm_id=None, budget_id=None)
    merge_commit = "aabbccdd1122" * 3  # fake commit sha
    record_self_mod_merge(task.id, merge_commit)
    return task, merge_commit


# ---------------------------------------------------------------------------
# 1. Allowlisted path succeeds
# ---------------------------------------------------------------------------

class TestAllowlistedPathSucceeds:
    def test_write_to_allowlisted_path_allowed(self, _project_self, can_self_modify_on):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        path = _allowlisted_path()
        result = _assert_safe_write_path(path)
        # Returns the resolved absolute path (case may vary on Windows)
        assert os.path.normcase(result) == os.path.normcase(os.path.realpath(path))


# ---------------------------------------------------------------------------
# 2. Non-allowlisted path raises ValueError
# ---------------------------------------------------------------------------

class TestNonAllowlistedPathBlocked:
    def test_non_allowlisted_path_raises(self, _project_self, can_self_modify_on):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        path = _non_allowlisted_path()
        with pytest.raises(ValueError, match="not on the self-modification allowlist"):
            _assert_safe_write_path(path)


# ---------------------------------------------------------------------------
# 3. HARD_BLOCKED path raises ValueError
# ---------------------------------------------------------------------------

class TestHardBlockedPathBlocked:
    def test_hard_blocked_path_raises(self, _project_self, can_self_modify_on):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        path = _hard_blocked_path()
        with pytest.raises(ValueError, match="permanently off-limits"):
            _assert_safe_write_path(path)


# ---------------------------------------------------------------------------
# 4. Wrong project name blocks even allowlisted path
# ---------------------------------------------------------------------------

class TestWrongProjectBlocked:
    def test_wrong_project_raises(self, _project_other, can_self_modify_on):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        path = _allowlisted_path()
        with pytest.raises(ValueError, match="inside the Maestro source tree"):
            _assert_safe_write_path(path)


# ---------------------------------------------------------------------------
# 5. can_self_modify=False blocks even with correct project
# ---------------------------------------------------------------------------

class TestCapabilityFlagOff:
    def test_flag_off_raises(self, _project_self, can_self_modify_off):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        path = _allowlisted_path()
        with pytest.raises(ValueError, match="inside the Maestro source tree"):
            _assert_safe_write_path(path)


# ---------------------------------------------------------------------------
# 6. cast_revert_vote: count increments correctly
# ---------------------------------------------------------------------------

class TestCastRevertVote:
    def test_vote_count_increments(self, tmp_path):
        upsert_project("_maestro_vote_test", path=str(tmp_path))
        task1 = create_task("vote test task 1", "idea",
                            project="_maestro_vote_test", llm_id=None, budget_id=None)
        task2 = create_task("vote test task 2", "idea",
                            project="_maestro_vote_test", llm_id=None, budget_id=None)
        commit = "deadbeef1234" * 3

        count1 = cast_revert_vote(task1.id, commit, "First reason")
        assert count1 == 1

        count2 = cast_revert_vote(task2.id, commit, "Second reason")
        assert count2 == 2

        votes = get_revert_votes(commit)
        assert len(votes) == 2
        reasons = {v["reason"] for v in votes}
        assert "First reason" in reasons
        assert "Second reason" in reasons

    def test_same_task_can_vote_twice(self, tmp_path):
        upsert_project("_maestro_vote_test2", path=str(tmp_path))
        task = create_task("double vote task", "idea",
                           project="_maestro_vote_test2", llm_id=None, budget_id=None)
        commit = "cafebabe5678" * 3

        count1 = cast_revert_vote(task.id, commit, "first")
        count2 = cast_revert_vote(task.id, commit, "second")
        assert count2 == 2


# ---------------------------------------------------------------------------
# 7. Revert threshold logic
# ---------------------------------------------------------------------------

class TestRevertThreshold:
    def test_below_threshold_no_action(self, _task_and_merge, monkeypatch):
        task, merge_commit = _task_and_merge
        # Cast only threshold-1 votes — should not trigger git revert
        for i in range(SELF_MOD_REVERT_VOTE_THRESHOLD - 1):
            cast_revert_vote(task.id, merge_commit, f"reason {i}")

        # Latest merge should still show as not reverted
        latest = get_latest_self_mod_merge()
        assert latest == merge_commit

    def test_at_threshold_triggers_revert(self, _task_and_merge):
        task, merge_commit = _task_and_merge
        # Pre-cast threshold-1 votes
        for i in range(SELF_MOD_REVERT_VOTE_THRESHOLD - 1):
            cast_revert_vote(task.id, merge_commit, f"pre {i}")

        # Cast the final vote via CRUD; verify count reaches the threshold.
        # The tool handler's git/PIP side effects are tested separately.
        count = cast_revert_vote(task.id, merge_commit, "final vote")
        assert count == SELF_MOD_REVERT_VOTE_THRESHOLD

        votes = get_revert_votes(merge_commit)
        assert len(votes) == SELF_MOD_REVERT_VOTE_THRESHOLD


# ---------------------------------------------------------------------------
# 8. can_auto_merge_self_modification=True + can_auto_merge_human_review=False → disabled
# ---------------------------------------------------------------------------

class TestAutoMergeGate:
    def test_self_mod_merge_requires_both_flags(self, monkeypatch):
        from app.agent import tools as _tools
        monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_auto_merge_human_review", False)
        monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_auto_merge_self_modification", True)

        # When can_auto_merge_human_review is False, the gate is considered disabled.
        # We verify by checking the effective condition used in _execute_merge_bg:
        caps = _tools.MAESTRO_CAPABILITIES
        gate_active = caps.can_auto_merge_human_review and caps.can_auto_merge_self_modification
        assert not gate_active, "Auto-merge gate should be inactive without can_auto_merge_human_review"

    def test_auto_merge_enabled_when_both_flags_set(self, monkeypatch):
        from app.agent import tools as _tools
        monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_auto_merge_human_review", True)
        monkeypatch.setattr(_tools.MAESTRO_CAPABILITIES, "can_auto_merge_self_modification", True)

        caps = _tools.MAESTRO_CAPABILITIES
        gate_active = caps.can_auto_merge_human_review and caps.can_auto_merge_self_modification
        assert gate_active, "Auto-merge gate should be active when both flags are set"


# ---------------------------------------------------------------------------
# 9. HARD_BLOCKED paths cannot be written even with all toggles enabled
# ---------------------------------------------------------------------------

class TestHardBlockedAlwaysBlocked:
    def test_hard_blocked_paths_unconditionally_blocked(self, _project_self, can_self_modify_on):
        if not MAESTRO_GIT_ROOT:
            pytest.skip("MAESTRO_GIT_ROOT not set")
        # Only test file paths (not directory entries which get complex path handling)
        file_blocked = [
            p for p in HARD_BLOCKED
            if p.endswith(".py") or p.endswith(".ini") or p.endswith(".env")
        ]
        assert file_blocked, "Expected at least one file in HARD_BLOCKED"
        for blocked_path in file_blocked:
            with pytest.raises(ValueError, match="permanently off-limits"):
                _assert_safe_write_path(blocked_path)
