"""
Tests for submit_work tool-call terminal detection in MaestroLoop and ComponentLoop.

Covers:
  - submit_work() output format
  - MaestroLoop._handle_tool_calls: sets _terminal_signal from __maestro_terminal__ marker
  - MaestroLoop.run(): exits ACCEPTED / REVERT_TO_DESIGN when LLM emits submit_work tool call
  - MaestroLoop nudge: no-tool-call response triggers message referencing 'submit_work'
  - ComponentLoop.run(): __maestro_terminal__ detection exits loop correctly
  - ComponentLoop: ACCEPTED gate blocks exit when tests haven't passed
  - ComponentLoop: empty-response nudge mentions 'submit_work'
"""

from __future__ import annotations

import asyncio
import json
import unittest.mock as mock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_tool_call(name: str, args: dict, call_id: str = "tc1") -> dict:
    """Build a fake call_llm response that makes the LLM call one tool."""
    return {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args),
                    },
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


def _llm_text(content: str) -> dict:
    """Build a fake call_llm response with plain text and no tool calls."""
    return {
        "choices": [{
            "message": {"content": content, "tool_calls": None},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 50, "completion_tokens": 5},
    }


def _sequential_llm(*responses):
    """Returns an async callable that yields responses in order (last one repeats)."""
    calls = list(responses)

    async def _call(*args, **kwargs):
        return calls.pop(0) if len(calls) > 1 else calls[0]

    return _call


# ---------------------------------------------------------------------------
# 1. submit_work() tool — pure unit tests, no mocking needed
# ---------------------------------------------------------------------------

def test_submit_work_accepted_returns_terminal_marker():
    from app.agent.tools import submit_work
    result = submit_work(signal="ACCEPTED", summary="Implementation complete")
    data = json.loads(result)
    assert data["__maestro_terminal__"] is True
    assert data["signal"] == "ACCEPTED"
    assert data["summary"] == "Implementation complete"
    assert data["payload"] == {}


def test_submit_work_revert_signal():
    from app.agent.tools import submit_work
    result = submit_work(signal="REVERT_TO_DESIGN", summary="Cannot proceed")
    data = json.loads(result)
    assert data["__maestro_terminal__"] is True
    assert data["signal"] == "REVERT_TO_DESIGN"


def test_submit_work_payload_is_included():
    from app.agent.tools import submit_work
    result = submit_work(signal="ACCEPTED", summary="done", payload={"key": "value"})
    data = json.loads(result)
    assert data["payload"] == {"key": "value"}


def test_submit_work_payload_defaults_to_empty_dict():
    from app.agent.tools import submit_work
    result = submit_work(signal="ACCEPTED", summary="done")
    data = json.loads(result)
    assert data["payload"] == {}


# ---------------------------------------------------------------------------
# 2. MaestroLoop._handle_tool_calls — sets _terminal_signal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_tool_calls_sets_terminal_signal_on_accepted():
    """_handle_tool_calls must store __maestro_terminal__ dict in _terminal_signal."""
    from app.agent.loop import MaestroLoop, _LOOP_STATUS

    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "t-htc-accepted"
    loop.llm_id = None
    loop.budget_id = None
    loop.llm_base_url = "http://localhost:8008/v1"
    loop.llm_model = "test"
    loop._git_branch = None
    loop._files_changed = []
    loop._terminal_signal = None
    _LOOP_STATUS[loop.task_id] = {}

    submit_json = json.dumps({
        "__maestro_terminal__": True,
        "signal": "ACCEPTED",
        "summary": "done",
        "payload": {},
    })

    tool_calls = [{
        "id": "tc-accepted",
        "function": {"name": "submit_work", "arguments": json.dumps({"signal": "ACCEPTED", "summary": "done"})},
    }]

    with mock.patch("app.agent.loop.async_dispatch_tool", return_value=submit_json):
        await loop._handle_tool_calls(tool_calls)

    assert loop._terminal_signal is not None
    assert loop._terminal_signal["signal"] == "ACCEPTED"
    assert loop._terminal_signal["__maestro_terminal__"] is True


@pytest.mark.asyncio
async def test_handle_tool_calls_does_not_set_terminal_signal_for_normal_tools():
    """A non-terminal tool result must not set _terminal_signal."""
    from app.agent.loop import MaestroLoop, _LOOP_STATUS

    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "t-htc-normal"
    loop.llm_id = None
    loop.budget_id = None
    loop.llm_base_url = "http://localhost:8008/v1"
    loop.llm_model = "test"
    loop._git_branch = None
    loop._files_changed = []
    loop._terminal_signal = None
    _LOOP_STATUS[loop.task_id] = {}

    tool_calls = [{
        "id": "tc-read",
        "function": {"name": "read_file", "arguments": json.dumps({"path": "README.md"})},
    }]

    with mock.patch("app.agent.loop.async_dispatch_tool", return_value="file contents here"):
        await loop._handle_tool_calls(tool_calls)

    assert loop._terminal_signal is None


# ---------------------------------------------------------------------------
# 3. MaestroLoop.run() — full loop exits via submit_work tool call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maestro_loop_exits_accepted_on_submit_work():
    """Loop must return status=ACCEPTED when LLM emits a submit_work(ACCEPTED) tool call."""
    from app.agent.loop import MaestroLoop

    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    with mock.patch("app.agent.loop.call_llm", _sequential_llm(submit_response)):
        loop = MaestroLoop(task_id="t-loop-accepted", max_turns=5)
        result = await loop.run()

    assert result.status == "ACCEPTED"
    assert result.task_id == "t-loop-accepted"


@pytest.mark.asyncio
async def test_maestro_loop_exits_revert_on_submit_work():
    """Loop must return status=REVERT_TO_DESIGN when LLM emits submit_work(REVERT_TO_DESIGN)."""
    from app.agent.loop import MaestroLoop

    revert_response = _llm_tool_call(
        "submit_work", {"signal": "REVERT_TO_DESIGN", "summary": "impossible"}
    )

    with mock.patch("app.agent.loop.call_llm", _sequential_llm(revert_response)):
        loop = MaestroLoop(task_id="t-loop-revert", max_turns=5)
        result = await loop.run()

    assert result.status == "REVERT_TO_DESIGN"


@pytest.mark.asyncio
async def test_maestro_loop_summary_propagates_from_submit_work():
    """The summary from submit_work must appear in the LoopResult.final_message."""
    from app.agent.loop import MaestroLoop

    submit_response = _llm_tool_call(
        "submit_work", {"signal": "ACCEPTED", "summary": "Refactored cache module"}
    )

    with mock.patch("app.agent.loop.call_llm", _sequential_llm(submit_response)):
        loop = MaestroLoop(task_id="t-loop-summary", max_turns=5)
        result = await loop.run()

    assert result.status == "ACCEPTED"
    assert "Refactored cache module" in (result.final_message or "")


@pytest.mark.asyncio
async def test_maestro_loop_no_tool_nudge_mentions_submit_work():
    """When the LLM emits plain text with no tool call, the nudge must name 'submit_work'."""
    from app.agent.loop import MaestroLoop

    # Turn 1: plain text (triggers nudge injection)
    # Turn 2: submit_work to end the loop cleanly
    text_response = _llm_text("I am thinking about the implementation.")
    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    captured_messages: list[list[dict]] = []

    async def recording_llm(messages, **kwargs):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            return text_response
        return submit_response

    with mock.patch("app.agent.loop.call_llm", recording_llm):
        loop = MaestroLoop(task_id="t-nudge", max_turns=5)
        await loop.run()

    # The second call should include the nudge injected after the plain-text response
    assert len(captured_messages) >= 2
    second_call_messages = captured_messages[1]
    # Find any user message injected between turn-1 and turn-2 calls
    nudge_contents = [
        m["content"] for m in second_call_messages
        if m.get("role") == "user" and "submit_work" in (m.get("content") or "")
    ]
    assert nudge_contents, "Expected a nudge message referencing 'submit_work'"
