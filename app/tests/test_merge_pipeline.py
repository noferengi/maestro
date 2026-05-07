"""
Tests for the merge pipeline (app/agent/merge.py).

All git and subprocess calls are mocked - no real git operations.
"""

import os
import sys
import pytest
from unittest.mock import patch, MagicMock, call

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


def _make_run_result(returncode, stdout="", stderr=""):
    m = MagicMock()
    m.returncode = returncode
    m.stdout = stdout
    m.stderr = stderr
    return m


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TASK_ID = "test-merge-task-001"
_BRANCH = f"maestro/task-{_TASK_ID}"


def _git_sequence(*responses):
    """Build a side_effect list for subprocess.run, one response per git call."""
    return [_make_run_result(rc, out) for rc, out in responses]


class TestMergeSuccess:
    def test_successful_merge_returns_merged_status(self):
        """Happy path: branch exists, merge succeeds, tests pass, push succeeds."""
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.update_task") as mock_update, \
             patch("app.database.create_merge_record"):
            # Sequence: branch_list, _get_base_branch(main), checkout main, pull,
            #           merge, rev-parse, pytest, push, tag
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),     # branch --list (task branch exists)
                _make_run_result(0, "  main"),    # branch --list main (_get_base_branch)
                _make_run_result(0),               # checkout main
                _make_run_result(0),               # pull
                _make_run_result(0),               # merge --no-ff
                _make_run_result(0, "abc123"),     # rev-parse HEAD
                _make_run_result(0),               # pytest
                _make_run_result(0),               # push
                _make_run_result(0),               # tag
            ]
            result = execute_merge(_TASK_ID)
        assert result.status == "merged"

    def test_task_marked_completed_on_success(self):
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.update_task") as mock_update, \
             patch("app.database.create_merge_record"):
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),
                _make_run_result(0, "  main"),    # _get_base_branch
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(0, "abc123"),
                _make_run_result(0),   # pytest
                _make_run_result(0),   # push
                _make_run_result(0),   # tag
            ]
            execute_merge(_TASK_ID)
        mock_update.assert_called_with(_TASK_ID, type="completed")

    def test_push_skipped_when_auto_push_false(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_AUTO_PUSH
        try:
            merge_module.MERGE_AUTO_PUSH = False
            with patch("subprocess.run") as mock_run, \
                 patch("app.database.update_task"), \
                 patch("app.database.create_merge_record"):
                mock_run.side_effect = [
                    _make_run_result(0, _BRANCH),
                    _make_run_result(0, "  main"),  # _get_base_branch
                    _make_run_result(0),
                    _make_run_result(0),
                    _make_run_result(0),
                    _make_run_result(0, "abc123"),
                    _make_run_result(0),   # pytest
                    _make_run_result(0),   # tag
                ]
                result = execute_merge(_TASK_ID)
            assert result.status == "merged"
        finally:
            merge_module.MERGE_AUTO_PUSH = original


class TestMergeConflict:
    def test_conflict_status_returned(self):
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.create_merge_record"):
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),          # branch --list (task branch)
                _make_run_result(0, "  main"),          # _get_base_branch
                _make_run_result(0),                    # checkout main
                _make_run_result(0),                    # pull
                _make_run_result(1, "", "conflict"),    # merge fails
                _make_run_result(0, ""),                # diff --name-only (no files listed)
                _make_run_result(0),                    # merge --abort
            ]
            result = execute_merge(_TASK_ID)
        assert result.status == "conflict"

    def test_task_NOT_marked_completed_on_conflict(self):
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.update_task") as mock_update, \
             patch("app.database.create_merge_record"):
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),
                _make_run_result(0, "  main"),          # _get_base_branch
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(1, "", "conflict"),
                _make_run_result(0, ""),                # diff --name-only
                _make_run_result(0),
            ]
            execute_merge(_TASK_ID)
        # update_task should NOT have been called with completed
        for c in mock_update.call_args_list:
            assert c != call(_TASK_ID, type="completed")


class TestTestFailure:
    def test_test_failure_status_returned(self):
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.create_merge_record"):
            # Order: branch, _get_base_branch, checkout, pull, merge, rev-parse,
            #        pytest(fails), reset
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),
                _make_run_result(0, "  main"),    # _get_base_branch
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(0),              # merge succeeds
                _make_run_result(0, "abc123"),    # rev-parse
                _make_run_result(1, "FAILED"),    # pytest fails
                _make_run_result(0),              # reset HEAD~1
            ]
            result = execute_merge(_TASK_ID)
        assert result.status == "test_failure"

    def test_task_NOT_marked_completed_on_test_failure(self):
        from app.agent.merge import execute_merge

        with patch("subprocess.run") as mock_run, \
             patch("app.database.update_task") as mock_update, \
             patch("app.database.create_merge_record"):
            mock_run.side_effect = [
                _make_run_result(0, _BRANCH),
                _make_run_result(0, "  main"),    # _get_base_branch
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(0),
                _make_run_result(0, "abc123"),
                _make_run_result(1, "FAILED"),    # pytest fails
                _make_run_result(0),              # reset HEAD~1
            ]
            execute_merge(_TASK_ID)
        for c in mock_update.call_args_list:
            assert c != call(_TASK_ID, type="completed")


