"""
Unit tests for pip_resolution.py — PIPResolutionAgent.

Covers the plan's Phase 7 testing requirements:
  - test_resolution_agent_runs_to_completion
  - test_resolution_agent_stalls_after_tool_failures
  - test_resolution_agent_signals_completion
"""
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.agent.pip_resolution import PIPResolutionAgent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_agent(**overrides):
    """Build a PIPResolutionAgent with safe test defaults."""
    defaults = dict(
        task_id="task-99",
        pip_id=7,
        requirements=["Add error logging.", "Write tests for edge cases."],
        research_findings="The auth module is missing error logs.",
        last_verification_findings=[
            {"requirement": "Add error logging.", "status": "missing", "detail": "no ERROR calls found"},
        ],
        project_root=None,   # skip set_task_git_cwd
        llm_id=1,
        budget_id=1,
        llm_base_url="http://localhost:8008/v1",
        llm_model="test-model",
        max_context=None,
        task_title="Auth Module",
        origin_stage="security",
    )
    defaults.update(overrides)
    return PIPResolutionAgent(**defaults)


def _tool_call_response(tool_name="list_files", tool_args=None):
    """Build a mock LLM response containing a single tool call."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": "",
                "tool_calls": [{
                    "id": "tc-1",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args or {"path": "."}),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20},
    }


def _text_response(content="I am done."):
    """Build a mock LLM response with no tool calls."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": content,
                "tool_calls": None,
            },
        }],
        "usage": {"prompt_tokens": 80, "completion_tokens": 10},
    }


def _stall_response():
    """Build a mock LLM response that emits the RESOLUTION_STALLED signal via submit_work."""
    return {
        "choices": [{
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "tc-stall",
                    "function": {
                        "name": "submit_work",
                        "arguments": json.dumps({
                            "signal": "RESOLUTION_STALLED",
                            "summary": "resolution exhausted",
                            "payload": {"reason": "consecutive tool failures"},
                        }),
                    },
                }],
            },
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


# ---------------------------------------------------------------------------
# Patches shared across tests
# ---------------------------------------------------------------------------

_COMMON_PATCHES = [
    # Prevent LLM HTTP calls
    # (patched per-test because responses differ)

    # Suppress snapshot I/O
    patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snapshot)"),
    patch("app.agent.project_snapshot.build_architecture_context", return_value=""),

    # Suppress DB access in _build_messages
    patch("app.database.get_task", return_value=None),
]


# ---------------------------------------------------------------------------
# test_resolution_agent_runs_to_completion
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_agent_runs_to_completion():
    """
    Agent makes tool calls in turn 1, then produces no tool calls twice.
    The second consecutive no-tool turn triggers 'done'.
    """
    responses = [
        _tool_call_response("list_files"),  # turn 1 — tool call
        _text_response("Checking…"),        # turn 2 — no tools → nudge
        _text_response("All done."),         # turn 3 — no tools again → done
    ]
    call_count = 0

    async def _mock_llm(*a, **kw):
        nonlocal call_count
        resp = responses[min(call_count, len(responses) - 1)]
        call_count += 1
        return resp

    agent = _make_agent()

    with patch("app.agent.pip_resolution.is_shutting_down", return_value=False), \
         patch("app.agent.agent_loop.call_llm", side_effect=_mock_llm), \
         patch("app.agent.agent_loop.async_dispatch_tool",
               new_callable=AsyncMock, return_value="['src/auth.py']"), \
         patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snap)"), \
         patch("app.agent.project_snapshot.build_architecture_context", return_value=""), \
         patch("app.database.get_task", return_value=None):

        result = await agent.run()

    assert result["status"] == "done"
    assert result["turns"] == 3
    # One tool call in turn 1, two text-only turns after
    assert call_count == 3


# ---------------------------------------------------------------------------
# test_resolution_agent_stalls_after_tool_failures
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_agent_stalls_after_tool_failures():
    """
    When every tool call in three consecutive turns returns an ERROR string,
    the agent stalls with status='stalled'.
    """
    # LLM always returns a tool call
    async def _always_tool(*a, **kw):
        return _tool_call_response("read_file", {"path": "src/auth.py"})

    agent = _make_agent()

    with patch("app.agent.pip_resolution.is_shutting_down", return_value=False), \
         patch("app.agent.agent_loop.call_llm", side_effect=_always_tool), \
         patch("app.agent.agent_loop.async_dispatch_tool",
               new_callable=AsyncMock,
               return_value="ERROR: file not found"), \
         patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snap)"), \
         patch("app.agent.project_snapshot.build_architecture_context", return_value=""), \
         patch("app.database.get_task", return_value=None):

        result = await agent.run()

    assert result["status"] == "stalled"
    assert result["turns"] == 3


# ---------------------------------------------------------------------------
# test_resolution_agent_stalls_on_signal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_agent_stalls_on_signal():
    """
    Agent stalls immediately when it emits {"signal": "RESOLUTION_STALLED"}.
    """
    agent = _make_agent()

    async def _emit_stall(*a, **kw):
        return _stall_response()

    with patch("app.agent.pip_resolution.is_shutting_down", return_value=False), \
         patch("app.agent.agent_loop.call_llm", side_effect=_emit_stall), \
         patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snap)"), \
         patch("app.agent.project_snapshot.build_architecture_context", return_value=""), \
         patch("app.database.get_task", return_value=None):

        result = await agent.run()

    assert result["status"] == "stalled"
    assert result["turns"] == 1


