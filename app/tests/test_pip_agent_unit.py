"""
Unit tests for pip_agent.py — PIP generator and pre-flight gate.

Covers the plan's Phase 4 testing requirements:
  - test_preflight_all_passed
  - test_preflight_one_failed
  - test_preflight_no_commit_context
  - test_generate_pip_captures_commit
  - test_generate_pip_no_git
"""
import json
import asyncio
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from app.agent.pip_agent import generate_pip, run_pip_preflight


# ---------------------------------------------------------------------------
# generate_pip tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_pip_success():
    """generate_pip creates a PIP row with the correct task/stage/requirements."""
    task_id = "task-123"
    origin_stage = "security"
    reason = "Found a hardcoded API key in the controller."

    mock_task = MagicMock()
    mock_task.id = task_id
    mock_task.title = "Fix API Keys"
    mock_task.project = "TestProject"
    mock_task.llm_id = 1
    mock_task.budget_id = 1

    mock_content = json.dumps({
        "requirements": [
            "Remove all hardcoded API keys from src/controllers.",
            "Implement a secure environment variable loader.",
        ]
    })
    mock_response = {
        "choices": [{"message": {"content": mock_content, "tool_calls": None}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 50},
    }

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_project_path", return_value="/tmp/proj"), \
         patch("app.agent.pip_agent.subprocess") as mock_subproc, \
         patch("app.agent.pip_agent.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.agent.pip_agent.create_pip") as mock_create:

        mock_subproc.run.return_value = MagicMock(returncode=0, stdout="abc123\n")
        mock_call.return_value = mock_response

        await generate_pip(task_id, origin_stage, reason)

        mock_call.assert_called_once()
        mock_create.assert_called_once()
        _, kwargs = mock_create.call_args
        assert kwargs["task_id"] == task_id
        assert kwargs["origin_stage"] == origin_stage
        assert "Remove all hardcoded" in kwargs["requirements"]


@pytest.mark.asyncio
async def test_generate_pip_captures_commit():
    """generate_pip passes the HEAD commit SHA to create_pip as created_at_commit."""
    mock_task = MagicMock()
    mock_task.id = "t-1"
    mock_task.title = "T"
    mock_task.project = "proj"
    mock_task.llm_id = 1
    mock_task.budget_id = 1

    mock_response = {
        "choices": [{"message": {"content": json.dumps({"requirements": ["req1"]}), "tool_calls": None}}],
        "usage": {},
    }

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_project_path", return_value="/tmp/proj"), \
         patch("app.agent.pip_agent.subprocess") as mock_subproc, \
         patch("app.agent.pip_agent.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.agent.pip_agent.create_pip") as mock_create:

        mock_subproc.run.return_value = MagicMock(returncode=0, stdout="deadbeef\n")
        mock_call.return_value = mock_response

        await generate_pip("t-1", "conceptual_review", "reason")

        _, kwargs = mock_create.call_args
        assert kwargs["created_at_commit"] == "deadbeef"


@pytest.mark.asyncio
async def test_generate_pip_no_git():
    """generate_pip stores 'none' as created_at_commit when git is unavailable."""
    mock_task = MagicMock()
    mock_task.id = "t-2"
    mock_task.title = "T"
    mock_task.project = "proj"
    mock_task.llm_id = 1
    mock_task.budget_id = 1

    mock_response = {
        "choices": [{"message": {"content": json.dumps({"requirements": ["req1"]}), "tool_calls": None}}],
        "usage": {},
    }

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_project_path", return_value="/tmp/proj"), \
         patch("app.agent.pip_agent.subprocess") as mock_subproc, \
         patch("app.agent.pip_agent.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.agent.pip_agent.create_pip") as mock_create:

        mock_subproc.run.side_effect = FileNotFoundError("git not found")
        mock_call.return_value = mock_response

        await generate_pip("t-2", "security", "reason")

        _, kwargs = mock_create.call_args
        assert kwargs["created_at_commit"] == "none"


# ---------------------------------------------------------------------------
# run_pip_preflight tests
# ---------------------------------------------------------------------------

def _make_pip(pip_id, origin_stage="security", requirements=None, commit="abc123"):
    pip = MagicMock()
    pip.id = pip_id
    pip.origin_stage = origin_stage
    pip.requirements = json.dumps(requirements or ["Req A", "Req B"])
    pip.created_at_commit = commit
    return pip


@pytest.mark.asyncio
async def test_preflight_all_passed():
    """When all PIPs pass, all_passed=True and verification rows are written."""
    pip1 = _make_pip(1)
    pip2 = _make_pip(2)

    mock_task = MagicMock()
    mock_task.id = "t-10"

    passed_content = json.dumps({
        "outcome": "passed",
        "summary": "All good.",
        "findings": [{"requirement": "Req A", "status": "satisfied", "detail": "done"}],
    })
    passed_response = {
        "choices": [{"message": {"content": passed_content, "tool_calls": None}}],
        "usage": {},
    }

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_pips_for_task", return_value=[pip1, pip2]), \
         patch("app.agent.pip_agent.build_project_snapshot", return_value="snap"), \
         patch("app.agent.pip_agent._get_git_diff_stat", return_value="1 file changed"), \
         patch("app.agent.pip_agent.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.agent.pip_agent.create_pip_verification") as mock_write:

        mock_call.return_value = passed_response

        result = await run_pip_preflight("t-10", "conceptual_review", 1, 1, "/tmp/proj")

    assert result["all_passed"] is True
    assert len(result["results"]) == 2
    assert all(r["outcome"] == "passed" for r in result["results"])
    assert mock_write.call_count == 2


@pytest.mark.asyncio
async def test_preflight_one_failed():
    """When one PIP fails, all_passed=False and a failed verification row is written."""
    pip1 = _make_pip(1)
    pip2 = _make_pip(2)

    mock_task = MagicMock()
    mock_task.id = "t-11"

    def _wrap(content_str):
        return {"choices": [{"message": {"content": content_str, "tool_calls": None}}], "usage": {}}

    passed = _wrap(json.dumps({"outcome": "passed", "summary": "OK", "findings": []}))
    failed = _wrap(json.dumps({"outcome": "failed", "summary": "Missing tests.", "findings": [
        {"requirement": "Req A", "status": "missing", "detail": "no tests found"}
    ]}))

    call_count = 0
    async def _alternating(*a, **kw):
        nonlocal call_count
        call_count += 1
        return passed if call_count % 2 == 1 else failed

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_pips_for_task", return_value=[pip1, pip2]), \
         patch("app.agent.pip_agent.build_project_snapshot", return_value="snap"), \
         patch("app.agent.pip_agent._get_git_diff_stat", return_value="diff"), \
         patch("app.agent.pip_agent.call_llm", side_effect=_alternating), \
         patch("app.agent.pip_agent.create_pip_verification") as mock_write:

        result = await run_pip_preflight("t-11", "optimization", 1, 1, "/tmp/proj")

    assert result["all_passed"] is False
    outcomes = {r["pip_id"]: r["outcome"] for r in result["results"]}
    assert mock_write.call_count == 2
    # At least one failed
    assert "failed" in outcomes.values()


@pytest.mark.asyncio
async def test_preflight_no_commit_context():
    """When created_at_commit='none', diff section shows the fallback text."""
    pip1 = _make_pip(1, commit="none")

    mock_task = MagicMock()
    mock_task.id = "t-12"

    captured_prompts = []
    async def _capture(messages, **kw):
        captured_prompts.append(messages[0]["content"])
        return {
            "choices": [{"message": {"content": json.dumps({"outcome": "passed", "summary": "OK", "findings": []}), "tool_calls": None}}],
            "usage": {},
        }

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_pips_for_task", return_value=[pip1]), \
         patch("app.agent.pip_agent.build_project_snapshot", return_value="snap"), \
         patch("app.agent.pip_agent.call_llm", side_effect=_capture), \
         patch("app.agent.pip_agent.create_pip_verification"):

        await run_pip_preflight("t-12", "conceptual_review", 1, 1, "/tmp/proj")

    assert len(captured_prompts) == 1
    assert "No commit history to diff against" in captured_prompts[0]


@pytest.mark.asyncio
async def test_preflight_no_pips_skips():
    """Tasks with no PIPs return all_passed=True immediately without any LLM call."""
    mock_task = MagicMock()
    mock_task.id = "t-13"

    with patch("app.agent.pip_agent.get_task", return_value=mock_task), \
         patch("app.agent.pip_agent.get_pips_for_task", return_value=[]), \
         patch("app.agent.pip_agent.call_llm", new_callable=AsyncMock) as mock_call:

        result = await run_pip_preflight("t-13", "security", 1, 1, "/tmp")

    assert result["all_passed"] is True
    assert result["results"] == []
    mock_call.assert_not_called()
