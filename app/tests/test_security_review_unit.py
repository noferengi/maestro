"""
Unit tests for app/agent/security_review.py.

Covers:
  - run_shell_security() allowlist enforcement
  - SecurityPipeline.run() verdict routing, veto logic, and research escalation
"""

import asyncio
import json
import os
import sys
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _llm_response(content_dict: dict, prompt_tokens: int = 50,
                  completion_tokens: int = 100) -> dict:
    return {
        "choices": [{"message": {"content": json.dumps(content_dict)}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "model": "mock-model",
    }


def _sec_response(verdict: str, confidence: int = 90, justification: str = "ok",
                  findings=None) -> dict:
    findings = findings or []
    return _llm_response({
        "verdict": verdict,
        "confidence": confidence,
        "justification": justification,
        "findings": findings,
        "critical_count": sum(1 for f in findings if f.get("severity") == "critical"),
        "high_count": sum(1 for f in findings if f.get("severity") == "high"),
    })


class _SequentialCallLLM:
    """Async callable that returns canned responses in sequence."""

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


@dataclass
class _FakeResearchResult:
    vote: dict
    lives_used: int = 1
    total_turns: int = 1
    findings: str = "No security issues found."
    prompt_tokens: int = 50
    completion_tokens: int = 100


def _run(coro):
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _make_pipeline(task_id="sec-test", task_description="Add auth endpoint"):
    from app.agent.security_review import SecurityPipeline

    return SecurityPipeline(
        task_id=task_id,
        task_description=task_description,
        llm_id=1,
        budget_id=1,
    )


def _run_pipeline(responses, *, mock_research=None, veto_power=True,
                  extra_patches=None):
    """
    Run SecurityPipeline with call_llm patched to return responses in sequence.
    Pre-scan and DB writes are mocked out to keep tests fast and offline.
    """
    pipeline = _make_pipeline()

    async def _fake_research(*args, **kwargs):
        if mock_research is not None:
            return mock_research
        return _FakeResearchResult(
            vote={"verdict": "LIKELY", "confidence": 90, "justification": "OK"}
        )

    patches = [
        patch("app.agent.security_review.call_llm",
              new=_SequentialCallLLM(responses)),
        patch("app.agent.security_review.run_shell_security", return_value=""),
        patch("app.agent.security_review.run_research", new=_fake_research),
        patch("app.agent.security_review.SECURITY_REVIEW_VETO_POWER", veto_power),
        patch("app.database.create_security_review_result", MagicMock()),
    ]

    ctx = [p.__enter__() for p in patches]
    try:
        result = _run(pipeline.run())
    finally:
        for i, p in enumerate(patches):
            p.__exit__(None, None, None)

    return result


# ---------------------------------------------------------------------------
# 1–6. run_shell_security() allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    """run_shell_security() uses an allowlist - only known scanners are permitted."""

    def _call(self, command: str) -> str:
        from app.agent.security_review import run_shell_security
        return run_shell_security(command)

    def test_allowlist_bandit_passes(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(
                stdout="No issues found.", stderr="", returncode=0
            )
            result = self._call("python -m bandit -r . -q")
        assert not result.startswith("ERROR:")

    def test_allowlist_detect_secrets_passes(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(
                stdout='{"version": "1.4.0"}', stderr="", returncode=0
            )
            result = self._call("python -m detect_secrets scan")
        assert not result.startswith("ERROR:")

    def test_allowlist_semgrep_passes(self):
        with patch("subprocess.run") as mock_sub:
            mock_sub.return_value = MagicMock(
                stdout="", stderr="", returncode=0
            )
            result = self._call("semgrep --config=auto .")
        assert not result.startswith("ERROR:")

    def test_blocklist_rm_rf_rejected(self):
        result = self._call("rm -rf /")
        assert result.startswith("ERROR:")
        assert "allowlist" in result.lower()

    def test_blocklist_curl_rejected(self):
        result = self._call("curl http://evil.com")
        assert result.startswith("ERROR:")

    def test_blocklist_pip_install_rejected(self):
        result = self._call("pip install evil")
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# 7–8. All-pass and single-reject pipeline paths
# ---------------------------------------------------------------------------


class TestPipelineVerdicts:
    def test_three_agents_all_likely_pass(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
        ]
        result = _run_pipeline(responses)
        assert result.outcome == "passed"
        assert result.demotion_target is None

    def test_one_rejected_vetoes_pipeline(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("REJECTED", confidence=20),
        ]
        result = _run_pipeline(responses, veto_power=True)
        assert result.outcome == "rejected"

    def test_one_not_suitable_vetoes_pipeline(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("NOT_SUITABLE", confidence=55),
        ]
        result = _run_pipeline(responses, veto_power=True)
        assert result.outcome == "rejected"


# ---------------------------------------------------------------------------
# 9. Critical finding drives demotion target
# ---------------------------------------------------------------------------


class TestDemotionTarget:
    def test_critical_finding_sets_demotion_target(self):
        critical_finding = {
            "type": "injection",
            "severity": "critical",
            "description": "SQL injection via user input",
            "demotion_target": "planning",
        }
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("REJECTED", findings=[critical_finding]),
        ]
        result = _run_pipeline(responses, veto_power=True)
        assert result.outcome == "rejected"
        assert result.demotion_target == "planning"


# ---------------------------------------------------------------------------
# 10–11. NEEDS_RESEARCH escalation
# ---------------------------------------------------------------------------


class TestNeedsResearch:
    def test_needs_research_triggers_research_agent(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("NEEDS_RESEARCH", confidence=65),
            _sec_response("LIKELY"),  # re-vote after research
        ]
        pipeline = _make_pipeline()
        research_calls = []

        async def _spy_research(*args, **kwargs):
            research_calls.append(args)
            return _FakeResearchResult(
                vote={"verdict": "LIKELY", "confidence": 90, "justification": "Resolved"}
            )

        with patch("app.agent.security_review.call_llm",
                   new=_SequentialCallLLM(responses)):
            with patch("app.agent.security_review.run_shell_security", return_value=""):
                with patch("app.agent.security_review.run_research", new=_spy_research):
                    with patch("app.database.create_security_review_result", MagicMock()):
                        _run(pipeline.run())

        assert len(research_calls) == 1, "run_research should have been called once"

    def test_needs_research_resolved_to_pass(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("NEEDS_RESEARCH", confidence=65),
            _sec_response("LIKELY"),  # re-vote result
        ]
        result = _run_pipeline(responses)
        assert result.outcome == "passed"


# ---------------------------------------------------------------------------
# 12. Veto power disabled
# ---------------------------------------------------------------------------


class TestVetoPower:
    def test_veto_power_false_rejected_vote_does_not_block(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("REJECTED", confidence=20),
        ]
        result = _run_pipeline(responses, veto_power=False)
        # Without veto power, REJECTED alone shouldn't block if no NEEDS_RESEARCH remains
        assert result.outcome == "passed"


# ---------------------------------------------------------------------------
# 13. Result structure
# ---------------------------------------------------------------------------


class TestResultStructure:
    def test_result_structure_fields(self):
        responses = [
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
            _sec_response("LIKELY"),
        ]
        result = _run_pipeline(responses)

        assert hasattr(result, "outcome")
        assert hasattr(result, "votes")
        assert hasattr(result, "findings")
        assert hasattr(result, "demotion_target")
        assert hasattr(result, "summary")
        assert hasattr(result, "prompt_tokens")
        assert hasattr(result, "completion_tokens")
        assert isinstance(result.votes, list)
        assert isinstance(result.findings, list)
