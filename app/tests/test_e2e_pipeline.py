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
from contextlib import asynccontextmanager

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Fixture: reset llm_client dispatch-stagger state between tests
# ---------------------------------------------------------------------------
# _endpoint_states is a module-level global that tracks per-endpoint stagger
# windows (_MIN_DISPATCH_GAP = 0.5 s).  asyncio.gather fans out concurrent
# reviewer coroutines that each claim a slot immediately; without a reset the
# Nth reviewer sleeps (N-1) * 0.5 s for real, turning each E2E test into a
# 1.5+ s sleep-fest even though httpx is fully mocked.

@pytest.fixture(autouse=True)
def _reset_llm_endpoint_state():
    from app.agent import llm_client
    llm_client._endpoint_states.clear()
    orig_gap = llm_client._MIN_DISPATCH_GAP
    llm_client._MIN_DISPATCH_GAP = 0.0  # no stagger in mocked tests
    yield
    llm_client._endpoint_states.clear()
    llm_client._MIN_DISPATCH_GAP = orig_gap


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


_E2E_STATIC_VOTE = {
    "stage": "static_analysis",
    "verdict": "LIKELY",
    "confidence": 0.95,
    "justification": "Clean static analysis.",
    "raw_response": None,
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "model": "static_analysis",
}


def _mock_client_cls(mock_llm):
    """
    Return a MagicMock suitable for ``patch("httpx.AsyncClient", ...)``.

    When the patched AsyncClient is instantiated and used as an async context
    manager, ``client.post(url, ...)`` is routed to ``mock_llm.handle_post``.
    It also supports ``client.stream(...)`` for SSE-compatible testing.
    """
    mock_instance = AsyncMock()
    mock_instance.post = mock_llm.handle_post

    @asynccontextmanager
    async def mock_stream(method, url, **kwargs):
        # We need to simulate the SSE stream.
        # Call the non-streaming mock first to get the response.
        resp_obj = await mock_llm.handle_post(url, **kwargs)
        full_json = resp_obj.json()

        # Convert to OpenAI streaming format
        choice = full_json["choices"][0]
        msg = choice["message"]
        delta = {}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = msg["tool_calls"]

        chunk = {
            "id": full_json["id"],
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": choice["finish_reason"]
            }],
            "usage": full_json["usage"]
        }

        mock_resp = MagicMock()
        mock_resp.is_success = True
        mock_resp.status_code = 200

        async def aiter_lines():
            yield f"data: {json.dumps(chunk)}"
            yield "data: [DONE]"

        mock_resp.aiter_lines = aiter_lines
        yield mock_resp

    mock_instance.stream = mock_stream

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




# ---------------------------------------------------------------------------
# 1–2. Intake pipeline via MockLLM ("intake_all_pass" / "intake_rejected")
# ---------------------------------------------------------------------------


class TestIntakePipelineE2E:
    def _run_intake(self, scenario: str, *, budget_id=1, llm_id=1):
        from app.agent.intake_stages import run_intake_pipeline
        from app.agent.mock_llm import MockLLM

        mock_llm = MockLLM(scenario=scenario)

        async def _go():
            with patch("app.agent.intake_stages._intake_static_analysis",
                       new=AsyncMock(return_value=_E2E_STATIC_VOTE)):
                return await run_intake_pipeline(
                    task_id="e2e-intake",
                    task_description="Add authentication endpoint",
                    task_title="Auth",
                    all_tasks=[],
                    project="TheMaestro",
                    llm_id=llm_id,
                    budget_id=budget_id,
                )

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
        from app.agent.intake_stages import run_intake_pipeline
        from app.agent.mock_llm import MockLLM

        mock_llm = MockLLM(scenario="intake_all_pass")
        mock_create = MagicMock()

        async def _go():
            with patch("app.agent.intake_stages._intake_static_analysis",
                       new=AsyncMock(return_value=_E2E_STATIC_VOTE)):
                return await run_intake_pipeline(
                    task_id="e2e-budget",
                    task_description="Add authentication endpoint",
                    task_title="Auth",
                    all_tasks=[],
                    project="TheMaestro",
                    llm_id=1,
                    budget_id=1,
                )

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
# 8. budget_id enforcement surfaces through the pipeline
# ---------------------------------------------------------------------------


class TestBudgetIdEnforcement:
    def test_budget_id_enforcement_in_pipeline(self):
        from app.agent.intake_stages import run_intake_pipeline

        async def _go():
            with patch("app.agent.intake_stages._intake_static_analysis",
                       new=AsyncMock(return_value=_E2E_STATIC_VOTE)):
                return await run_intake_pipeline(
                    task_id="enforcement-test",
                    task_description="Test",
                    task_title="Test",
                    all_tasks=[],
                    project="TheMaestro",
                    llm_id=1,
                    budget_id=None,  # Missing - should be enforced
                )

        # call_llm raises ValueError for missing budget_id.
        # intake stages catch stage exceptions internally -> outcome is not "passed".
        result = _run(_go())
        assert result["outcome"] != "passed"


# ---------------------------------------------------------------------------
# 9. Custom PatternRule drives responses in the real HTTP call chain
# ---------------------------------------------------------------------------


class TestMockLLMCustomRules:
    def test_mock_llm_custom_rules_drive_responses(self):
        from app.agent.planning_gate import run_planning_gate
        from app.agent.mock_llm import MockLLM, PatternRule

        mock_llm = MockLLM(custom_rules=[
            PatternRule(
                pattern="feasibility",
                response_content="",
                tool_calls=[{
                    "id": "call_mock_feas",
                    "type": "function",
                    "function": {
                        "name": "submit_work",
                        "arguments": json.dumps({
                            "signal": "ACCEPTED",
                            "summary": "Feasibility recheck complete",
                            "payload": {"feasible": True, "concerns": ["minor risk"]},
                        }),
                    },
                }],
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





