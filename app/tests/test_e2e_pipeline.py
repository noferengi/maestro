"""
End-to-end pipeline tests.

Patches at the httpx.AsyncClient layer (via MockLLM) rather than at call_llm,
so that the full HTTP -> token-tracking -> budget-entry chain is exercised.
All real DB writes and subprocess calls are mocked out.
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


def _patch_static(pipeline_instance):
    """Monkey-patch _stage_static_analysis to avoid tree-sitter I/O."""
    _STATIC_VOTE = {
        "stage": "static_analysis",
        "verdict": "LIKELY",
        "confidence": 0.95,
        "justification": "Clean static analysis.",
        "raw_response": None,
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "model": "static_analysis",
    }

    async def _mock_static(scope_vote):
        return _STATIC_VOTE

    pipeline_instance._stage_static_analysis = _mock_static


def _mock_client_cls(mock_llm):
    """
    Return a MagicMock suitable for ``patch("httpx.AsyncClient", ...)``.

    When the patched AsyncClient is instantiated and used as an async context
    manager, ``client.post(url, ...)`` is routed to ``mock_llm.handle_post``.
    """
    mock_instance = AsyncMock()
    mock_instance.post = mock_llm.handle_post  # handle_post is async def
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls


def _sec_response_content(verdict: str, pt: int = 50, ct: int = 100) -> str:
    """Build JSON string for a security reviewer LLM response."""
    return json.dumps({
        "verdict": verdict,
        "confidence": 90 if verdict == "LIKELY" else 20,
        "justification": "ok",
        "findings": [],
        "critical_count": 0,
        "high_count": 0,
    })


def _submit_work_response(verdict: str, pt: int = 50, ct: int = 5):
    """Build a submit_work tool-call response for security/conceptual/full-review pipelines."""
    from app.agent.mock_llm import MockLLM
    signal = "REVERT_TO_DESIGN" if verdict == "REJECTED" else "ACCEPTED"
    return MockLLM._tool_call_response(
        "submit_work",
        {
            "signal": signal,
            "summary": "ok",
            "payload": {
                "verdict": verdict,
                "confidence": 90 if verdict == "LIKELY" else 20,
                "findings": [],
            },
        },
        prompt_tokens=pt,
        completion_tokens=ct,
    )


def _make_security_mock_llm(*verdicts, pt=50, ct=100):
    """
    Build a MockLLM with a fixed queue of security-formatted responses.
    Each verdict string in *verdicts produces one queued response.
    """
    from app.agent.mock_llm import MockLLM

    ml = MockLLM(scenario="pass")  # bootstrap valid internal state
    ml._response_queue = [
        _submit_work_response(v, pt=pt, ct=ct)
        for v in verdicts
    ]
    ml._queue_index = 0
    return ml


# ---------------------------------------------------------------------------
# 1–2. Intake pipeline via MockLLM ("intake_all_pass" / "intake_rejected")
# ---------------------------------------------------------------------------


class TestIntakePipelineE2E:
    def _run_intake(self, scenario: str, *, budget_id=1, llm_id=1):
        from app.agent.intake import IntakePipeline
        from app.agent.mock_llm import MockLLM

        mock_llm = MockLLM(scenario=scenario)

        async def _go():
            pipeline = IntakePipeline(
                task_id="e2e-intake",
                task_description="Add authentication endpoint",
                task_title="Auth",
                all_tasks=[],
                project="TheMaestro",
                llm_id=llm_id,
                budget_id=budget_id,
            )
            _patch_static(pipeline)
            return await pipeline.run()

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                return _run(_go())

    def test_intake_all_pass_through_mock_llm(self):
        result = self._run_intake("intake_all_pass")
        assert result["outcome"] == "passed"

    def test_intake_rejected_through_mock_llm(self):
        result = self._run_intake("intake_rejected")
        assert result["outcome"] == "rejected"


# ---------------------------------------------------------------------------
# 3. Budget entries are created for each LLM stage
# ---------------------------------------------------------------------------


class TestIntakeBudgetEntries:
    def test_intake_budget_entries_recorded(self):
        from app.agent.intake import IntakePipeline
        from app.agent.mock_llm import MockLLM

        mock_llm = MockLLM(scenario="intake_all_pass")
        mock_create = MagicMock()

        async def _go():
            pipeline = IntakePipeline(
                task_id="e2e-budget",
                task_description="Add authentication endpoint",
                task_title="Auth",
                all_tasks=[],
                project="TheMaestro",
                llm_id=1,
                budget_id=1,
            )
            _patch_static(pipeline)
            return await pipeline.run()

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", mock_create):
                _run(_go())

        # scope + conflict + feasibility = 3 LLM calls -> 3 budget entries
        assert mock_create.call_count >= 3


# ---------------------------------------------------------------------------
# 4. PlanningGate feasibility via MockLLM
# ---------------------------------------------------------------------------


class TestPlanningGateE2E:
    def test_planning_gate_feasibility_via_mock_llm(self):
        from app.agent.planning_gate import run_planning_gate
        from app.agent.mock_llm import MockLLM, PatternRule

        mock_llm = MockLLM(custom_rules=[
            PatternRule(
                pattern="feasibility",
                response_content=json.dumps({"feasible": True, "concerns": []}),
            )
        ])
        mock_create = MagicMock()

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", mock_create):
                with patch(
                    "app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", True
                ):
                    result = _run(
                        run_planning_gate(
                            task_id="e2e-gate",
                            planning_result=_VALID_PLAN,
                            all_tasks=[],
                            llm_id=1,
                            budget_id=1,
                        )
                    )

        assert result["passed"] is True
        mock_create.assert_called()  # budget entry logged for feasibility call


# ---------------------------------------------------------------------------
# 5–6. Security pipeline via MockLLM
# ---------------------------------------------------------------------------


class TestSecurityPipelineE2E:
    def _run_security(self, *verdicts, veto_power=True):
        from app.agent.security_review import SecurityPipeline

        mock_llm = _make_security_mock_llm(*verdicts)

        async def _go():
            pipeline = SecurityPipeline(
                task_id="e2e-sec",
                task_description="Add authentication endpoint",
                llm_id=1,
                budget_id=1,
            )
            return await pipeline.run()

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                with patch("app.database.create_security_review_result", MagicMock()):
                    with patch(
                        "app.agent.security_review.run_shell_security", return_value=""
                    ):
                        with patch(
                            "app.agent.security_review.SECURITY_REVIEW_VETO_POWER",
                            veto_power,
                        ):
                            return _run(_go())

    def test_security_all_pass_via_mock_llm(self):
        result = self._run_security("LIKELY", "LIKELY", "LIKELY")
        assert result.outcome == "passed"

    def test_security_rejected_via_mock_llm(self):
        result = self._run_security("LIKELY", "LIKELY", "REJECTED")
        assert result.outcome == "rejected"
        assert result.demotion_target is not None


# ---------------------------------------------------------------------------
# 7. Token accumulation across security pipeline reviewers
# ---------------------------------------------------------------------------


class TestSecurityTokenAccumulation:
    def test_token_accumulation_in_security_pipeline(self):
        from app.agent.security_review import SecurityPipeline

        # 3 reviewers × 50pt / 100ct each
        mock_llm = _make_security_mock_llm("LIKELY", "LIKELY", "LIKELY", pt=50, ct=100)

        async def _go():
            pipeline = SecurityPipeline(
                task_id="e2e-tokens",
                task_description="Add auth",
                llm_id=1,
                budget_id=1,
            )
            return await pipeline.run()

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                with patch("app.database.create_security_review_result", MagicMock()):
                    with patch(
                        "app.agent.security_review.run_shell_security", return_value=""
                    ):
                        result = _run(_go())

        assert result.prompt_tokens == 150
        assert result.completion_tokens == 300


# ---------------------------------------------------------------------------
# 8. budget_id enforcement surfaces through the pipeline
# ---------------------------------------------------------------------------


class TestBudgetIdEnforcement:
    def test_budget_id_enforcement_in_pipeline(self):
        from app.agent.intake import IntakePipeline

        async def _go():
            pipeline = IntakePipeline(
                task_id="enforcement-test",
                task_description="Test",
                task_title="Test",
                all_tasks=[],
                project="TheMaestro",
                llm_id=1,
                budget_id=None,  # Missing - should be enforced
            )
            _patch_static(pipeline)
            return await pipeline.run()

        # call_llm raises ValueError for missing budget_id.
        # IntakePipeline catches stage exceptions internally -> outcome is not "passed".
        result = _run(_go())
        assert result["outcome"] != "passed"


# ---------------------------------------------------------------------------
# 9. Custom PatternRule drives responses in the real HTTP call chain
# ---------------------------------------------------------------------------


class TestMockLLMCustomRules:
    def test_mock_llm_custom_rules_drive_responses(self):
        from app.agent.planning_gate import run_planning_gate
        from app.agent.mock_llm import MockLLM, PatternRule

        feasibility_response = {"feasible": True, "concerns": ["minor risk"]}
        mock_llm = MockLLM(custom_rules=[
            PatternRule(
                pattern="feasibility",
                response_content=json.dumps(feasibility_response),
            )
        ])

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                with patch(
                    "app.agent.planning_gate.PLANNING_GATE_FEASIBILITY_RECHECK", True
                ):
                    result = _run(
                        run_planning_gate(
                            task_id="e2e-custom",
                            planning_result=_VALID_PLAN,
                            all_tasks=[],
                            llm_id=1,
                            budget_id=1,
                        )
                    )

        # PatternRule matched and the concern string was extracted correctly
        assert result["passed"] is True
        assert mock_llm.call_count >= 1
        feasibility_check = next(
            c for c in result["checks"] if c["name"] == "feasibility_recheck"
        )
        assert "minor risk" in feasibility_check["detail"]


# ---------------------------------------------------------------------------
# Shared data for new E2E tests (10–12)
# ---------------------------------------------------------------------------

_EMPTY_PLAN = {
    "file_manifest": [],
    "dependency_graph": {},
    "implementation_steps": [],
    "test_strategy": [],
}


def _make_conceptual_mock_llm(*verdicts, pt=50, ct=100):
    """Queue of conceptual reviewer LLM responses (one per verdict string)."""
    from app.agent.mock_llm import MockLLM

    ml = MockLLM(scenario="pass")
    ml._response_queue = [
        _submit_work_response(v, pt=pt, ct=ct)
        for v in verdicts
    ]
    ml._queue_index = 0
    return ml


def _make_full_review_mock_llm(*verdicts, pt=50, ct=100):
    """Queue of full-review LLM responses (one per verdict string)."""
    from app.agent.mock_llm import MockLLM

    ml = MockLLM(scenario="pass")
    ml._response_queue = [
        _submit_work_response(v, pt=pt, ct=ct)
        for v in verdicts
    ]
    ml._queue_index = 0
    return ml


# ---------------------------------------------------------------------------
# 10. ConceptualReviewPipeline E2E via MockLLM
# ---------------------------------------------------------------------------


class TestConceptualReviewPipelineE2E:
    def _run_conceptual(self, mock_llm, *, task_id="e2e-cr"):
        from app.agent.conceptual_review import run_conceptual_review

        async def _go():
            return await run_conceptual_review(
                task_id=task_id,
                task_description="Add authentication endpoint",
                planning_result=_EMPTY_PLAN,
                llm_id=1,
                budget_id=1,
            )

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                return _run(_go())

    def test_conceptual_review_all_pass(self):
        mock_llm = _make_conceptual_mock_llm("LIKELY", "LIKELY", "LIKELY", "LIKELY")
        result = self._run_conceptual(mock_llm)
        assert result["outcome"] == "passed"

    def test_conceptual_review_high_severity_blocks(self):
        from app.agent.mock_llm import MockLLM

        ml = MockLLM(scenario="pass")
        ml._response_queue = [
            _submit_work_response("REJECTED"),
            _submit_work_response("LIKELY"),
            _submit_work_response("LIKELY"),
            _submit_work_response("LIKELY"),
        ]
        ml._queue_index = 0
        result = self._run_conceptual(ml)
        assert result["outcome"] == "rejected"

    def test_conceptual_review_budget_entries_recorded(self):
        from app.agent.conceptual_review import run_conceptual_review

        mock_llm = _make_conceptual_mock_llm("LIKELY", "LIKELY", "LIKELY", "LIKELY")
        mock_create = MagicMock()

        async def _go():
            return await run_conceptual_review(
                task_id="e2e-cr-budget",
                task_description="Add auth",
                planning_result=_EMPTY_PLAN,
                llm_id=1,
                budget_id=1,
            )

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", mock_create):
                _run(_go())

        assert mock_create.call_count >= 4

    def test_conceptual_review_token_accumulation(self):
        mock_llm = _make_conceptual_mock_llm("LIKELY", "LIKELY", "LIKELY", "LIKELY", pt=50, ct=100)
        result = self._run_conceptual(mock_llm, task_id="e2e-cr-tokens")
        assert result["total_prompt_tokens"] == 200
        assert result["total_completion_tokens"] == 400


# ---------------------------------------------------------------------------
# 11. FullReviewPipeline E2E via MockLLM
# ---------------------------------------------------------------------------


class TestFullReviewPipelineE2E:
    def _run_full_review(self, mock_llm, *, task_id="e2e-fr"):
        from app.agent.full_review import run_full_review_pipeline

        async def _go():
            return await run_full_review_pipeline(
                task_id=task_id,
                task_description="Add authentication endpoint",
                files_changed=[],  # no frontend -> 3 reviewers only
                llm_id=1,
                budget_id=1,
            )

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", MagicMock()):
                with patch("app.database.create_full_review_result", MagicMock()):
                    return _run(_go())

    def test_full_review_all_pass(self):
        mock_llm = _make_full_review_mock_llm("LIKELY", "LIKELY", "LIKELY")
        result = self._run_full_review(mock_llm)
        assert result["outcome"] == "passed"

    def test_full_review_rejected(self):
        # 3rd reviewer (integration) rejects -> demotion_target="indev"
        mock_llm = _make_full_review_mock_llm("LIKELY", "LIKELY", "REJECTED")
        result = self._run_full_review(mock_llm)
        assert result["outcome"] == "rejected"
        assert result["demotion_target"] is not None

    def test_full_review_budget_entries_recorded(self):
        from app.agent.full_review import run_full_review_pipeline

        mock_llm = _make_full_review_mock_llm("LIKELY", "LIKELY", "LIKELY")
        mock_create = MagicMock()

        async def _go():
            return await run_full_review_pipeline(
                task_id="e2e-fr-budget",
                task_description="Add auth",
                files_changed=[],
                llm_id=1,
                budget_id=1,
            )

        with patch("httpx.AsyncClient", _mock_client_cls(mock_llm)):
            with patch("app.database.create_budget_entry", mock_create):
                with patch("app.database.create_full_review_result", MagicMock()):
                    _run(_go())

        assert mock_create.call_count >= 3

    def test_full_review_token_accumulation(self):
        mock_llm = _make_full_review_mock_llm("LIKELY", "LIKELY", "LIKELY", pt=50, ct=100)
        result = self._run_full_review(mock_llm, task_id="e2e-fr-tokens")
        assert result["total_prompt_tokens"] == 150
        assert result["total_completion_tokens"] == 300


# ---------------------------------------------------------------------------
# 12. Scheduler full-chain E2E (dispatcher functions called synchronously)
# ---------------------------------------------------------------------------


class TestSchedulerFullChainE2E:
    def test_full_chain_conceptual_to_full_review(self):
        from app.agent.scheduler import (
            _run_conceptual_review_task,
            _run_optimization_task,
            _run_security_task,
        )

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        fake_task = _fake_db_task_e2e(task_id="chain-t", task_type="conceptual_review")

        fake_plan = MagicMock()
        fake_plan.file_manifest = "[]"
        fake_plan.implementation_steps = "[]"
        fake_plan.dependency_graph = "{}"
        fake_plan.test_strategy = "[]"

        with patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.get_planning_result", return_value=fake_plan), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.conceptual_review.run_conceptual_review",
                   return_value={"outcome": "passed", "votes": [], "summary": "",
                                 "total_prompt_tokens": 0, "total_completion_tokens": 0}), \
             patch("app.agent.optimization.run_optimization_pipeline",
                   return_value={"outcome": "optimized",
                                 "total_prompt_tokens": 0, "total_completion_tokens": 0}), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value={"outcome": "passed", "demotion_target": None, "summary": "",
                                 "total_prompt_tokens": 0, "total_completion_tokens": 0}), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_conceptual_review_task("chain-t", "http://localhost:8008/v1", "model")
            _run_optimization_task("chain-t", "http://localhost:8008/v1", "model")
            _run_security_task("chain-t", "http://localhost:8008/v1", "model")

        assert "optimization" in updated_types
        assert "security" in updated_types
        assert "full_review" in updated_types

    def test_full_chain_merge_virtual_passed_records_ready_for_review(self):
        """After full_review passes and virtual merge succeeds, task history is updated."""
        from app.agent.scheduler import _run_full_review_task
        from app.agent.merge import MergeResult

        mock_append = MagicMock()
        fake_task = _fake_db_task_e2e(task_id="merge-t", task_type="full_review")

        with patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.update_task", MagicMock()), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.database.append_task_history", mock_append), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value={"outcome": "passed", "demotion_target": None, "summary": "",
                                 "total_prompt_tokens": 0, "total_completion_tokens": 0, "votes": []}), \
             patch("app.agent.merge.execute_merge",
                   return_value=MergeResult(task_id="merge-t", status="virtual_passed")), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_full_review_task("merge-t", "http://localhost:8008/v1", "model")

        mock_append.assert_called_once()
        assert mock_append.call_args[0][0] == "merge-t"
        assert mock_append.call_args[0][1] == "ready_for_review"

    def test_full_chain_security_failure_stops_chain(self):
        from app.agent.scheduler import _run_security_task

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        mock_record = MagicMock()

        with patch("app.database.get_task", return_value=_fake_db_task_e2e(task_id="sec-fail", task_type="security")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value={"outcome": "rejected", "demotion_target": "indev",
                                 "summary": "vuln found",
                                 "total_prompt_tokens": 0, "total_completion_tokens": 0}), \
             patch("app.agent.scheduler._record_demotion_inline", mock_record):
            _run_security_task("sec-fail", "http://localhost:8008/v1", "model")

        assert "indev" in updated_types
        mock_record.assert_called_once()
        assert "full_review" not in updated_types


def _fake_db_task_e2e(task_id="t", task_type="planning"):
    """Minimal fake DB task for E2E chain tests."""
    t = MagicMock()
    t.id = task_id
    t.type = task_type
    t.description = "Add auth endpoint"
    t.title = "Auth"
    t.project = "TestProject"
    t.llm_id = 1
    t.budget_id = 1
    t.prerequisites = []
    t.demotion_count = 0
    t.demotion_history = []
    return t
