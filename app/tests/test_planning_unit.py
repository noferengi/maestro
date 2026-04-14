"""
Unit tests for app/agent/planning_gate.py.

Covers all 7 checks of PlanningGate and the public run_planning_gate() entry point.
Patches LLM calls, file-safety assertions, and asyncio.sleep to stay fast and offline.
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VALID_PLAN = {
    "interface_contracts": [
        {"component": "auth", "provides": ["token"], "consumes": []}
    ],
    "dependency_graph": {"auth": []},
    "file_manifest": [{"path": "app/auth.py", "action": "create"}],
    "test_strategy": [
        {"component": "app/auth.py", "test_file": "tests/test_auth.py"}
    ],
    "implementation_steps": [
        {"order": 0, "component": "auth", "estimated_context_tokens": 1000}
    ],
}


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_gate(plan=None, all_tasks=None, max_context=100_000):
    from app.agent.planning_gate import PlanningGate

    return PlanningGate(
        task_id="test-task",
        planning_result=plan if plan is not None else _VALID_PLAN,
        all_tasks=all_tasks or [],
        max_context=max_context,
    )


def _get_check(result, name):
    return next(c for c in result.checks if c.name == name)


# ---------------------------------------------------------------------------
# 1. All checks pass on a minimal valid plan
# ---------------------------------------------------------------------------


class TestAllChecksPass:
    def test_all_checks_pass_minimal_plan(self):
        gate = _make_gate()
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        assert result.passed is True
        assert len(result.checks) == 7


# ---------------------------------------------------------------------------
# 2–3. Interface completeness (check 1)
# ---------------------------------------------------------------------------


class TestInterfaceCompleteness:
    def test_interface_unresolved_consumes_hard_fail(self):
        plan = {
            **_VALID_PLAN,
            "interface_contracts": [
                {"component": "api", "provides": [], "consumes": ["token"]},
            ],
        }
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "interface_completeness")
        assert check.passed is False
        assert check.hard_fail is True

    def test_interface_resolved_consumes_pass(self):
        plan = {
            **_VALID_PLAN,
            "interface_contracts": [
                {"component": "auth", "provides": ["token"], "consumes": []},
                {"component": "api", "provides": [], "consumes": ["token"]},
            ],
        }
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "interface_completeness")
        assert check.passed is True


# ---------------------------------------------------------------------------
# 4–5. Circular dependency (check 2)
# ---------------------------------------------------------------------------


class TestCircularDependency:
    def test_circular_dependency_detected(self):
        plan = {**_VALID_PLAN, "dependency_graph": {"A": ["B"], "B": ["A"]}}
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "circular_dependency")
        assert check.passed is False
        assert check.hard_fail is True

    def test_no_circular_dependency_passes(self):
        plan = {**_VALID_PLAN, "dependency_graph": {"A": ["B"], "B": []}}
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "circular_dependency")
        assert check.passed is True


# ---------------------------------------------------------------------------
# 6–7. Test strategy (check 3)
# ---------------------------------------------------------------------------


class TestTestStrategy:
    def test_test_strategy_missing_majority_fail(self):
        plan = {
            **_VALID_PLAN,
            "file_manifest": [
                {"path": "app/a.py", "action": "create"},
                {"path": "app/b.py", "action": "create"},
                {"path": "app/c.py", "action": "create"},
            ],
            "test_strategy": [],
        }
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "test_strategy")
        assert check.passed is False
        assert check.hard_fail is True

    def test_test_strategy_sufficient_coverage_pass(self):
        plan = {
            **_VALID_PLAN,
            "file_manifest": [{"path": "app/auth.py", "action": "create"}],
            "test_strategy": [
                {"component": "app/auth.py", "test_file": "tests/test_auth.py"}
            ],
        }
        gate = _make_gate(plan=plan)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "test_strategy")
        assert check.passed is True


# ---------------------------------------------------------------------------
# 8–9. Prerequisites resolved (check 4)
# ---------------------------------------------------------------------------


class TestPrerequisites:
    def test_prerequisite_not_done_hard_fail(self):
        all_tasks = [
            {"id": "test-task", "prerequisites": ["prereq-1"]},
            {"id": "prereq-1", "type": "planning"},
        ]
        gate = _make_gate(all_tasks=all_tasks)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "prerequisites_resolved")
        assert check.passed is False
        assert check.hard_fail is True

    def test_prerequisite_completed_passes(self):
        all_tasks = [
            {"id": "test-task", "prerequisites": ["prereq-1"]},
            {"id": "prereq-1", "type": "completed"},
        ]
        gate = _make_gate(all_tasks=all_tasks)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(gate.run())
        check = _get_check(result, "prerequisites_resolved")
        assert check.passed is True


# ---------------------------------------------------------------------------
# 10. File manifest safety (check 5)
# ---------------------------------------------------------------------------


class TestFileSafety:
    def test_file_safety_check_fail(self):
        gate = _make_gate()
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            with patch(
                "app.agent.tools._assert_safe_path",
                side_effect=ValueError("path traversal"),
            ):
                result = _run(gate.run())
        check = _get_check(result, "file_safety")
        assert check.passed is False
        assert check.hard_fail is True


# ---------------------------------------------------------------------------
# 11–13. Feasibility re-check (check 6)
# ---------------------------------------------------------------------------


class TestFeasibilityRecheck:
    def test_feasibility_recheck_disabled_no_llm_call(self):
        gate = _make_gate()
        mock_call = AsyncMock()
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            with patch("app.agent.llm_client.call_llm", mock_call):
                result = _run(gate.run())
        mock_call.assert_not_called()
        check = _get_check(result, "feasibility_recheck")
        assert check.passed is True
        assert "Skipped" in check.detail

    def test_feasibility_recheck_enabled_llm_pass(self):
        gate = _make_gate()
        feasibility_response = {
            "choices": [
                {
                    "message": {
                        "content": json.dumps({"feasible": True, "concerns": []})
                    }
                }
            ],
            "usage": {"prompt_tokens": 50, "completion_tokens": 100},
        }
        mock_call = AsyncMock(return_value=feasibility_response)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", True):
            with patch("app.agent.llm_client.call_llm", mock_call):
                result = _run(gate.run())
        mock_call.assert_called_once()
        check = _get_check(result, "feasibility_recheck")
        assert check.passed is True

    def test_feasibility_all_retries_exhausted_soft_fail(self):
        gate = _make_gate()
        mock_call = AsyncMock(side_effect=Exception("LLM unavailable"))
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", True):
            with patch("app.agent.llm_client.call_llm", mock_call):
                with patch("asyncio.sleep", new=AsyncMock()):
                    result = _run(gate.run())
        assert result.llm_check_unavailable is True
        check = _get_check(result, "feasibility_recheck")
        assert check.passed is True   # soft-fail, never blocks gate
        assert check.hard_fail is False


# ---------------------------------------------------------------------------
# 14–15. Context budget (check 7)
# ---------------------------------------------------------------------------


class TestContextBudget:
    def test_context_budget_within_limit_passes(self):
        plan = {
            **_VALID_PLAN,
            "implementation_steps": [
                {"order": 0, "component": "auth", "estimated_context_tokens": 1000}
            ],
        }
        gate = _make_gate(plan=plan, max_context=100_000)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            with patch(
                "app.agent.planning_gate.PLANNING_GATE_CONTEXT_SAFETY_MARGIN", 0.15
            ):
                result = _run(gate.run())
        check = _get_check(result, "context_budget")
        assert check.passed is True

    def test_context_budget_exceeds_limit_hard_fail(self):
        plan = {
            **_VALID_PLAN,
            "implementation_steps": [
                {"order": 0, "component": "auth", "estimated_context_tokens": 100_000}
            ],
        }
        gate = _make_gate(plan=plan, max_context=100_000)
        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            with patch(
                "app.agent.planning_gate.PLANNING_GATE_CONTEXT_SAFETY_MARGIN", 0.15
            ):
                result = _run(gate.run())
        check = _get_check(result, "context_budget")
        assert check.passed is False
        assert check.hard_fail is True


# ---------------------------------------------------------------------------
# 16. Public entry point
# ---------------------------------------------------------------------------


class TestRunPlanningGate:
    def test_run_planning_gate_returns_dict(self):
        from app.agent.planning_gate import run_planning_gate

        with patch("app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", False):
            result = _run(
                run_planning_gate(
                    task_id="test-task",
                    planning_result=_VALID_PLAN,
                    all_tasks=[],
                )
            )

        assert "checks" in result
        assert isinstance(result["checks"], list)
        assert len(result["checks"]) == 7
        for check in result["checks"]:
            for field in ("name", "passed", "hard_fail", "detail"):
                assert field in check, f"Missing field '{field}' in check {check['name']}"
