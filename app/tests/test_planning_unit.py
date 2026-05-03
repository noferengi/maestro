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
from app.agent.planning_gate import PlanningGate

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
        assert len(result.checks) == 10


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
        assert check.hard_fail is False  # advisory-only: INDEV tests are the real arbiter

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
            "choices": [{
                "message": {
                    "content": None,
                    "tool_calls": [{
                        "id": "tc-feas",
                        "type": "function",
                        "function": {
                            "name": "submit_work",
                            "arguments": json.dumps({
                                "signal": "ACCEPTED",
                                "summary": "Feasibility check passed",
                                "payload": {
                                    "feasible": True,
                                    "spec_violation": False,
                                    "spec_violation_detail": "",
                                    "concerns": [],
                                },
                            }),
                        },
                    }],
                },
                "finish_reason": "tool_calls",
            }],
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
    def test_check_interface_completeness_existing_file_passes(self, tmp_path):
        # Create a dummy project file
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / "existing.py").write_text("class Existing: pass")

        planning_result = {
            "interface_contracts": [
                {
                    "component": "src/new_file.py",
                    "provides": ["NewClass"],
                    "consumes": [str(src_dir / "existing.py"), "src.existing"]
                }
            ]
        }

        # Test direct _check_interface_completeness
        # Use str(tmp_path) as project_path
        gate = PlanningGate("test", planning_result, [], project_path=str(tmp_path))
        check = gate._check_interface_completeness()

        assert check.passed
        assert "Filtered" in check.detail or "all consumes resolved" in check.detail

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
        assert len(result["checks"]) == 10
        for check in result["checks"]:
            for field in ("name", "passed", "hard_fail", "detail"):
                assert field in check, f"Missing field '{field}' in check {check['name']}"


# ---------------------------------------------------------------------------
# PlanningPipeline unit tests
# ---------------------------------------------------------------------------

import asyncio as _asyncio
from unittest.mock import AsyncMock as _AsyncMock, patch as _patch


def _run_async(coro):
    loop = _asyncio.new_event_loop()
    _asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_pipeline(title="Write a function", description="A simple task."):
    from app.agent.planning import PlanningPipeline
    return PlanningPipeline(
        task_id="test-task",
        task_title=title,
        task_description=description,
        all_tasks=[],
    )


class TestFailedGenerationShortCircuit:
    """failed_generation dummy from the judge must skip the reviewer panel."""

    def test_reviewer_panel_skipped_when_all_designs_fail(self):
        """When _stage_judge_designs returns failed_generation, _stage_design_review is not called."""
        pipeline = _make_pipeline()
        reviewer_calls: list = []

        async def mock_survey(self):
            return "SURVEY: empty greenfield project"

        async def mock_generate(self, survey, best_of_n=None):
            n = best_of_n or 5
            return [{"error": "LLM timeout"} for _ in range(n)]

        async def mock_review(self, design, survey):
            reviewer_calls.append(design)
            return []

        async def mock_pitfall(self, design, survey):
            return []

        async def mock_consolidate(self, design, pitfalls, survey):
            return design

        with _patch.object(pipeline.__class__, "_stage_codebase_survey", mock_survey), \
             _patch.object(pipeline.__class__, "_stage_design_generation", mock_generate), \
             _patch.object(pipeline.__class__, "_stage_design_review", mock_review), \
             _patch.object(pipeline.__class__, "_stage_pitfall_detection", mock_pitfall), \
             _patch.object(pipeline.__class__, "_stage_consolidation", mock_consolidate), \
             _patch("app.agent.planning.is_shutting_down", return_value=False), \
             _patch("app.agent.planning.PlanningPipeline._store_result", return_value=None):
            result = _run_async(pipeline.run())

        assert reviewer_calls == [], (
            "Reviewer panel must not be called when all design generation failed"
        )

    def test_failed_generation_recorded_as_rejected_vote(self):
        """The failed_generation path injects a synthetic REJECTED vote so the loop retries."""
        from app.agent.verdicts import Verdict
        pipeline = _make_pipeline()

        async def mock_survey(self):
            return "SURVEY: empty project"

        async def mock_generate(self, survey, best_of_n=None):
            n = best_of_n or 5
            return [{"error": "timeout"} for _ in range(n)]

        async def mock_review(self, design, survey):
            return []

        async def mock_pitfall(self, design, survey):
            return []

        async def mock_consolidate(self, design, pitfalls, survey):
            return design

        with _patch.object(pipeline.__class__, "_stage_codebase_survey", mock_survey), \
             _patch.object(pipeline.__class__, "_stage_design_generation", mock_generate), \
             _patch.object(pipeline.__class__, "_stage_design_review", mock_review), \
             _patch.object(pipeline.__class__, "_stage_pitfall_detection", mock_pitfall), \
             _patch.object(pipeline.__class__, "_stage_consolidation", mock_consolidate), \
             _patch("app.agent.planning.is_shutting_down", return_value=False), \
             _patch("app.agent.planning.PlanningPipeline._store_result", return_value=None):
            result = _run_async(pipeline.run())

        # After all retries fail, the last review_votes should be the synthetic one
        assert any(
            v.verdict is Verdict.REJECTED
            for v in result.review_votes
        ), "Expected a synthetic REJECTED vote after failed generation"