# ---------------------------------------------------------------------------
# test_resolution_agent_signals_completion
# ---------------------------------------------------------------------------

def test_resolution_agent_signals_completion():
    """
    _run_pip_resolution_agent() (the scheduler wrapper) must call
    signal_completion(f"pip_resolution_{pip_id}") on every exit path —
    even when the agent finishes normally (status='done').
    """
    from app.agent.scheduler import _run_pip_resolution_agent, signal_completion

    # Build minimal mock objects that the wrapper function needs
    mock_job = MagicMock()
    mock_job.id = 42
    mock_job.pip_id = 7
    mock_job.task_id = "task-99"
    mock_job.stage_blocked_at = "conceptual_review"
    mock_job.research_findings = "research text"

    mock_task = MagicMock()
    mock_task.project = "TestProject"
    mock_task.llm_id = 1
    mock_task.budget_id = 1
    mock_task.title = "Auth Module"

    mock_llm = MagicMock()
    mock_llm.id = 1
    mock_llm.address = "localhost"
    mock_llm.port = 8008
    mock_llm.model = "test-model"
    mock_llm.max_context = 8192

    mock_pip = MagicMock()
    mock_pip.id = 7
    mock_pip.origin_stage = "security"
    mock_pip.requirements = json.dumps(["Add error logging."])

    mock_v = MagicMock()
    mock_v.findings = json.dumps([
        {"requirement": "Add error logging.", "status": "missing", "detail": "none found"},
    ])

    signalled_keys = []

    def _fake_signal(key):
        signalled_keys.append(key)

    # PIPResolutionAgent.run() returns 'done' immediately
    async def _fake_run(self):
        return {"status": "done", "turns": 1}

    # The scheduler function uses lazy imports (from app.database import ...) so we
    # patch on the source module, not on app.agent.scheduler.
    with patch("app.database.get_pips_for_task", return_value=[mock_pip]), \
         patch("app.database.get_project_path", return_value=None), \
         patch("app.database.get_latest_pip_verification", return_value=mock_v), \
         patch("app.database.get_llm", return_value=mock_llm), \
         patch("app.database.update_pip_resolution_job"), \
         patch("app.agent.pip_resolution.PIPResolutionAgent.run", _fake_run), \
         patch("app.agent.scheduler.signal_completion", side_effect=_fake_signal), \
         patch("app.agent.scheduler._active_sessions", {}), \
         patch("app.agent.scheduler._session_llm_ids", {}), \
         patch("app.agent.scheduler._session_titles", {}), \
         patch("app.agent.scheduler._llm_session_counts",
               {mock_llm.id: 1}) as mock_counts:

        _run_pip_resolution_agent(mock_job, mock_task, mock_llm)

    assert f"pip_resolution_{mock_job.pip_id}" in signalled_keys, (
        "signal_completion must be called with 'pip_resolution_<pip_id>'"
    )


# ---------------------------------------------------------------------------
# test_resolution_agent_respects_max_turns
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resolution_agent_respects_max_turns():
    """
    When the LLM keeps calling tools forever, the agent exits with
    status='max_turns' once the turn cap is reached.
    """
    async def _always_tool(*a, **kw):
        return _tool_call_response()

    # Agent must be constructed inside the patch context so that PIP_RESOLUTION_MAX_TURNS
    # is already patched when the __init__ reads it for max_turns.
    with patch("app.agent.pip_resolution.PIP_RESOLUTION_MAX_TURNS", 3), \
         patch("app.agent.pip_resolution.is_shutting_down", return_value=False), \
         patch("app.agent.agent_loop.call_llm", side_effect=_always_tool), \
         patch("app.agent.agent_loop.async_dispatch_tool",
               new_callable=AsyncMock, return_value="ok"), \
         patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snap)"), \
         patch("app.agent.project_snapshot.build_architecture_context", return_value=""), \
         patch("app.database.get_task", return_value=None):

        agent = _make_agent()
        result = await agent.run()

    assert result["status"] == "max_turns"
    assert result["turns"] == 3


# ---------------------------------------------------------------------------
# test_resolution_agent_system_prompt_contains_requirements
# ---------------------------------------------------------------------------

def test_resolution_agent_system_prompt_contains_requirements():
    """
    _build_messages() must include all PIP requirements and key contextual
    sections in the system prompt.
    """
    agent = _make_agent(
        requirements=["Log at ERROR level.", "Add integration tests."],
        origin_stage="optimization",
        task_title="Perf Task",
    )

    with patch("app.agent.project_snapshot.build_project_snapshot", return_value="(snap)"), \
         patch("app.agent.project_snapshot.build_architecture_context", return_value=""), \
         patch("app.database.get_task", return_value=None):
        messages = agent._build_messages()

    assert len(messages) == 1
    system = messages[0]["content"]
    assert "Log at ERROR level." in system
    assert "Add integration tests." in system
    assert "optimization" in system   # origin_stage
    assert "Perf Task" in system      # task_title
    assert "RESOLUTION_STALLED" in system
