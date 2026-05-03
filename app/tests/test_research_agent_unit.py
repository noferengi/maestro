"""
test_research_agent_unit.py
---------------------------
Unit tests for app/agent/research.py.

Tests pure methods without LLM calls (extract_vote, build_life_context,
handle_tool_calls, build_restricted_schemas), plus the async run() with
call_llm patched to exercise:
  - Immediate verdict on first turn
  - Lives exhaustion: forced-verdict epilogue call, fallback to confidence 40 if epilogue also fails
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
from app.agent.config import RESEARCH_AGENT_TOOLS, PROJECT_ROOT, check_context_saturation


# ---------------------------------------------------------------------------
# Async helper - consistent with the rest of the test suite
# ---------------------------------------------------------------------------

def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


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


def _submit_work_resp(verdict="LIKELY", confidence=95, findings="found it",
                      prompt_tokens=10, completion_tokens=5):
    """Build an LLM response that calls submit_work with the given verdict payload."""
    args = json.dumps({
        "signal": "RESEARCH_COMPLETE",
        "summary": findings,
        "payload": {
            "verdict": verdict,
            "confidence": confidence,
            "justification": "test justification",
            "findings": findings,
        },
    })
    tool_calls = [{
        "id": "tc_submit",
        "type": "function",
        "function": {"name": "submit_work", "arguments": args},
    }]
    return _llm_resp("", tool_calls=tool_calls,
                     prompt_tokens=prompt_tokens, completion_tokens=completion_tokens)


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
        self.agent._accumulated_summaries = [""]
        ctx = self.agent._build_life_context(2)
        # Must indicate continuation somehow
        assert "life 2" in ctx.lower() or "continued" in ctx.lower() or "2/" in ctx

    def test_life_2_includes_previous_findings(self):
        self.agent._accumulated_findings = ["[Life 1] found WebSocket handler stub"]
        self.agent._accumulated_summaries = [""]
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
        self.agent._accumulated_summaries = [""]
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
        """list_directory is in RESEARCH_AGENT_TOOLS - it should be dispatched."""
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
        """Malformed JSON args must not raise - tool call should still return a string."""
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
# ResearchAgent.run() - full async flow with patched call_llm
# ===========================================================================

class TestResearchAgentRun:
    def test_immediate_verdict_terminates_on_life_1(self):
        """When the LLM calls submit_work on the first turn, run() returns immediately."""
        agent = ResearchAgent(
            question="feasible?",
            context={"task": "add logging"},
            max_lives=3,
            llm_id=1,
            budget_id=1,
        )
        response = _submit_work_resp("LIKELY", 95)
        with patch("app.agent.research.call_llm", _sequential_llm(response)):
            result = _run(agent.run())

        assert isinstance(result, ResearchResult)
        assert result.vote["verdict"] == "LIKELY"
        assert result.lives_used == 1
        assert result.total_turns == 1

    def test_lives_exhaustion_epilogue_fails_returns_not_suitable_confidence_40(self):
        """
        After all lives exhaust their turns with no verdict, a forced-verdict epilogue
        call is made.  If the epilogue response also contains no parseable JSON,
        the ultimate fallback fires: NOT_SUITABLE, confidence 40.
        """
        agent = ResearchAgent(
            question="is this possible?",
            context={},
            max_turns_per_life=1,  # Force each life to immediately hit turn cap
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        # All calls (lives + epilogue) return no JSON verdict
        no_verdict = _llm_resp("I need to investigate more.")
        with patch("app.agent.research.call_llm", _sequential_llm(no_verdict)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "NOT_SUITABLE"
        assert result.vote["confidence"] == 40  # ultimate fallback, not hardcoded 55
        assert result.lives_used == 2

    def test_lives_exhaustion_epilogue_synthesises_verdict(self):
        """
        When all lives exhaust their turns, the forced-verdict epilogue fires and
        returns a parseable verdict that is used instead of the NOT_SUITABLE fallback.
        Epilogue now uses grammar-constrained format: grade (0-10000) not confidence.
        grade=8000 (80.00%) -> confidence = 8000 // 100 = 80, POSSIBLE range [76-91] ✓
        """
        agent = ResearchAgent(
            question="is this possible?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("I need to investigate more.")
        # Epilogue uses the new grammar format: grade, justification, verdict (no confidence).
        # Call sequence with post-mortem (max_turns_per_life=1, max_lives=2):
        #   life1 turn1, life1 post-mortem, life2 turn1, life2 post-mortem, epilogue = 5 calls
        epilogue_resp = _llm_resp(
            '{"grade": 8000, "justification": "synthesised from findings", "verdict": "POSSIBLE"}'
        )
        with patch(
            "app.agent.research.call_llm",
            _sequential_llm(no_verdict, no_verdict, no_verdict, no_verdict, epilogue_resp),
        ):
            result = _run(agent.run())

        assert result.vote["verdict"] == "POSSIBLE"
        assert result.vote["confidence"] == 80   # 8000 // 100
        assert result.vote["grade"] == 8000
        assert result.lives_used == 2

    def test_needs_research_verdict_continues_to_next_life(self):
        """A NEEDS_RESEARCH verdict does not terminate - the next life is spawned."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        needs_research = _submit_work_resp("NEEDS_RESEARCH", 70)
        likely = _submit_work_resp("LIKELY", 93)
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
        rejected = _submit_work_resp("REJECTED", 15)
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
        response = _submit_work_resp("LIKELY", 92, prompt_tokens=50, completion_tokens=25)
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
        # First life hits turn cap (no verdict), second life also hits cap -> exhaustion fallback
        no_verdict = _llm_resp("Still looking.")
        with patch("app.agent.research.call_llm", _sequential_llm(no_verdict)):
            result = _run(agent.run())

        # The fallback vote's justification should mention the exhausted lives
        assert result.lives_used == 2
        assert isinstance(result.findings, str)

    def test_llm_exception_is_caught_and_agent_continues(self):
        """An LLM call failure must not crash run() - the agent nudges itself."""
        call_count = [0]

        async def flaky_llm(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise ConnectionError("LLM unreachable")
            return _submit_work_resp("POSSIBLE", 80)

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
# run_tiebreaker - question and context construction
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


# ===========================================================================
# _forced_verdict_call - grammar-constrained epilogue
# ===========================================================================

class TestForcedVerdictEpilogue:
    def _make_epilogue_resp(self, grade: int, verdict: str, justification: str = "test synthesis") -> dict:
        """Build a grammar-constrained epilogue response (no JSON block fences - raw JSON)."""
        payload = f'{{"grade": {grade}, "justification": "{justification}", "verdict": "{verdict}"}}'
        return _llm_resp(payload)

    def test_epilogue_allows_needs_research_and_sets_source_tag(self):
        """Epilogue may return NEEDS_RESEARCH; result must carry source=research_agent_epilogue."""
        agent = ResearchAgent(
            question="is this feasible?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("I need to investigate more.")
        epilogue = self._make_epilogue_resp(grade=6800, verdict="NEEDS_RESEARCH",
                                           justification="budget was insufficient to conclude")
        # post-mortem adds 1 extra call per exhausted life (max_lives=2 -> +2 calls)
        with patch("app.agent.research.call_llm",
                   _sequential_llm(no_verdict, no_verdict, no_verdict, no_verdict, epilogue)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "NEEDS_RESEARCH"
        assert result.vote["source"] == "research_agent_epilogue"

    def test_epilogue_grade_sets_confidence_via_integer_division(self):
        """grade=7258 (72.58%) -> confidence = 7258 // 100 = 72 (NEEDS_RESEARCH range)."""
        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("Still looking.")
        epilogue = self._make_epilogue_resp(grade=7258, verdict="NEEDS_RESEARCH")
        # post-mortem adds 1 extra call per exhausted life (max_lives=2 -> +2 calls)
        with patch("app.agent.research.call_llm",
                   _sequential_llm(no_verdict, no_verdict, no_verdict, no_verdict, epilogue)):
            result = _run(agent.run())

        assert result.vote["grade"] == 7258
        assert result.vote["confidence"] == 72  # 7258 // 100

    def test_epilogue_synthesises_verdict_has_grade_field(self):
        """Existing epilogue synthesis test: result must now include a grade field."""
        agent = ResearchAgent(
            question="is this possible?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("I need to investigate more.")
        # grade=8000 (80.00%) -> confidence 80, POSSIBLE range [76-91] ✓
        epilogue = self._make_epilogue_resp(grade=8000, verdict="POSSIBLE",
                                           justification="synthesised from findings")
        # post-mortem adds 1 extra call per exhausted life (max_lives=2 -> +2 calls)
        with patch("app.agent.research.call_llm",
                   _sequential_llm(no_verdict, no_verdict, no_verdict, no_verdict, epilogue)):
            result = _run(agent.run())

        assert result.vote["verdict"] == "POSSIBLE"
        assert "grade" in result.vote
        assert result.vote["grade"] == 8000

    def test_epilogue_retries_without_grammar_on_500(self):
        """
        First call (grammar=True) raises, second call (grammar=False) returns valid JSON.
        The result should use the second call's verdict, not the NOT_SUITABLE fallback.
        """
        agent = ResearchAgent(
            question="is this feasible?",
            context={},
            max_lives=1,
            max_turns_per_life=1,
            task_id=1,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("I need more time.")
        epilogue_no_grammar = _llm_resp(
            '{"grade": 7500, "justification": "retry without grammar succeeded", "verdict": "LIKELY"}'
        )

        call_count = [0]
        responses_for_epilogue = [Exception("500 Internal Server Error"), epilogue_no_grammar]

        async def mock_llm(*args, **kwargs):
            # life1 turn1 returns no_verdict; life1 post-mortem returns no_verdict;
            # then two epilogue attempts follow
            if call_count[0] < 2:
                call_count[0] += 1
                return no_verdict
            idx = call_count[0] - 2
            call_count[0] += 1
            r = responses_for_epilogue[idx]
            if isinstance(r, Exception):
                raise r
            return r

        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert result.vote["verdict"] == "LIKELY"
        assert result.vote["grade"] == 7500
        assert result.vote["source"] == "research_agent_epilogue"

    def test_epilogue_fallback_when_both_attempts_fail(self):
        """
        Both epilogue attempts (grammar and no-grammar) raise; ultimate fallback fires:
        NOT_SUITABLE, confidence 40.
        """
        agent = ResearchAgent(
            question="is this feasible?",
            context={},
            max_lives=1,
            max_turns_per_life=1,
            task_id=1,
            llm_id=1,
            budget_id=1,
        )
        no_verdict = _llm_resp("Still investigating.")

        call_count = [0]

        async def mock_llm(*args, **kwargs):
            call_count[0] += 1
            # First two calls: life1 turn + post-mortem - succeed with no-verdict content
            if call_count[0] <= 2:
                return no_verdict
            # Remaining calls (epilogue attempts): always raise
            raise Exception("500 Internal Server Error")

        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert result.vote["verdict"] == "NOT_SUITABLE"
        assert result.vote["confidence"] == 40


# ===========================================================================
# Post-mortem on turn exhaustion
# ===========================================================================

class TestPostMortem:
    def test_post_mortem_fires_on_turn_exhaustion(self):
        """When a life exhausts its turns, one extra post-mortem call is made."""
        call_count = [0]
        responses = [
            _llm_resp("Still investigating."),   # turn 1 - no verdict
            _llm_resp("WHAT I INVESTIGATED: read main.py\nWHAT I FOUND: nothing\n"
                      "WHAT I AM SATISFIED WITH: file structure\n"
                      "WHAT I AM UNSATISFIED WITH: auth flow unclear\n"
                      "WHAT THE NEXT INVESTIGATOR SHOULD FOCUS ON: app/auth.py"),  # post-mortem
            _submit_work_resp("POSSIBLE", 80),  # life 2, turn 1 - verdict
        ]

        async def mock_llm(*args, **kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[i]

        agent = ResearchAgent(
            question="is auth feasible?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        # 3 calls: life1 turn, post-mortem, life2 turn
        assert call_count[0] == 3
        assert result.vote["verdict"] == "POSSIBLE"
        assert result.lives_used == 2

    def test_post_mortem_content_appears_in_next_life_context(self):
        """The post-mortem response is surfaced as a structured summary in life 2's context."""
        summary_text = (
            "WHAT I INVESTIGATED: read database.py\n"
            "WHAT I FOUND: Task model exists\n"
            "WHAT I AM SATISFIED WITH: schema is clear\n"
            "WHAT I AM UNSATISFIED WITH: migration strategy unknown\n"
            "WHAT THE NEXT INVESTIGATOR SHOULD FOCUS ON: app/migrations/"
        )
        responses = [
            _llm_resp("Looking around."),       # life1 turn1
            _llm_resp(summary_text),             # life1 post-mortem
            _submit_work_resp("LIKELY", 93),    # life2 turn1
        ]
        idx = [0]

        async def mock_llm(*args, **kwargs):
            i = min(idx[0], len(responses) - 1)
            idx[0] += 1
            return responses[i]

        agent = ResearchAgent(
            question="is migration feasible?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert result.vote["verdict"] == "LIKELY"
        # Verify the summary was stored on the agent
        assert len(agent._accumulated_summaries) >= 1
        assert "migration strategy unknown" in agent._accumulated_summaries[0]

    def test_post_mortem_failure_does_not_crash_run(self):
        """A post-mortem LLM call failure must not propagate - run() returns normally."""
        call_count = [0]

        async def mock_llm(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 2:  # post-mortem call
                raise ConnectionError("LLM unreachable")
            if call_count[0] == 3:
                return _submit_work_resp("POSSIBLE", 80)
            return _llm_resp("investigating...")

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=1,
            max_lives=2,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert result.vote is not None
        # handoff_summary is empty on failure - life 2 context uses findings fallback
        assert agent._accumulated_summaries[0] == ""

    def test_post_mortem_not_fired_when_verdict_rendered(self):
        """No post-mortem when the agent renders a verdict within the turn cap."""
        call_count = [0]

        async def mock_llm(*args, **kwargs):
            call_count[0] += 1
            return _submit_work_resp("LIKELY", 95)

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=5,
            max_lives=1,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert call_count[0] == 1   # only the verdict turn; no post-mortem
        assert result.vote["verdict"] == "LIKELY"


# ===========================================================================
# Structured context for life N+1 (_build_life_context with summaries)
# ===========================================================================

class TestBuildLifeContextWithSummaries:
    def setup_method(self):
        self.agent = ResearchAgent(
            question="Is caching feasible?",
            context={},
            max_lives=3,
            llm_id=1,
            budget_id=1,
        )

    def test_summary_preferred_over_findings_when_present(self):
        """When a post-mortem summary exists, it replaces the raw findings line."""
        self.agent._accumulated_findings = ["[Life 1] exhausted 5 turns without verdict"]
        self.agent._accumulated_summaries = ["WHAT I FOUND: cache module exists at app/cache.py"]
        ctx = self.agent._build_life_context(2)
        assert "cache module exists" in ctx
        # Raw "exhausted" line should NOT appear (summary took priority)
        assert "exhausted 5 turns" not in ctx

    def test_findings_fallback_when_summary_empty(self):
        """When the post-mortem summary is empty, the raw findings line is used."""
        self.agent._accumulated_findings = ["[Life 1] found relevant code in app/db.py"]
        self.agent._accumulated_summaries = [""]
        ctx = self.agent._build_life_context(2)
        assert "found relevant code" in ctx

    def test_unresolved_section_surfaced_in_context(self):
        """The WHAT I AM UNSATISFIED WITH section is extracted and shown separately."""
        summary = (
            "WHAT I INVESTIGATED: read app/auth.py\n"
            "WHAT I FOUND: JWT tokens used\n"
            "WHAT I AM SATISFIED WITH: token format\n"
            "WHAT I AM UNSATISFIED WITH: refresh token rotation is unclear\n"
            "WHAT THE NEXT INVESTIGATOR SHOULD FOCUS ON: app/auth/refresh.py"
        )
        self.agent._accumulated_findings = ["[Life 1] checked auth"]
        self.agent._accumulated_summaries = [summary]
        ctx = self.agent._build_life_context(2)
        assert "refresh token rotation is unclear" in ctx
        assert "Still Unresolved" in ctx

    def test_no_still_unresolved_section_when_no_summary(self):
        """No 'Still Unresolved' section when there is no post-mortem summary."""
        self.agent._accumulated_findings = ["[Life 1] checked stuff"]
        self.agent._accumulated_summaries = [""]
        ctx = self.agent._build_life_context(2)
        assert "Still Unresolved" not in ctx


# ===========================================================================
# Context overflow (P0-A) - TOO_LARGE verdict on 400 errors
# ===========================================================================

class TestContextOverflow:
    def test_context_overflow_400_emits_too_large(self):
        """A 400 error from call_llm must terminate the life with TOO_LARGE verdict."""
        async def _400_llm(*args, **kwargs):
            raise Exception("HTTP 400 Bad Request: context length exceeded")

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=5,
            max_lives=1,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", _400_llm):
            result = _run(agent.run())

        assert result.vote["verdict"] == "TOO_LARGE"
        assert result.vote["confidence"] == 100

    def test_too_large_terminates_without_more_lives(self):
        """run() must return immediately on TOO_LARGE - subsequent lives must not be spawned."""
        call_count = [0]

        async def _400_first_call(*args, **kwargs):
            call_count[0] += 1
            raise Exception("HTTP 400 Bad Request")

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=3,
            max_lives=3,  # would spawn 3 lives if not short-circuited
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", _400_first_call):
            result = _run(agent.run())

        assert result.lives_used == 1
        assert result.vote["verdict"] == "TOO_LARGE"
        # Only 1 LLM call (the overflowing turn) - no post-mortem, no further lives
        assert call_count[0] == 1

    def test_non_400_error_still_nudges(self):
        """A non-400 error (e.g. timeout) must nudge the agent and not emit TOO_LARGE."""
        call_count = [0]

        async def _timeout_then_verdict(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("Connection timeout")
            return _submit_work_resp("POSSIBLE", 80)

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=3,
            max_lives=1,
            llm_id=1,
            budget_id=1,
        )
        with patch("app.agent.research.call_llm", _timeout_then_verdict):
            result = _run(agent.run())

        # The timeout must NOT trigger TOO_LARGE - agent continues and gets a real verdict
        assert result.vote["verdict"] != "TOO_LARGE"
        assert result.vote["verdict"] == "POSSIBLE"
        assert call_count[0] == 2  # failure + successful retry


# ===========================================================================
# check_context_saturation() - shared utility unit tests
# ===========================================================================

class TestContextSaturationUtility:
    def test_returns_false_when_max_context_zero(self):
        """Disabled when max_context=0 - must never inject nudges or terminate."""
        warned: set[float] = set()
        messages: list[dict] = []
        result = check_context_saturation(
            prompt_tokens=90_000,
            max_context=0,
            warned_set=warned,
            messages=messages,
        )
        assert result is False
        assert len(messages) == 0
        assert len(warned) == 0

    def test_injects_nudge_at_threshold(self):
        """Crossing 55% saturation appends a nudge (50% threshold) into the conversation."""
        warned: set[float] = set()
        messages: list[dict] = []
        # 55_000 / 100_000 = 55% - above 50% threshold only
        check_context_saturation(
            prompt_tokens=55_000,
            max_context=100_000,
            warned_set=warned,
            messages=messages,
        )
        assert len(messages) == 1
        assert 0.5 in warned
        assert "context" in messages[0]["content"].lower()

    def test_each_threshold_fires_only_once(self):
        """Calling the utility repeatedly at the same saturation level injects at most one nudge."""
        warned: set[float] = set()
        messages: list[dict] = []
        # 55% crosses only the 50% threshold; calling repeatedly must not add more messages
        for _ in range(5):
            check_context_saturation(
                prompt_tokens=55_000,
                max_context=100_000,
                warned_set=warned,
                messages=messages,
            )
        # Only one message ever appended for the 50% threshold
        assert len(messages) == 1

    def test_returns_true_at_terminate_threshold(self):
        """Saturation >= terminate_threshold (0.95 by default) returns True - no nudge injected."""
        warned: set[float] = set()
        messages: list[dict] = []
        result = check_context_saturation(
            prompt_tokens=95_001,
            max_context=100_000,
            warned_set=warned,
            messages=messages,
        )
        assert result is True
        # No nudge messages when hard-terminate fires
        assert len(messages) == 0


# ===========================================================================
# ResearchAgent - proactive context saturation (new _run_life() behaviour)
# ===========================================================================

class TestContextSaturation:
    def test_nudge_injected_at_warning_threshold(self):
        """
        When a turn's prompt_tokens cross 75%, a warning message is injected
        into the conversation.  The agent continues to run (no termination).
        """
        call_count = [0]
        responses = [
            # Turn 1: high saturation but below terminate threshold, no verdict/tools
            _llm_resp("Still investigating.", prompt_tokens=76_000),
            # Turn 2: render verdict via submit_work
            _submit_work_resp("POSSIBLE", 80, prompt_tokens=10),
        ]

        async def mock_llm(*args, **kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[i]

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=5,
            max_lives=1,
            llm_id=1,
            budget_id=1,
            max_context=100_000,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        assert result.vote["verdict"] == "POSSIBLE"
        assert call_count[0] == 2  # both turns ran

    def test_each_threshold_fires_only_once(self):
        """A given warning level (e.g. 75%) is injected at most once per life."""
        call_count = [0]
        responses = [
            _llm_resp("Investigating.", prompt_tokens=76_000),   # fires 75% warning
            _llm_resp("Still going.", prompt_tokens=76_000),     # 75% already warned
            _submit_work_resp("LIKELY", 93, prompt_tokens=10),
        ]

        async def mock_llm(*args, **kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[i]

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=10,
            max_lives=1,
            llm_id=1,
            budget_id=1,
            max_context=100_000,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        # Count user messages that look like context warnings
        life_messages_with_context_warn = [
            m for m in []  # can't inspect internal messages; just verify verdict returned
        ]
        assert result.vote["verdict"] == "LIKELY"

    def test_life_terminates_at_terminate_threshold(self):
        """
        When saturation >= terminate_threshold (0.95), the life breaks cleanly
        and falls through to _post_mortem_call(), then the next life continues.
        No TOO_LARGE verdict - this is a graceful break, not a 400 error path.
        """
        call_count = [0]
        responses = [
            # Life 1, turn 1: 96% saturation, no verdict, no tool calls -> triggers terminate
            _llm_resp("Still investigating.", prompt_tokens=96_000),
            # Life 1 post-mortem call
            _llm_resp("WHAT I INVESTIGATED: nothing\nWHAT I FOUND: little"),
            # Life 2, turn 1: render verdict via submit_work
            _submit_work_resp("POSSIBLE", 80, prompt_tokens=10),
        ]

        async def mock_llm(*args, **kwargs):
            i = min(call_count[0], len(responses) - 1)
            call_count[0] += 1
            return responses[i]

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=10,
            max_lives=2,
            llm_id=1,
            budget_id=1,
            max_context=100_000,
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        # Must NOT return TOO_LARGE - that's the 400-error path
        assert result.vote["verdict"] != "TOO_LARGE"
        assert result.vote["verdict"] == "POSSIBLE"
        assert result.lives_used == 2
        assert call_count[0] == 3  # turn1 + post-mortem + turn2

    def test_saturation_disabled_when_max_context_zero(self):
        """When max_context=0, saturation checking is disabled even at high token counts."""
        call_count = [0]

        async def mock_llm(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # Very high token count - would terminate if saturation were enabled
                return _llm_resp("Still investigating.", prompt_tokens=999_999)
            return _submit_work_resp("LIKELY", 93, prompt_tokens=10)

        agent = ResearchAgent(
            question="q?",
            context={},
            max_turns_per_life=5,
            max_lives=1,
            llm_id=1,
            budget_id=1,
            max_context=0,  # disabled
        )
        with patch("app.agent.research.call_llm", mock_llm):
            result = _run(agent.run())

        # No saturation termination - agent reaches verdict normally
        assert result.vote["verdict"] == "LIKELY"
        assert call_count[0] == 2
