"""
Unit tests for app.agent.tool_success_store — pure in-memory, no mocks needed.
"""

from __future__ import annotations

import pytest

from app.agent.tool_success_store import (
    reset,
    record,
    query,
    query_group,
    get_all,
    infer_success,
    TRACKED_TOOLS,
)

# Use a fixed task ID so tests are self-contained; reset() clears it before each test.
_TASK = "test-task-store"


@pytest.fixture(autouse=True)
def fresh_store():
    reset(_TASK)
    yield
    reset(_TASK)


# ── query_group ──────────────────────────────────────────────────────────────

class TestQueryGroup:
    def test_any_tool_succeeds_returns_true(self):
        record(_TASK, "run_test_cargo", True)
        assert query_group(_TASK, ["run_test_pytest", "run_test_cargo", "run_test_go"]) is True

    def test_none_succeed_returns_false(self):
        record(_TASK, "run_test_pytest", False)
        assert query_group(_TASK, ["run_test_pytest", "run_test_cargo"]) is False

    def test_never_called_returns_false(self):
        assert query_group(_TASK, ["run_test_pytest", "run_test_cargo"]) is False

    def test_empty_group_returns_false(self):
        assert query_group(_TASK, []) is False

    def test_all_succeed_returns_true(self):
        record(_TASK, "run_test_pytest", True)
        record(_TASK, "run_test_cargo", True)
        assert query_group(_TASK, ["run_test_pytest", "run_test_cargo"]) is True

    def test_mixed_success_failure_returns_true(self):
        record(_TASK, "run_test_pytest", False)
        record(_TASK, "run_test_cargo", True)
        assert query_group(_TASK, ["run_test_pytest", "run_test_cargo"]) is True

    def test_single_tool_succeeds(self):
        record(_TASK, "run_lean4", True)
        assert query_group(_TASK, ["run_lean4"]) is True

    def test_single_tool_fails(self):
        record(_TASK, "run_lean4", False)
        assert query_group(_TASK, ["run_lean4"]) is False

    def test_untracked_tool_never_counted_as_success(self):
        # "run_arbitrary" is not in TRACKED_TOOLS so record() ignores it
        record(_TASK, "run_arbitrary", True)
        assert query_group(_TASK, ["run_arbitrary"]) is False

    def test_after_reset_group_returns_false(self):
        record(_TASK, "run_test_pytest", True)
        reset(_TASK)
        assert query_group(_TASK, ["run_test_pytest"]) is False


# ── existing API not regressed ────────────────────────────────────────────────

class TestQueryAndRecord:
    def test_never_called_is_none(self):
        assert query(_TASK, "run_test_pytest") is None

    def test_success_recorded(self):
        record(_TASK, "run_test_pytest", True)
        assert query(_TASK, "run_test_pytest") is True

    def test_failure_recorded(self):
        record(_TASK, "run_test_pytest", False)
        assert query(_TASK, "run_test_pytest") is False

    def test_untracked_tool_not_stored(self):
        record(_TASK, "submit_work", True)
        assert query(_TASK, "submit_work") is None
        assert "submit_work" not in get_all(_TASK)


# ── infer_success ─────────────────────────────────────────────────────────────

class TestInferSuccess:
    def test_sandbox_ok_true(self):
        import json
        result = json.dumps({"ok": True, "stdout": "proof compiled"})
        assert infer_success("run_lean4", result) is True

    def test_sandbox_ok_false(self):
        import json
        result = json.dumps({"ok": False, "error": "type mismatch"})
        assert infer_success("run_lean4", result) is False

    def test_subprocess_exit_zero(self):
        assert infer_success("run_test_pytest", "[EXIT:0]\n5 passed") is True

    def test_subprocess_nonzero_exit(self):
        assert infer_success("run_test_pytest", "[EXIT:1]\n2 failed") is False

    def test_error_prefix_always_false(self):
        assert infer_success("run_check_mypy", "ERROR: tool failed") is False

    def test_no_exit_prefix_is_false(self):
        assert infer_success("run_check_ruff", "All checks passed") is False