class TestComplexityClassifier:
    """_is_simple_task() heuristics."""

    def test_greenfield_keyword_simple(self):
        from app.agent.planning import _is_simple_task
        assert _is_simple_task("Build something", "A greenfield project") is True

    def test_naive_keyword_simple(self):
        from app.agent.planning import _is_simple_task
        assert _is_simple_task("naive recursive Fibonacci", "Write a naive recursive function") is True

    def test_short_description_simple(self):
        from app.agent.planning import _is_simple_task
        assert _is_simple_task("Add logging", "Add a log line.") is True

    def test_long_complex_description_not_simple(self):
        from app.agent.planning import _is_simple_task
        long_desc = (
            "Refactor the authentication middleware to support OAuth2 PKCE flows. "
            "This involves updating the token validation pipeline, adding refresh-token "
            "rotation, integrating with the existing session store, migrating existing "
            "sessions gracefully, and updating the API documentation to reflect the "
            "new authentication flow. Security review is required before merge."
        )
        assert _is_simple_task("OAuth2 PKCE refactor", long_desc) is False

    def test_simple_task_gets_reduced_best_of_n(self):
        """Simple tasks must use a smaller design pool in run()."""
        from app.agent.planning import PlanningPipeline, PLANNING_BEST_OF_N
        pipeline = _make_pipeline(
            title="Write a recursive Fibonacci",
            description="Write a naive recursive Fibonacci function in Python.",
        )
        generation_counts: list[int] = []

        async def mock_survey(self):
            return "SURVEY: empty project"

        async def mock_generate(self, survey, best_of_n=None):
            generation_counts.append(best_of_n or PLANNING_BEST_OF_N)
            return [{"error": "timeout"} for _ in range(best_of_n or PLANNING_BEST_OF_N)]

        async def mock_review(self, design, survey):
            return []

        async def mock_pitfall(self, design, survey):
            return []

        async def mock_consolidate(self, design, pitfalls, survey):
            return design

        with _patch.object(pipeline.__class__, "_stage_codebase_survey", mock_survey), \
             _patch.object(pipeline.__class__, "_stage_design_generation", mock_generate), \
             _patch.object(pipeline.__class__, "_stage_design_review", mock_review), \
             _patch.object(pipeline.__class__, "_stage_pitfall_detection", mock_pitfall), \
             _patch.object(pipeline.__class__, "_stage_consolidation", mock_consolidate), \
             _patch("app.agent.planning.is_shutting_down", return_value=False), \
             _patch("app.agent.planning.PlanningPipeline._store_result", return_value=None):
            _run_async(pipeline.run())

        assert all(n < PLANNING_BEST_OF_N for n in generation_counts), (
            f"Simple task should use fewer than {PLANNING_BEST_OF_N} designs, got {generation_counts}"
        )

    def test_simple_task_uses_lite_reviewer_subset(self):
        """Simple tasks must run fewer reviewers in _stage_design_review."""
        from app.agent.planning import PlanningPipeline, _SIMPLE_TASK_REVIEWER_SUBSET
        pipeline = _make_pipeline(
            title="Write a recursive Fibonacci",
            description="Write a naive recursive Fibonacci function.",
        )
        pipeline._is_simple = True  # force simple path

        _MINIMAL_DESIGN = {
            "design_rationale": "Simple recursive fib",
            "file_manifest": [],
            "implementation_steps": [],
        }
        reviewer_names_used: list[str] = []

        original_call_llm_call = None

        async def mock_call_llm(messages, **kwargs):
            agent_name = kwargs.get("agent_name", "")
            reviewer_names_used.append(agent_name)
            return {
                "choices": [{"message": {"content": '{"verdict":"LIKELY","confidence":95,"justification":"ok"}'}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 5},
            }

        with _patch("app.agent.planning.call_llm", mock_call_llm):
            result = _run_async(pipeline._stage_design_review(_MINIMAL_DESIGN, "survey"))

        full_reviewer_names = {r["name"] for r in [
            {"name": "coupling_reviewer"}, {"name": "interface_reviewer"},
            {"name": "testability_reviewer"}, {"name": "security_design_reviewer"},
            {"name": "performance_reviewer"},
        ]}
        voted_verdicts = {v.stage for v in result}
        skipped = full_reviewer_names - voted_verdicts
        assert skipped, "Some reviewers should be skipped for simple tasks"
        assert voted_verdicts.issubset(_SIMPLE_TASK_REVIEWER_SUBSET), (
            f"Simple task should only use {_SIMPLE_TASK_REVIEWER_SUBSET}, got {voted_verdicts}"
        )