class TestPushFailure:
    def _merge_then_push_fail(self, mock_run, push_rc=1, push_attempts=3):
        """Build side_effect for: branch exists, _get_base_branch, checkout, pull, merge, rev-parse, pytest(passes), [push failures]."""
        responses = [
            _make_run_result(0, _BRANCH),   # branch --list (task branch)
            _make_run_result(0, "  main"),  # _get_base_branch
            _make_run_result(0),             # checkout main
            _make_run_result(0),             # pull
            _make_run_result(0),             # merge --no-ff
            _make_run_result(0, "abc123"),   # rev-parse HEAD
            _make_run_result(0),             # pytest passes
        ]
        # All push attempts fail
        for _ in range(push_attempts):
            responses.append(_make_run_result(push_rc, "", "push failed"))
        return responses

    def test_push_failure_status_returned(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_PUSH_RETRIES
        try:
            merge_module.MERGE_PUSH_RETRIES = 2
            with patch("subprocess.run") as mock_run, \
                 patch("app.database.create_merge_record"), \
                 patch("time.sleep"):
                mock_run.side_effect = self._merge_then_push_fail(mock_run, push_attempts=2)
                result = execute_merge(_TASK_ID)
            assert result.status == "push_failure"
        finally:
            merge_module.MERGE_PUSH_RETRIES = original

    def test_task_NOT_marked_completed_on_push_failure(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_PUSH_RETRIES
        try:
            merge_module.MERGE_PUSH_RETRIES = 2
            with patch("subprocess.run") as mock_run, \
                 patch("app.database.update_task") as mock_update, \
                 patch("app.database.create_merge_record"), \
                 patch("time.sleep"):
                mock_run.side_effect = self._merge_then_push_fail(mock_run, push_attempts=2)
                execute_merge(_TASK_ID)
            for c in mock_update.call_args_list:
                assert c != call(_TASK_ID, type="completed")
        finally:
            merge_module.MERGE_PUSH_RETRIES = original

    def test_merge_record_stored_as_push_failure(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_PUSH_RETRIES
        try:
            merge_module.MERGE_PUSH_RETRIES = 2
            with patch("subprocess.run") as mock_run, \
                 patch("app.database.create_merge_record") as mock_record, \
                 patch("time.sleep"):
                mock_run.side_effect = self._merge_then_push_fail(mock_run, push_attempts=2)
                execute_merge(_TASK_ID)
            # Should have been called with status="push_failure"
            assert any(
                "push_failure" in str(c)
                for c in mock_record.call_args_list
            )
        finally:
            merge_module.MERGE_PUSH_RETRIES = original

    def test_push_retried_n_times(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_PUSH_RETRIES
        try:
            merge_module.MERGE_PUSH_RETRIES = 3
            push_calls = []
            def counting_run(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "push":
                    push_calls.append(1)
                    return _make_run_result(1, "", "push failed")
                # Other calls succeed
                cmd_str = " ".join(str(c) for c in cmd)
                if "branch" in cmd_str and "--list" in cmd_str:
                    return _make_run_result(0, _BRANCH)
                return _make_run_result(0)

            with patch("subprocess.run", side_effect=counting_run), \
                 patch("app.database.create_merge_record"), \
                 patch("time.sleep"):
                execute_merge(_TASK_ID)
            assert len(push_calls) == 3
        finally:
            merge_module.MERGE_PUSH_RETRIES = original

    def test_push_success_on_second_attempt(self):
        from app.agent import merge as merge_module
        from app.agent.merge import execute_merge

        original = merge_module.MERGE_PUSH_RETRIES
        try:
            merge_module.MERGE_PUSH_RETRIES = 3
            push_attempt = [0]
            def push_retry_run(*args, **kwargs):
                cmd = args[0] if args else kwargs.get("args", [])
                if isinstance(cmd, list) and len(cmd) >= 2 and cmd[0] == "git" and cmd[1] == "push":
                    push_attempt[0] += 1
                    if push_attempt[0] < 2:
                        return _make_run_result(1, "", "push failed")
                    return _make_run_result(0)
                cmd_str = " ".join(str(c) for c in cmd)
                if "branch" in cmd_str and "--list" in cmd_str:
                    return _make_run_result(0, _BRANCH)
                if "pytest" in cmd_str or "pytest" in str(cmd):
                    return _make_run_result(0, "1 passed")
                return _make_run_result(0)

            with patch("subprocess.run", side_effect=push_retry_run), \
                 patch("app.database.update_task"), \
                 patch("app.database.create_merge_record"), \
                 patch("time.sleep"):
                result = execute_merge(_TASK_ID)
            assert result.status == "merged"
        finally:
            merge_module.MERGE_PUSH_RETRIES = original
