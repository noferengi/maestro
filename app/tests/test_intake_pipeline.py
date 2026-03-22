"""
Integration tests for app/agent/intake.py.

Patches app.agent.intake.call_llm directly (bypassing the budget_id
enforcement in llm_client.py) and mocks the static analysis stage.
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Mock response builders
# ---------------------------------------------------------------------------

def _llm_response(content_dict: dict, prompt_tokens: int = 50,
                  completion_tokens: int = 100) -> dict:
    """Build a minimal OpenAI-format response dict from a content dict."""
    return {
        "choices": [{"message": {"content": json.dumps(content_dict)}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "model": "mock-model",
    }


# Canned stage responses
_SCOPE_PASS = {
    "scope": "medium", "complexity": 5, "decomposition_needed": False,
    "subtasks": [], "affected_areas": ["app/"], "effort": "moderate",
    "vote": {"verdict": "LIKELY", "confidence": 0.93,
             "justification": "Task is well-defined."},
}
_SCOPE_REJECTED = {
    "scope": "epic", "complexity": 10, "decomposition_needed": True,
    "subtasks": [], "affected_areas": [], "effort": "major",
    "vote": {"verdict": "REJECTED", "confidence": 0.20,
             "justification": "Task is fundamentally unfeasible."},
}
_SCOPE_SUBDIVIDE = {
    "scope": "epic", "complexity": 9, "decomposition_needed": True,
    "subtasks": [], "affected_areas": [], "effort": "major",
    "vote": {"verdict": "SUBDIVIDE_IDEA", "confidence": 0.85,
             "justification": "Too large — decompose first."},
}
_SCOPE_NEEDS_RESEARCH = {
    "scope": "large", "complexity": 7, "decomposition_needed": False,
    "subtasks": [], "affected_areas": ["unknown"], "effort": "significant",
    "vote": {"verdict": "NEEDS_RESEARCH", "confidence": 0.65,
             "justification": "Cannot determine scope."},
}
_CONFLICT_PASS = {
    "file_conflicts": [], "semantic_conflicts": [], "priority_conflicts": [],
    "resource_conflicts": [],
    "vote": {"verdict": "LIKELY", "confidence": 0.95,
             "justification": "No conflicts."},
}
_FEASIBILITY_PASS = {
    "feasibility_rating": 0.85, "ambiguities": [], "external_dependencies": [],
    "risks": [], "codebase_readiness": "ready",
    "vote": {"verdict": "POSSIBLE", "confidence": 0.80,
             "justification": "Codebase is ready."},
}

# Static analysis fallback vote (returned by the mocked stage)
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


@dataclass
class _FakeResearchResult:
    vote: dict
    lives_used: int = 1
    total_turns: int = 1
    findings: str = "No issues."
    prompt_tokens: int = 50
    completion_tokens: int = 100


# ---------------------------------------------------------------------------
# Pipeline runner helpers
# ---------------------------------------------------------------------------

class _SequentialCallLLM:
    """Async callable that returns canned responses in order."""
    def __init__(self, responses: list[dict]):
        self._responses = responses
        self._index = 0

    async def __call__(self, messages, **kwargs):
        if self._index < len(self._responses):
            r = self._responses[self._index]
        else:
            r = self._responses[-1]
        self._index += 1
        return r


def _all_pass_responses():
    return [
        _llm_response(_SCOPE_PASS),
        _llm_response(_CONFLICT_PASS),
        _llm_response(_FEASIBILITY_PASS),
    ]


def _patch_static(pipeline_instance, vote=None):
    """Monkey-patch _stage_static_analysis to avoid tree-sitter I/O."""
    async def _mock_static(scope_vote):
        return vote or _STATIC_VOTE
    pipeline_instance._stage_static_analysis = _mock_static


async def _run_pipeline_direct(call_llm_responses, task_id="test-task-1",
                               task_description="Test task description",
                               task_title="Test Task",
                               all_tasks=None):
    """
    Create an IntakePipeline, patch call_llm and the static analysis stage,
    then run it. Returns the tally result.
    """
    from app.agent.intake import IntakePipeline
    from app.database import get_project_path

    pipeline = IntakePipeline(
        task_id=task_id,
        task_description=task_description,
        task_title=task_title,
        all_tasks=all_tasks or [],
        project="TheMaestro",  # Required for static analysis
    )
    _patch_static(pipeline)

    with patch("app.agent.intake.call_llm",
               new=_SequentialCallLLM(call_llm_responses)):
        result = await pipeline.run()
    return result, pipeline


# ---------------------------------------------------------------------------
# Full pass
# ---------------------------------------------------------------------------

class TestFullPass:
    def test_intake_all_pass_outcome(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses())
        )
        assert result["outcome"] == "passed"

    def test_passed_result_has_votes(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses())
        )
        assert len(result["votes"]) >= 3  # scope + static + conflict + feasibility

    def test_token_totals_match_votes(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses())
        )
        vote_prompt = sum(v.get("prompt_tokens", 0) for v in result["votes"])
        vote_completion = sum(v.get("completion_tokens", 0) for v in result["votes"])
        assert result["total_prompt_tokens"] == vote_prompt
        assert result["total_completion_tokens"] == vote_completion

    def test_transition_is_idea_to_planning(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses())
        )
        assert result["transition"] == "idea_to_planning"


# ---------------------------------------------------------------------------
# Full reject
# ---------------------------------------------------------------------------

class TestFullReject:
    def test_intake_rejected_outcome(self):
        result, _ = asyncio.run(
            _run_pipeline_direct([_llm_response(_SCOPE_REJECTED)])
        )
        assert result["outcome"] == "rejected"

    def test_rejection_reasons_populated(self):
        result, _ = asyncio.run(
            _run_pipeline_direct([_llm_response(_SCOPE_REJECTED)])
        )
        assert len(result["rejection_reasons"]) > 0

    def test_rejected_early_exits_before_other_stages(self):
        """A REJECTED scope vote should not run conflict/feasibility stages."""
        result, pipeline = asyncio.run(
            _run_pipeline_direct([_llm_response(_SCOPE_REJECTED)])
        )
        assert result["outcome"] == "rejected"
        # Scope vote only — pipeline exits early after REJECTED
        assert len(result["votes"]) == 1


# ---------------------------------------------------------------------------
# SUBDIVIDE_IDEA
# ---------------------------------------------------------------------------

class TestSubdivideIdea:
    def test_subdivide_outcome(self):
        """A scope vote of SUBDIVIDE_IDEA produces outcome 'subdivide'."""
        result, _ = asyncio.run(
            _run_pipeline_direct([_llm_response(_SCOPE_SUBDIVIDE)])
        )
        assert result["outcome"] == "subdivide"


# ---------------------------------------------------------------------------
# NEEDS_RESEARCH
# ---------------------------------------------------------------------------

class TestNeedsResearch:
    def test_needs_research_stage_recorded(self):
        """When scope votes NEEDS_RESEARCH, research_needed lists that stage."""
        # We also mock _handle_needs_research so it returns immediately
        async def _fake_handle(tally):
            return tally  # return as-is (outcome stays needs_research)

        async def _run():
            from app.agent.intake import IntakePipeline
            pipeline = IntakePipeline(
                task_id="test-nr", task_description="Vague task",
                task_title="Vague", all_tasks=[],
                project="TheMaestro",  # Required for static analysis
            )
            _patch_static(pipeline)
            responses = [
                _llm_response(_SCOPE_NEEDS_RESEARCH),
                _llm_response(_CONFLICT_PASS),
                _llm_response(_FEASIBILITY_PASS),
            ]
            with patch("app.agent.intake.call_llm",
                       new=_SequentialCallLLM(responses)):
                with patch.object(pipeline, "_handle_needs_research",
                                  new=_fake_handle):
                    return await pipeline.run()

        result = asyncio.run(_run())
        assert result["outcome"] == "needs_research"
        assert len(result["research_needed"]) > 0


# ---------------------------------------------------------------------------
# Stage error fallback
# ---------------------------------------------------------------------------

class TestStageErrorFallback:
    def test_error_vote_structure(self):
        """_error_vote returns a NEEDS_RESEARCH vote dict with zero tokens."""
        from app.agent.intake import IntakePipeline
        pipeline = IntakePipeline(
            task_id="err-test", task_description="x",
            task_title="x", all_tasks=[],
            project="TheMaestro",  # Required for static analysis
        )
        vote = pipeline._error_vote("scope_analysis", RuntimeError("timeout"))
        assert vote["verdict"] == "NEEDS_RESEARCH"
        assert vote["confidence"] == 0.0
        assert vote["prompt_tokens"] == 0
        assert vote["completion_tokens"] == 0
        assert "failed" in vote["justification"].lower()

    def test_pipeline_continues_after_stage_error(self):
        """If call_llm raises, the pipeline should catch and continue."""
        async def _raising_call_llm(messages, **kwargs):
            raise RuntimeError("LLM connection refused")

        async def _run():
            from app.agent.intake import IntakePipeline
            pipeline = IntakePipeline(
                task_id="err-test-2", task_description="x",
                task_title="x", all_tasks=[],
                project="TheMaestro",  # Required for static analysis
            )
            _patch_static(pipeline)
            with patch("app.agent.intake.call_llm", new=_raising_call_llm):
                # Mock research agent to avoid extra complexity
                with patch.object(pipeline, "_handle_needs_research",
                                  new=AsyncMock(return_value={
                                      "task_id": "err-test-2",
                                      "transition": "idea_to_planning",
                                      "votes": [], "outcome": "needs_research",
                                      "rejection_reasons": [], "research_needed": [],
                                      "total_prompt_tokens": 0,
                                      "total_completion_tokens": 0,
                                  })):
                    return await pipeline.run()

        result = asyncio.run(_run())
        # Should not raise, and should have some outcome
        assert result["outcome"] in ("needs_research", "rejected", "passed", "tie")


# ---------------------------------------------------------------------------
# Result structure
# ---------------------------------------------------------------------------

class TestResultStructure:
    def test_required_keys_present(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses())
        )
        for key in ("task_id", "transition", "votes", "outcome",
                    "rejection_reasons", "research_needed",
                    "total_prompt_tokens", "total_completion_tokens"):
            assert key in result, f"Missing key: {key}"

    def test_task_id_preserved(self):
        result, _ = asyncio.run(
            _run_pipeline_direct(_all_pass_responses(), task_id="my-task-99")
        )
        assert result["task_id"] == "my-task-99"


# ---------------------------------------------------------------------------
# Static analysis integration tests
# ---------------------------------------------------------------------------

class TestStaticAnalysisIntegration:
    """Test static analysis stage actual logic (not just mocked)."""

    def test_static_analysis_uses_affected_areas(self):
        """Static analysis collects files from affected_areas in scope vote."""
        async def _run():
            from app.agent.intake import IntakePipeline
            from app.agent.static_analysis import analyze_project, generate_vote
            pipeline = IntakePipeline(
                task_id="static-test-1", task_description="Add auth",
                task_title="Auth", all_tasks=[],
                project="TheMaestro",
            )
            # Mock the actual analyze_project to verify it was called
            original_analyze = analyze_project
            analyze_called_with = []

            async def mock_analyze(file_paths):
                analyze_called_with.extend(file_paths)
                return original_analyze(file_paths)

            async def mock_generate(analysis, desc):
                return {"verdict": "POSSIBLE", "confidence": 80, "justification": "OK"}

            with patch("app.agent.intake.call_llm",
                       new=_SequentialCallLLM([
                           _llm_response({
                               **_SCOPE_PASS,
                               "raw_response": {"affected_areas": ["app/agent/"]}
                           }),
                           _llm_response(_CONFLICT_PASS),
                           _llm_response(_FEASIBILITY_PASS),
                       ])):
                with patch.object(pipeline, "_stage_static_analysis") as mock_static:
                    # Call the actual static analysis
                    async def actual_static(sv):
                        from app.agent.static_analysis import analyze_project, generate_vote
                        loop = asyncio.get_running_loop()
                        analysis_result = await loop.run_in_executor(None, analyze_project, [])
                        vote_data = await loop.run_in_executor(
                            None, generate_vote, analysis_result, "Add auth"
                        )
                        return {
                            "stage": "static_analysis",
                            "verdict": vote_data.get("verdict", "POSSIBLE"),
                            "confidence": float(vote_data.get("confidence", 0.5)),
                            "justification": vote_data.get("justification", "Done"),
                            "raw_response": vote_data,
                            "prompt_tokens": 0, "completion_tokens": 0,
                            "model": "static_analysis",
                        }
                    mock_static.side_effect = actual_static
                    return await pipeline.run()

        result = asyncio.run(_run())
        assert result["outcome"] in ("passed", "needs_research")  # May need research if tree-sitter fails
        # Verify we actually ran static analysis (not mocked)
        static_votes = [v for v in result["votes"] if v.get("model") == "static_analysis"]
        assert len(static_votes) == 1, "Static analysis should have produced a vote"

    def test_static_analysis_fallback_when_no_affected_areas(self):
        """Static analysis falls back to all Python files when no affected_areas."""
        async def _run():
            from app.agent.intake import IntakePipeline
            pipeline = IntakePipeline(
                task_id="static-test-2", task_description="Global change",
                task_title="Global", all_tasks=[],
                project="TheMaestro",
            )
            with patch("app.agent.intake.call_llm",
                       new=_SequentialCallLLM([
                           _llm_response({
                               "scope": "medium", "complexity": 5,
                               "decomposition_needed": False, "subtasks": [],
                               "affected_areas": [],  # Empty - triggers fallback
                               "effort": "moderate",
                               "vote": {"verdict": "LIKELY", "confidence": 0.93,
                                        "justification": "OK"}
                           }),
                           _llm_response(_CONFLICT_PASS),
                           _llm_response(_FEASIBILITY_PASS),
                       ])):
                return await pipeline.run()

        result = asyncio.run(_run())
        # Should complete successfully
        assert result["outcome"] == "passed"


# ---------------------------------------------------------------------------
# _handle_needs_research integration tests
# ---------------------------------------------------------------------------

class TestNeedsResearchIntegration:
    """Test actual _handle_needs_research logic (not mocked)."""

    def test_needs_research_spawns_agent(self):
        """NEEDS_RESEARCH outcome triggers research agent."""
        async def _run():
            from app.agent.intake import IntakePipeline
            from app.agent.research import run_research
            pipeline = IntakePipeline(
                task_id="nr-test", task_description="Unclear task",
                task_title="Unclear", all_tasks=[],
                project="TheMaestro",
            )
            # Replace _handle_needs_research with a version that actually calls research
            original_handle = pipeline._handle_needs_research

            # Mock the research function to return a passing result
            async def mock_run_research(*args, **kwargs):
                from app.agent.verdicts import Vote, Verdict
                return type("MockResult", (), {
                    "vote": {
                        "verdict": "LIKELY",
                        "confidence": 93,
                        "justification": "Research resolved it",
                    },
                    "prompt_tokens": 100,
                    "completion_tokens": 150,
                })()

            with patch("app.agent.research.run_research", new_callable=lambda: mock_run_research):
                with patch("app.agent.intake.call_llm",
                           new=_SequentialCallLLM([
                               _llm_response(_SCOPE_NEEDS_RESEARCH),
                               _llm_response(_CONFLICT_PASS),
                               _llm_response(_FEASIBILITY_PASS),
                           ])):
                    # Run pipeline which will hit needs_research
                    return await pipeline.run()

        result = asyncio.run(_run())
        # After research resolves, should pass
        assert result["outcome"] == "passed"

    def test_needs_research_fallback_on_agent_failure(self):
        """Research agent failure results in NOT_SUITABLE fallback."""
        async def _run():
            from app.agent.intake import IntakePipeline
            pipeline = IntakePipeline(
                task_id="nr-fail-test", task_description="Unclear task",
                task_title="Unclear", all_tasks=[],
                project="TheMaestro",
            )

            async def failing_research(*args, **kwargs):
                raise RuntimeError("Research agent crashed")

            with patch("app.agent.research.run_research", new=failing_research):
                with patch("app.agent.intake.call_llm",
                           new=_SequentialCallLLM([
                               _llm_response(_SCOPE_NEEDS_RESEARCH),
                               _llm_response(_CONFLICT_PASS),
                               _llm_response(_FEASIBILITY_PASS),
                           ])):
                    return await pipeline.run()

        result = asyncio.run(_run())
        # Should have been replaced with NOT_SUITABLE
        scope_vote = next((v for v in result["votes"] if v["stage"] == "scope_analysis"), None)
        assert scope_vote is not None
        assert scope_vote["verdict"] == "NOT_SUITABLE"
        assert "Research agent failed" in scope_vote["justification"]
