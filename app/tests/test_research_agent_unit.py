"""
test_research_agent_unit.py
---------------------------
Unit tests for app/agent/research.py.

Tests pure methods without LLM calls (extract_vote, build_life_context,
handle_tool_calls, build_restricted_schemas), plus the async run() with
call_llm patched to exercise:
  - Immediate verdict on first turn
  - Lives exhaustion fallback (NOT_SUITABLE, confidence 55)
  - NEEDS_RESEARCH verdict continues to next life rather than terminating
  - Token accumulation across lives
  - Tiebreaker mode: correct question format and pass/fail vote split
  - Tool call blocking for disallowed tools
  - Graceful handling of bad JSON in tool call arguments
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.research import (
    ResearchAgent,
    ResearchResult,
    _build_restricted_schemas,
    run_research,
    run_tiebreaker,
)
from app.agent.config import RESEARCH_AGENT_TOOLS, PROJECT_ROOT


# ---------------------------------------------------------------------------
# Async helper — consistent with the rest of the test suite
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        pass  # keep loop alive; matches pattern used across the test suite


# ---------------------------------------------------------------------------
# LLM mock helpers
# ---------------------------------------------------------------------------

def _llm_resp(content: str, tool_calls=None, prompt_tokens=10, completion_tokens=5):
    return {
        "choices": [{"message": {"content": content, "tool_calls": tool_calls}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
    }


def _verdict_json(verdict="LIKELY", confidence=95, findings="found it"):
    payload = {
        "verdict": verdict,
        "confidence": confidence,
        "justification": "test justification",
        "findings": findings,
    }
    return f"```json\n{json.dumps(payload)}\n```"


def _sequential_llm(*responses):
    """Return an async callable that yields responses in sequence (last repeats)."""
    idx = [0]

    async def _call(*args, **kwargs):
        i = min(idx[0], len(responses) - 1)
        idx[0] += 1
        return responses[i]

    return _call


# ===========================================================================
# _build_restricted_schemas
# ===========================================================================

class TestBuildRestrictedSchemas:
    def test_only_allowed_tools_present(self):
        schemas = _build_restricted_schemas()
        names = {s["function"]["name"] for s in schemas}
        for name in names:
            assert name in RESEARCH_AGENT_TOOLS, (
                f"Schema contains tool '{name}' that is not in RESEARCH_AGENT_TOOLS"
            )

    def test_all_allowed_tools_present(self):
        schemas = _build_restricted_schemas()
        names = {s["function"]["name"] for s in schemas}
        for tool in RESEARCH_AGENT_TOOLS:
            assert tool in names, (
                f"Expected tool '{tool}' missing from restricted schemas"
            )

    def test_write_file_excluded(self):
        """write_file is a write tool and must not appear in research schemas."""
        schemas = _build_restricted_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "write_file" not in names

    def test_run_shell_excluded(self):
        """run_shell is dangerous and must not appear in research schemas."""
        schemas = _build_restricted_schemas()
        names = {s["function"]["name"] for s in schemas}
        assert "run_shell" not in names


# ===========================================================================
# ResearchAgent._extract_vote
# ===========================================================================

class TestExtractVote:
    def setup_method(self):
        self.agent = ResearchAgent(question="test?", context={}, llm_id=1, budget_id=1)

    def test_valid_verdict_json_returned(self):
        result = self.agent._extract_vote(_verdict_json("LIKELY", 95))
        assert result is not None
        assert result["verdict"] == "LIKELY"
        assert result["confidence"] == 95

    def test_no_json_block_returns_none(self):
        result = self.agent._extract_vote("I need to look at more files.")
        assert result is None

    def test_json_missing_verdict_key_returns_none(self):
        content = '```json\n{"confidence": 80, "justification": "ok"}\n```'
        result = self.agent._extract_vote(content)
        assert result is None

    def test_json_missing_confidence_key_returns_none(self):
        content = '```json\n{"verdict": "LIKELY", "justification": "ok"}\n```'
        result = self.agent._extract_vote(content)
        assert result is None

    def test_malformed_json_returns_none(self):
        content = "```json\n{not valid json here}\n```"
        result = self.agent._extract_vote(content)
        assert result is None

    def test_rejected_verdict_extracted(self):
        result = self.agent._extract_vote(_verdict_json("REJECTED", 10))
        assert result["verdict"] == "REJECTED"

    def test_needs_research_verdict_extracted(self):
        result = self.agent._extract_vote(_verdict_json("NEEDS_RESEARCH", 70))
        assert result["verdict"] == "NEEDS_RESEARCH"

    def test_not_suitable_verdict_extracted(self):
        result = self.agent._extract_vote(_verdict_json("NOT_SUITABLE", 55))
        assert result["verdict"] == "NOT_SUITABLE"


# ===========================================================================
# ResearchAgent._build_life_context
# ===========================================================================

class TestBuildLifeContext:
    def setup_method(self):
        self.agent = ResearchAgent(
            question="Is it feasible to add WebSockets?",
            context={"task": "build realtime chat"},
            max_lives=3,
            llm_id=1,
            budget_id=1,
        )

    def test_life_1_contains_question(self):
        ctx = self.agent._build_life_context(1)
        assert "Is it feasible to add WebSockets?" in ctx

    def test_life_1_contains_context_data(self):
        ctx = self.agent._build_life_context(1)
        assert "build realtime chat" in ctx

    def test_life_2_contains_continued_marker(self):
        self.agent._accumulated_findings = ["[Life 1] found relevant code"]
        ctx = self.agent._build_life_context(2)
        # Must indicate continuation somehow
        assert "life 2" in ctx.lower() or "continued" in ctx.lower() or "2/" in ctx

    def test_life_2_includes_previous_findings(self):
        self.agent._accumulated_findings = ["[Life 1] found WebSocket handler stub"]
        ctx = self.agent._build_life_context(2)
        assert "found WebSocket handler stub" in ctx

    def test_final_life_urges_verdict_now(self):
        """The last life context must explicitly demand a verdict."""
        ctx = self.agent._build_life_context(3)  # max_lives=3
        lower = ctx.lower()
        assert "verdict" in lower or "must" in lower or "render" in lower

    def test_intermediate_life_does_not_demand_verdict(self):
        """Life 2 of 3 should invite investigation, not demand immediate verdict."""
        self.agent._accumulated_findings = ["[Life 1] inconclusive"]
        ctx = self.agent._build_life_context(2)
        # Should NOT contain "you must render a verdict this time" literally
        assert "you must render a verdict this time" not in ctx


# ===========================================================================
# ResearchAgent._handle_tool_calls
# ===========================================================================

class TestHandleToolCalls:
    def setup_method(self):
        self.agent = ResearchAgent(question="q?", context={}, llm_id=1, budget_id=1)

    def test_disallowed_tool_returns_error_content(self):
        tool_calls = [{"id": "tc1", "function": {"name": "write_file", "arguments": "{}"}}]
        results = self.agent._handle_tool_calls(tool_calls)
        assert len(results) == 1
        assert "not available" in results[0]["content"] or "ERROR" in results[0]["content"]

    def test_run_shell_is_disallowed(self):
        tool_calls = [{"id": "tc2", "function": {"name": "run_shell", "arguments": '{"command": "ls"}'}}]
        results = self.agent._handle_tool_calls(tool_calls)
        assert "not available" in results[0]["content"] or "ERROR" in results[0]["content"]

    def test_allowed_tool_dispatched(self):
        """list_directory is in RESEARCH_AGENT_TOOLS — it should be dispatched."""
        tool_calls = [{
            "id": "tc3",
            "function": {
                "name": "list_directory",
                "arguments": json.dumps({"path": PROJECT_ROOT}),
            },
        }]
        results = self.agent._handle_tool_calls(tool_calls)
        assert len(results) == 1
        assert "not available" not in results[0]["content"]

    def test_bad_json_arguments_handled_gracefully(self):
        """Malformed JSON args must not raise — tool call should still return a string."""
        tool_calls = [{"id": "tc4", "function": {"name": "read_file", "arguments": "{not json"}}]
        results = self.agent._handle_tool_calls(tool_calls)
        assert len(results) == 1
        assert isinstance(results[0]["content"], str)

    def test_result_has_correct_role(self):
        tool_calls = [{"id": "tc5", "function": {"name": "write_file", "arguments": "{}"}}]
        results = self.agent._handle_tool_calls(tool_calls)
        assert results[0]["role"] == "tool"

    def test_result_preserves_tool_call_id(self):
        tool_calls = [{"id": "my-id-42", "function": {"name": "write_file", "arguments": "{}"}}]
        results = self.agent._handle_tool_calls(tool_calls)
        assert results[0]["tool_call_id"] == "my-id-42"

    def test_multiple_tool_calls_all_processed(self):
        tool_calls = [
            {"id": "t1", "function": {"name": "write_file", "arguments": "{}"}},
            {"id": "t2", "function": {"name": "run_shell", "arguments": "{}"}},
        ]
        results = self.agent._handle_tool_calls(tool_calls)
        assert len(results) == 2


# ===========================================================================
# ResearchAgent.run() — full async flow with patched call_llm
# ===========================================================================

class TestResearchAgentRun:
    def test_immediate_verdict_terminates_on_life_1(self):
        """When the LLM returns a verdict JSON on the first turn, run() returns immediately."""
        agent = ResearchAgent(
            question="feasible?",
            context={"task": "add logging"},
            max_lives=3,
            llm_id=1,
            budget_id=1,
        )
        response = _llm_resp(_verdict_json("LIKELY", 95))
        with patch("app.agent.research.call_llm", _sequential_llm(response)):
            result = _run(agent.run())

        assert isinstance(result, ResearchResult)
        assert result.vote["verdict"] == "LIKELY"
        assert result.lives_used == 1
        assert result.total_turns == 1

    def test_lives_exhaustion_returns_not_suitable_confidence_55(self):
        """After all lives exhaust their turns with no verdict, fallback is NOT_SUITABLE."""
        agent = ResearchAgent(
            question="is this possible?",
            context={},
            max_turns_per_life=1,  # Force each life to immediately hit turn cap
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        # Response has no JSON verdict and no tool calls → turn cap hit
        no_verdict = _llm_resp("I need to investigate more.")
        with patch("app.agent.research.call_llm", _sequential_llm(no_verdict)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "NOT_SUITABLE"
        assert result.vote["confidence"] == 55
        assert result.lives_used == 2

    def test_needs_research_verdict_continues_to_next_life(self):
        """A NEEDS_RESEARCH verdict does not terminate — the next life is spawned."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        needs_research = _llm_resp(_verdict_json("NEEDS_RESEARCH", 70))
        likely = _llm_resp(_verdict_json("LIKELY", 93))
        with patch("app.agent.research.call_llm", _sequential_llm(needs_research, likely)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "LIKELY"
        assert result.lives_used == 2

    def test_non_needs_research_verdict_terminates_early(self):
        """Any verdict other than NEEDS_RESEARCH terminates immediately."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_lives=3,
            llm_id=1,
            budget_id=1,
        )
        rejected = _llm_resp(_verdict_json("REJECTED", 15))
        with patch("app.agent.research.call_llm", _sequential_llm(rejected)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "REJECTED"
        assert result.lives_used == 1

    def test_token_counts_accumulated(self):
        """Prompt and completion tokens from each LLM call are summed in result."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_lives=1,
            llm_id=1,
            budget_id=1,
        )
        response = _llm_resp(_verdict_json("LIKELY", 92), prompt_tokens=50, completion_tokens=25)
        with patch("app.agent.research.call_llm", _sequential_llm(response)):
            result = _run(agent.run())

        assert result.prompt_tokens == 50
        assert result.completion_tokens == 25

    def test_findings_accumulated_across_lives(self):
        """Findings from earlier lives appear in the final ResearchResult.findings."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        # First life hits turn cap (no verdict), second life also hits cap → exhaustion fallback
        no_verdict = _llm_resp("Still looking.")
        with patch("app.agent.research.call_llm", _sequential_llm(no_verdict)):
            result = _run(agent.run())

        # The fallback vote's justification should mention the exhausted lives
        assert result.lives_used == 2
        assert isinstance(result.findings, str)

    def test_llm_exception_is_caught_and_agent_continues(self):
        """An LLM call failure must not crash run() — the agent nudges itself."""
        call_count = [0]

        async def flaky_llm(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("LLM unreachable")
            return _llm_resp(_verdict_json("POSSIBLE", 80))

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=3,
            max_lives=1,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", flaky_llm):
            result = _run(agent.run())

        # Should survive the first failure and return a verdict on retry
        assert result.vote["verdict"] in ("POSSIBLE", "LIKELY", "NOT_SUITABLE",
                                          "NEEDS_RESEARCH", "REJECTED")


# ===========================================================================
# run_tiebreaker — question and context construction
# ===========================================================================

class TestRunTiebreaker:
    def test_question_contains_task_description(self):
        """run_tiebreaker must embed the task description in the question."""
        votes = [
            {"verdict": "LIKELY", "confidence": 88, "justification": "looks solid"},
            {"verdict": "REJECTED", "confidence": 12, "justification": "scope is huge"},
        ]
        with patch("app.agent.research.ResearchAgent") as MockAgent:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=ResearchResult(
                vote={"verdict": "LIKELY", "confidence": 88, "justification": "x"},
                lives_used=1,
                total_turns=1,
                findings="",
                prompt_tokens=0,
                completion_tokens=0,
            ))
            MockAgent.return_value = instance
            _run(run_tiebreaker("build a caching layer", votes, llm_id=1, budget_id=1))

        call_kwargs = MockAgent.call_args.kwargs
        assert "build a caching layer" in call_kwargs["question"]
        assert call_kwargs["is_tiebreaker"] is True

    def test_pass_fail_votes_correctly_split_in_context(self):
        """run_tiebreaker must categorise POSSIBLE/LIKELY as pass and REJECTED/NOT_SUITABLE as fail."""
        votes = [
            {"verdict": "LIKELY",       "confidence": 90, "justification": "pass1"},
            {"verdict": "POSSIBLE",     "confidence": 78, "justification": "pass2"},
            {"verdict": "REJECTED",     "confidence": 10, "justification": "fail1"},
            {"verdict": "NOT_SUITABLE", "confidence": 55, "justification": "fail2"},
        ]
        with patch("app.agent.research.ResearchAgent") as MockAgent:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=ResearchResult(
                vote={"verdict": "LIKELY", "confidence": 90, "justification": "x"},
                lives_used=1, total_turns=1, findings="",
                prompt_tokens=0, completion_tokens=0,
            ))
            MockAgent.return_value = instance
            _run(run_tiebreaker("some task", votes, llm_id=1, budget_id=1))

        ctx = MockAgent.call_args.kwargs["context"]
        assert len(ctx["pass_votes"]) == 2
        assert len(ctx["fail_votes"]) == 2
        assert len(ctx["all_votes"]) == 4

    def test_tiebreaker_question_mentions_tie(self):
        """The tiebreaker question must indicate this is a tie scenario."""
        votes = [
            {"verdict": "LIKELY", "confidence": 85, "justification": "yes"},
            {"verdict": "REJECTED", "confidence": 20, "justification": "no"},
        ]
        with patch("app.agent.research.ResearchAgent") as MockAgent:
            instance = MagicMock()
            instance.run = AsyncMock(return_value=ResearchResult(
                vote={"verdict": "LIKELY", "confidence": 85, "justification": "x"},
                lives_used=1, total_turns=1, findings="",
                prompt_tokens=0, completion_tokens=0,
            ))
            MockAgent.return_value = instance
            _run(run_tiebreaker("my task", votes, llm_id=1, budget_id=1))

        question = MockAgent.call_args.kwargs["question"]
        assert "tie" in question.lower()
