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

    with mock.patch("app.agent.agent_loop.async_dispatch_tool", return_value=submit_json):
        await loop._dispatch_tools(tool_calls)

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

    with mock.patch("app.agent.agent_loop.async_dispatch_tool", return_value="file contents here"):
        await loop._dispatch_tools(tool_calls)

    assert loop._terminal_signal is None


# ---------------------------------------------------------------------------
# 3. MaestroLoop.run() — full loop exits via submit_work tool call
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_maestro_loop_exits_accepted_on_submit_work():
    """Loop must return status=ACCEPTED when LLM emits a submit_work(ACCEPTED) tool call."""
    from app.agent.loop import MaestroLoop

    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    with mock.patch("app.agent.agent_loop.call_llm", _sequential_llm(submit_response)):
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

    with mock.patch("app.agent.agent_loop.call_llm", _sequential_llm(revert_response)):
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

    with mock.patch("app.agent.agent_loop.call_llm", _sequential_llm(submit_response)):
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

    with mock.patch("app.agent.agent_loop.call_llm", recording_llm):
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


# ---------------------------------------------------------------------------
# 4. ComponentLoop.run() — __maestro_terminal__ detection
# ---------------------------------------------------------------------------

def _make_component_loop(task_id: str = "t-comp", max_turns: int = 5) -> object:
    from app.agent.component_loop import ComponentLoop
    return ComponentLoop(
        task_id=task_id,
        component_name="widget",
        implementation_step={"description": "Build it", "files": ["widget.py"], "depends_on": []},
        planning_context="Build a widget.",
        allowed_write_paths=["widget.py"],
        max_turns=max_turns,
    )


@pytest.mark.asyncio
async def test_component_loop_exits_accepted_on_submit_work():
    """ComponentLoop must return status=ACCEPTED when LLM calls submit_work(ACCEPTED)."""
    from app.agent.component_loop import ComponentLoop

    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    with mock.patch("app.agent.component_loop.call_llm", _sequential_llm(submit_response)):
        # Patch dispatcher so submit_work goes through the real function (no file I/O)
        loop = _make_component_loop()
        # Bypass test gate — mark tests as passed so ACCEPTED is not blocked
        loop._tests_passed = True
        result = await loop.run()

    assert result.status == "ACCEPTED"
    assert result.component_name == "widget"


@pytest.mark.asyncio
async def test_component_loop_exits_revert_on_submit_work():
    """ComponentLoop must return status=REVERT_TO_DESIGN on submit_work(REVERT_TO_DESIGN)."""
    revert_response = _llm_tool_call(
        "submit_work", {"signal": "REVERT_TO_DESIGN", "summary": "cannot proceed"}
    )

    with mock.patch("app.agent.component_loop.call_llm", _sequential_llm(revert_response)):
        loop = _make_component_loop()
        result = await loop.run()

    assert result.status == "REVERT_TO_DESIGN"
    assert "cannot proceed" in (result.error_detail or "")


@pytest.mark.asyncio
async def test_component_loop_accepted_gate_blocks_without_tests():
    """submit_work(ACCEPTED) before any passing test run must be blocked; loop continues."""
    # Turn 1: LLM returns submit_work(ACCEPTED), tests not passed → gate injects nudge, loops
    # Turn 2: loop sets _tests_passed=True before returning response → ACCEPTED goes through
    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    call_count = 0
    loop_ref: list = []  # filled after loop is created

    async def gating_llm(messages, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            # Simulate that tests passed between turns
            loop_ref[0]._tests_passed = True
        return submit_response

    with mock.patch("app.agent.component_loop.call_llm", gating_llm):
        loop = _make_component_loop(max_turns=10)
        loop_ref.append(loop)
        # _tests_passed starts False — first ACCEPTED must be blocked
        result = await loop.run()

    # The gate blocked the first ACCEPTED; the second call went through
    assert result.status == "ACCEPTED"
    assert call_count >= 2, f"Expected ≥2 LLM calls (gate should block first ACCEPTED); got {call_count}"


@pytest.mark.asyncio
async def test_component_loop_empty_response_nudge_mentions_submit_work():
    """An empty LLM response must produce a nudge that names 'submit_work'."""
    empty_response = _llm_text("")  # no content, no tool_calls
    empty_response["choices"][0]["message"]["content"] = ""
    submit_response = _llm_tool_call("submit_work", {"signal": "ACCEPTED", "summary": "done"})

    captured_messages: list[list[dict]] = []

    async def recording_llm(messages, **kwargs):
        captured_messages.append(list(messages))
        if len(captured_messages) == 1:
            return empty_response
        # Subsequent calls: mark tests passed and return submit_work
        return submit_response

    with mock.patch("app.agent.component_loop.call_llm", recording_llm):
        loop = _make_component_loop(max_turns=5)
        loop._tests_passed = True
        await loop.run()

    assert len(captured_messages) >= 2
    second_call_messages = captured_messages[1]
    nudge_contents = [
        m["content"] for m in second_call_messages
        if m.get("role") == "user" and "submit_work" in (m.get("content") or "")
    ]
    assert nudge_contents, "Expected a nudge message referencing 'submit_work'"
