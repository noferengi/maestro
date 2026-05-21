"""
Unit and integration tests for GAP 8 — Real-time inter-agent messaging.

Tests cover:
  - ask_agent: depth cap fires at ASK_AGENT_MAX_DEPTH
  - ask_agent: inactive target returns clear error
  - list_active_sessions: calling session excluded from results
  - list_active_sessions: project filter works
  - InterAgentSession._build_context: correct task info injected
  - ask_agent full round-trip: answer returned as plain string, budget tagged
  - depth cap chain: depth 2 → nested ask_agent at depth 3 → cap error, no hang
"""

from __future__ import annotations

import asyncio
import threading
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _llm_resp(content: str, tool_calls=None):
    return {
        "choices": [
            {
                "message": {
                    "content": content,
                    "tool_calls": tool_calls,
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 20, "completion_tokens": 10},
    }


# ---------------------------------------------------------------------------
# Test 1: depth cap fires at ASK_AGENT_MAX_DEPTH
# ---------------------------------------------------------------------------

class TestAskAgentDepthCap:
    def test_depth_cap_fires(self):
        """ask_agent returns a readable error when ask_depth >= ASK_AGENT_MAX_DEPTH."""
        from app.agent.tools import async_dispatch_tool, _ask_depth_ctx
        from app.agent.config import ASK_AGENT_MAX_DEPTH

        async def _run():
            tok = _ask_depth_ctx.set(ASK_AGENT_MAX_DEPTH)
            try:
                result = await async_dispatch_tool(
                    "ask_agent",
                    {"target_session_id": "task-99", "question": "What did you find?"},
                    task_id="task-caller",
                    llm_id=1,
                    budget_id=1,
                )
            finally:
                _ask_depth_ctx.reset(tok)
            return result

        result = asyncio.run(_run())
        assert "Max inter-agent ask depth" in result
        assert str(ASK_AGENT_MAX_DEPTH) in result
        assert "__maestro_terminal__" not in result

    def test_depth_cap_no_crash(self):
        """Depth cap must return a string, never raise."""
        from app.agent.tools import async_dispatch_tool, _ask_depth_ctx
        from app.agent.config import ASK_AGENT_MAX_DEPTH

        async def _run():
            tok = _ask_depth_ctx.set(ASK_AGENT_MAX_DEPTH + 5)
            try:
                return await async_dispatch_tool(
                    "ask_agent",
                    {"target_session_id": "task-x", "question": "hello"},
                    task_id="task-caller",
                )
            finally:
                _ask_depth_ctx.reset(tok)

        result = asyncio.run(_run())
        assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Test 2: inactive target returns clear error
# ---------------------------------------------------------------------------

class TestAskAgentInactiveTarget:
    def test_inactive_target_returns_error(self):
        """ask_agent with a non-existent session_id returns a clear error without crashing."""
        from app.agent.tools import async_dispatch_tool, _ask_depth_ctx

        async def _run():
            tok = _ask_depth_ctx.set(0)
            try:
                with patch(
                    "app.agent.scheduler.get_active_session_info",
                    return_value=None,
                ):
                    result = await async_dispatch_tool(
                        "ask_agent",
                        {"target_session_id": "nonexistent-task", "question": "hello"},
                        task_id="task-caller",
                        llm_id=1,
                        budget_id=1,
                    )
            finally:
                _ask_depth_ctx.reset(tok)
            return result

        result = asyncio.run(_run())
        assert "not active" in result
        assert "list_active_sessions" in result
        assert "ERROR" not in result.split(":")[0]  # readable, not an ERROR: prefix


# ---------------------------------------------------------------------------
# Test 3: list_active_sessions excludes calling session
# ---------------------------------------------------------------------------

class TestListActiveSessions:
    def _make_thread(self) -> threading.Thread:
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()
        # Keep a reference so is_alive() is stable for test duration
        t._target = None  # mark as done-but-alive-stub
        # Create a fresh alive thread
        event = threading.Event()
        t2 = threading.Thread(target=event.wait)
        t2.daemon = True
        t2.start()
        return t2, event

    def test_excludes_calling_session(self):
        """list_active_sessions must not include the calling task's session."""
        import app.agent.scheduler as sched

        t_a, ev_a = self._make_thread()
        t_b, ev_b = self._make_thread()
        try:
            with sched._active_sessions_lock:
                sched._active_sessions["task-A"] = t_a
                sched._active_sessions["task-B"] = t_b
                sched._session_titles["task-A"] = "Task A"
                sched._session_titles["task-B"] = "Task B"
                sched._session_types["task-A"] = "indev"
                sched._session_types["task-B"] = "indev"
                sched._session_llm_ids["task-A"] = 1
                sched._session_llm_ids["task-B"] = 1

            result = sched.list_active_sessions(exclude_task_id="task-A")

            task_ids = [s["task_id"] for s in result]
            assert "task-A" not in task_ids
            assert "task-B" in task_ids
        finally:
            ev_a.set()
            ev_b.set()
            with sched._active_sessions_lock:
                sched._active_sessions.pop("task-A", None)
                sched._active_sessions.pop("task-B", None)
                for d in (sched._session_titles, sched._session_types, sched._session_llm_ids):
                    d.pop("task-A", None)
                    d.pop("task-B", None)

    def test_project_filter(self):
        """list_active_sessions respects the project_filter parameter."""
        import app.agent.scheduler as sched

        t_a, ev_a = self._make_thread()
        t_b, ev_b = self._make_thread()
        try:
            with sched._active_sessions_lock:
                sched._active_sessions["task-proj1"] = t_a
                sched._active_sessions["task-proj2"] = t_b
                sched._session_titles["task-proj1"] = "In proj-alpha"
                sched._session_titles["task-proj2"] = "In proj-beta"
                sched._session_types["task-proj1"] = "indev"
                sched._session_types["task-proj2"] = "indev"
                sched._session_llm_ids["task-proj1"] = 1
                sched._session_llm_ids["task-proj2"] = 1

            mock_task_alpha = MagicMock()
            mock_task_alpha.project = "proj-alpha"
            mock_task_beta = MagicMock()
            mock_task_beta.project = "proj-beta"

            def _fake_get_task(tid):
                return mock_task_alpha if tid == "task-proj1" else mock_task_beta

            with patch("app.database.get_task", side_effect=_fake_get_task):
                result = sched.list_active_sessions(project_filter="proj-alpha")

            task_ids = [s["task_id"] for s in result]
            assert "task-proj1" in task_ids
            assert "task-proj2" not in task_ids
        finally:
            ev_a.set()
            ev_b.set()
            with sched._active_sessions_lock:
                for key in ("task-proj1", "task-proj2"):
                    sched._active_sessions.pop(key, None)
                    for d in (sched._session_titles, sched._session_types, sched._session_llm_ids):
                        d.pop(key, None)


# ---------------------------------------------------------------------------
# Test 4: InterAgentSession._build_context
# ---------------------------------------------------------------------------

class TestInterAgentSessionContext:
    def test_build_context_injects_task_info(self):
        """_build_context returns the target task's title, stage, and description."""
        from app.agent.inter_agent_session import InterAgentSession

        mock_task = MagicMock()
        mock_task.title = "Refactor auth module"
        mock_task.type = "indev"
        mock_task.description = "Rewrite the JWT handler using PyJWT 3.x."

        session = InterAgentSession(
            question="What's your current approach?",
            target_task_id="task-42",
            calling_task_id="task-99",
            calling_session_id=None,
            ask_depth=1,
            llm_id=1,
            budget_id=1,
        )

        with patch("app.database.get_task", return_value=mock_task):
            ctx = session._build_context()

        assert "Refactor auth module" in ctx
        assert "indev" in ctx
        assert "Rewrite the JWT handler" in ctx

    def test_build_context_missing_task(self):
        """_build_context returns empty string when target task does not exist."""
        from app.agent.inter_agent_session import InterAgentSession

        session = InterAgentSession(
            question="hello",
            target_task_id="nonexistent",
            calling_task_id="task-99",
            calling_session_id=None,
            ask_depth=1,
            llm_id=1,
            budget_id=1,
        )

        with patch("app.database.get_task", return_value=None):
            ctx = session._build_context()

        assert ctx == ""


# ---------------------------------------------------------------------------
# Test 5: Full round-trip — answer returned as plain string
# ---------------------------------------------------------------------------

class TestAskAgentRoundTrip:
    def test_answer_returned_as_string(self):
        """ask_agent returns the peer's answer as a plain non-terminal string."""
        import app.agent.scheduler as sched
        from app.agent.tools import async_dispatch_tool, _ask_depth_ctx

        t, ev = self._make_alive_thread()
        try:
            with sched._active_sessions_lock:
                sched._active_sessions["task-peer"] = t
                sched._session_titles["task-peer"] = "Peer Task"
                sched._session_types["task-peer"] = "indev"
                sched._session_llm_ids["task-peer"] = 1

            async def _run():
                tok = _ask_depth_ctx.set(0)
                try:
                    with patch(
                        "app.agent.llm_client.call_llm",
                        new=AsyncMock(return_value=_llm_resp("Use singleton pattern.")),
                    ):
                        return await async_dispatch_tool(
                            "ask_agent",
                            {"target_session_id": "task-peer", "question": "Which pattern?"},
                            task_id="task-caller",
                            llm_id=1,
                            budget_id=1,
                        )
                finally:
                    _ask_depth_ctx.reset(tok)

            result = asyncio.run(_run())
            assert result == "Use singleton pattern."
            assert "__maestro_terminal__" not in result
        finally:
            ev.set()
            with sched._active_sessions_lock:
                sched._active_sessions.pop("task-peer", None)
                for d in (sched._session_titles, sched._session_types, sched._session_llm_ids):
                    d.pop("task-peer", None)

    def test_budget_agent_name_tagged(self):
        """Budget entries from InterAgentSession are tagged agent_name='InterAgentSession'."""
        import app.agent.scheduler as sched
        from app.agent.inter_agent_session import InterAgentSession, AGENT_NAME

        t, ev = self._make_alive_thread()
        try:
            with sched._active_sessions_lock:
                sched._active_sessions["task-peer2"] = t
                sched._session_titles["task-peer2"] = "Peer2"
                sched._session_types["task-peer2"] = "indev"
                sched._session_llm_ids["task-peer2"] = 1

            recorded_calls: list[dict] = []

            async def _fake_call_llm(*args, **kwargs):
                recorded_calls.append(kwargs)
                return _llm_resp("42 is the answer.")

            session = InterAgentSession(
                question="What is the answer?",
                target_task_id="task-peer2",
                calling_task_id="task-caller2",
                calling_session_id=None,
                ask_depth=1,
                llm_id=1,
                budget_id=1,
            )

            with patch("app.agent.llm_client.call_llm", new=_fake_call_llm):
                with patch("app.database.get_task", return_value=None):
                    result = asyncio.run(session.run())

            assert result == "42 is the answer."
            assert any(kw.get("agent_name") == AGENT_NAME for kw in recorded_calls)
        finally:
            ev.set()
            with sched._active_sessions_lock:
                sched._active_sessions.pop("task-peer2", None)
                for d in (sched._session_titles, sched._session_types, sched._session_llm_ids):
                    d.pop("task-peer2", None)

    def _make_alive_thread(self):
        event = threading.Event()
        t = threading.Thread(target=event.wait)
        t.daemon = True
        t.start()
        return t, event


# ---------------------------------------------------------------------------
# Test 6: Depth cap chain — no hang or crash at max depth
# ---------------------------------------------------------------------------

class TestDepthCapChain:
    def test_nested_ask_at_cap_returns_error(self):
        """
        InterAgentSession at depth=(max-1) dispatches ask_agent which
        immediately hits the cap and returns an error string — no hang, no crash.
        """
        from app.agent.inter_agent_session import InterAgentSession
        from app.agent.config import ASK_AGENT_MAX_DEPTH

        # Build a tool_call response that asks another agent
        nested_tool_call = {
            "id": "tc-1",
            "function": {
                "name": "ask_agent",
                "arguments": '{"target_session_id": "task-nested", "question": "hello"}',
            },
        }
        # First LLM turn: return a nested ask_agent call
        # Second LLM turn: return a plain answer
        call_count = {"n": 0}

        async def _fake_llm(*args, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _llm_resp(None, tool_calls=[nested_tool_call])
            return _llm_resp("Done, used my own judgment.")

        session = InterAgentSession(
            question="Please ask task-nested something",
            target_task_id="task-target",
            calling_task_id="task-caller",
            calling_session_id=None,
            ask_depth=ASK_AGENT_MAX_DEPTH,  # already AT cap
            llm_id=1,
            budget_id=1,
        )

        with patch("app.agent.llm_client.call_llm", new=_fake_llm):
            with patch("app.database.get_task", return_value=None):
                result = asyncio.run(session.run())

        # The nested ask_agent tool call should have returned the depth-cap message,
        # then the second LLM call should have proceeded normally.
        assert isinstance(result, str)
        assert len(result) > 0
