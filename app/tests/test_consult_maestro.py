"""
Unit tests for the consult_maestro tool (GAP 1).

Tests cover:
  - Answer is returned as a plain tool result (non-terminal)
  - Call cap fires on the (N+1)th call
  - Maestro orchestrator tool list excludes consult_maestro
  - Config: ORCHESTRATION_LLM_ID and CONSULT_MAX_CALLS_PER_SESSION resolve
  - maestro_llm_id on Project model and CRUD
  - ConsultAgent LLM resolution helper
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch, MagicMock

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
# Test: async_dispatch_tool returns ConsultAgent answer (mocked)
# ---------------------------------------------------------------------------

class TestConsultMaestroDispatch:
    """consult_maestro in async_dispatch_tool spins up ConsultAgent and returns answer."""

    def test_answer_returned_as_string(self):
        """Non-terminal: the answer is returned as a plain string tool result."""
        from app.agent.tools import reset_consult_count, _consult_call_counts

        task_id = "test-task-consult-1"
        reset_consult_count(task_id)

        async def _run():
            with patch("app.agent.consult_agent.run_consult_agent", new=AsyncMock(
                return_value="Use the Strategy pattern here."
            )):
                from app.agent.tools import async_dispatch_tool
                result = await async_dispatch_tool(
                    "consult_maestro",
                    {"question": "Should I use Strategy or Command pattern?"},
                    task_id=task_id,
                    llm_id=1,
                    budget_id=1,
                )
            return result

        result = asyncio.run(_run())
        assert result == "Use the Strategy pattern here."
        assert "__maestro_terminal__" not in result

    def test_answer_not_terminal_signal(self):
        """The tool result must not be a terminal signal JSON."""
        import json
        from app.agent.tools import reset_consult_count

        task_id = "test-task-consult-2"
        reset_consult_count(task_id)

        async def _run():
            with patch("app.agent.consult_agent.run_consult_agent", new=AsyncMock(
                return_value="Proceed with option B."
            )):
                from app.agent.tools import async_dispatch_tool
                return await async_dispatch_tool(
                    "consult_maestro",
                    {"question": "Which option?"},
                    task_id=task_id,
                    llm_id=1,
                    budget_id=1,
                )

        result = asyncio.run(_run())
        try:
            parsed = json.loads(result)
            assert not parsed.get("__maestro_terminal__"), "Must not be a terminal signal"
        except json.JSONDecodeError:
            pass  # Plain string — correct

    def test_empty_question_returns_error(self):
        """Empty question is rejected before ConsultAgent is invoked."""
        from app.agent.tools import reset_consult_count

        task_id = "test-task-consult-empty"
        reset_consult_count(task_id)

        async def _run():
            from app.agent.tools import async_dispatch_tool
            return await async_dispatch_tool(
                "consult_maestro",
                {"question": "   "},
                task_id=task_id,
                llm_id=1,
                budget_id=1,
            )

        result = asyncio.run(_run())
        assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# Test: call cap
# ---------------------------------------------------------------------------

class TestConsultCallCap:
    """consult_maestro enforces CONSULT_MAX_CALLS_PER_SESSION."""

    def test_cap_fires_on_excess_call(self, monkeypatch):
        """The (N+1)th call returns the cap message without calling ConsultAgent."""
        from app.agent.tools import reset_consult_count
        import app.agent.config as cfg

        task_id = "test-task-cap"
        reset_consult_count(task_id)
        monkeypatch.setattr(cfg, "CONSULT_MAX_CALLS_PER_SESSION", 2)

        call_tracker: list[int] = []

        async def _fake_run_consult(**kwargs):
            call_tracker.append(1)
            return "Some answer."

        async def _run():
            from app.agent.tools import async_dispatch_tool
            results = []
            for i in range(4):
                r = await async_dispatch_tool(
                    "consult_maestro",
                    {"question": f"Question {i}"},
                    task_id=task_id,
                    llm_id=1,
                    budget_id=1,
                )
                results.append(r)
            return results

        with patch("app.agent.consult_agent.run_consult_agent", new=AsyncMock(
            side_effect=_fake_run_consult
        )):
            results = asyncio.run(_run())

        # Calls 1 and 2 should succeed, calls 3 and 4 should hit the cap
        assert results[0] == "Some answer."
        assert results[1] == "Some answer."
        assert "limit" in results[2].lower() or "reached" in results[2].lower()
        assert "limit" in results[3].lower() or "reached" in results[3].lower()
        assert len(call_tracker) == 2  # ConsultAgent only ran for the first 2

    def test_reset_clears_count(self, monkeypatch):
        """reset_consult_count() resets the counter so subsequent calls go through."""
        from app.agent.tools import reset_consult_count
        import app.agent.config as cfg

        task_id = "test-task-reset"
        monkeypatch.setattr(cfg, "CONSULT_MAX_CALLS_PER_SESSION", 1)

        async def _run_once(tid):
            from app.agent.tools import async_dispatch_tool
            return await async_dispatch_tool(
                "consult_maestro",
                {"question": "Any question"},
                task_id=tid,
                llm_id=1,
                budget_id=1,
            )

        with patch("app.agent.consult_agent.run_consult_agent", new=AsyncMock(
            return_value="Answer."
        )):
            # First session: use up the cap
            reset_consult_count(task_id)
            r1 = asyncio.run(_run_once(task_id))
            r2 = asyncio.run(_run_once(task_id))  # over cap

            # Reset and try again
            reset_consult_count(task_id)
            r3 = asyncio.run(_run_once(task_id))  # should work again

        assert r1 == "Answer."
        assert "limit" in r2.lower() or "reached" in r2.lower()
        assert r3 == "Answer."


# ---------------------------------------------------------------------------
# Test: sync dispatch_tool returns error (consult_maestro needs async)
# ---------------------------------------------------------------------------

class TestConsultSyncFallback:
    def test_sync_dispatch_returns_error(self):
        """Calling consult_maestro via the sync dispatch_tool returns an error."""
        from app.agent.tools import dispatch_tool

        result = dispatch_tool("consult_maestro", {"question": "test"})
        assert "ERROR" in result
        assert "async" in result.lower()


# ---------------------------------------------------------------------------
# Test: Maestro orchestrator tool list excludes consult_maestro
# ---------------------------------------------------------------------------

class TestMaestroToolListExclusion:
    def test_maestro_tool_list_excludes_consult_maestro(self):
        """The Maestro orchestrator's tool lists must not include consult_maestro."""
        import ast, os
        maestro_path = os.path.join(
            os.path.dirname(__file__), "..", "agent", "maestro.py"
        )
        with open(maestro_path, "r", encoding="utf-8") as f:
            source = f.read()
        # Check that consult_maestro is not in any maestro_tools list literal
        # This is a heuristic: parse the file and check string literals in list assignments
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "maestro_tools":
                        # Collect all string constants in this list
                        for elt in ast.walk(node.value):
                            if isinstance(elt, ast.Constant) and isinstance(elt.value, str):
                                assert elt.value != "consult_maestro", (
                                    "consult_maestro must not appear in maestro_tools"
                                )


# ---------------------------------------------------------------------------
# Test: INDEV_AGENT_TOOLS includes consult_maestro
# ---------------------------------------------------------------------------

class TestIndevToolsIncludesConsult:
    def test_indev_tools_includes_consult_maestro(self):
        """INDEV_AGENT_TOOLS must include consult_maestro so MaestroLoop exposes it."""
        from app.agent.config import INDEV_AGENT_TOOLS
        assert "consult_maestro" in INDEV_AGENT_TOOLS

    def test_tool_schema_built_for_indev(self):
        """build_tool_schemas(INDEV_AGENT_TOOLS) includes the consult_maestro schema."""
        from app.agent.config import INDEV_AGENT_TOOLS
        from app.agent.tools import build_tool_schemas

        schemas = build_tool_schemas(INDEV_AGENT_TOOLS)
        names = [s["function"]["name"] for s in schemas]
        assert "consult_maestro" in names


# ---------------------------------------------------------------------------
# Test: config resolution
# ---------------------------------------------------------------------------

class TestOrchestrationConfig:
    def test_consult_max_calls_default(self):
        """CONSULT_MAX_CALLS_PER_SESSION defaults to 3."""
        from app.agent.config import CONSULT_MAX_CALLS_PER_SESSION
        assert CONSULT_MAX_CALLS_PER_SESSION == 3

    def test_consult_agent_max_turns_default(self):
        """CONSULT_AGENT_MAX_TURNS defaults to 5."""
        from app.agent.config import CONSULT_AGENT_MAX_TURNS
        assert CONSULT_AGENT_MAX_TURNS == 5

    def test_orchestration_llm_id_is_none_or_int(self):
        """ORCHESTRATION_LLM_ID is either None or a positive integer."""
        from app.agent.config import ORCHESTRATION_LLM_ID
        assert ORCHESTRATION_LLM_ID is None or (
            isinstance(ORCHESTRATION_LLM_ID, int) and ORCHESTRATION_LLM_ID > 0
        )


# ---------------------------------------------------------------------------
# Test: Project.maestro_llm_id DB column
# ---------------------------------------------------------------------------

class TestProjectMaestroLlmId:
    def test_upsert_project_accepts_maestro_llm_id(self):
        """upsert_project can store and retrieve maestro_llm_id."""
        from app.database import upsert_project, get_project, create_llm

        llm = create_llm(address="127.0.0.1", port=18008, model="consult-test-model")
        assert llm is not None
        llm_id = llm.id

        project = upsert_project("test-consult-project", maestro_llm_id=llm_id)
        assert project is not None
        assert project.maestro_llm_id == llm_id

        fetched = get_project("test-consult-project")
        assert fetched is not None
        assert fetched.maestro_llm_id == llm_id

    def test_upsert_project_maestro_llm_id_default_none(self):
        """maestro_llm_id defaults to None when not provided."""
        from app.database import upsert_project

        project = upsert_project("test-consult-project-no-llm")
        assert project is not None
        assert project.maestro_llm_id is None

    def test_project_to_dict_includes_maestro_llm_id(self):
        """project_to_dict includes maestro_llm_id in the output dict."""
        from app.database import upsert_project, project_to_dict

        project = upsert_project("test-consult-dict-project")
        d = project_to_dict(project)
        assert "maestro_llm_id" in d

    def test_upsert_project_clear_maestro_llm_id(self):
        """Passing maestro_llm_id=None explicitly clears the value."""
        from app.database import upsert_project, get_project, create_llm

        llm = create_llm(address="127.0.0.2", port=18008, model="consult-clear-model")
        assert llm is not None

        upsert_project("test-consult-clear", maestro_llm_id=llm.id)
        upsert_project("test-consult-clear", maestro_llm_id=None)
        fetched = get_project("test-consult-clear")
        assert fetched.maestro_llm_id is None


# ---------------------------------------------------------------------------
# Test: ConsultAgent LLM resolution
# ---------------------------------------------------------------------------

class TestConsultAgentLlmResolution:
    def test_project_setting_wins(self, monkeypatch):
        """project.maestro_llm_id takes priority over ini/system setting."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", 99)

        result = ca._resolve_maestro_llm(project_maestro_llm_id=42)
        assert result == 42

    def test_ini_setting_fallback(self, monkeypatch):
        """ORCHESTRATION_LLM_ID used when no project setting is set."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", 5)

        result = ca._resolve_maestro_llm(project_maestro_llm_id=None)
        assert result == 5

    def test_system_setting_fallback(self, monkeypatch):
        """Falls back to system_setting 'maestro_llm_id' when ini is not set."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", None)

        with patch("app.database.get_system_setting", return_value=7):
            result = ca._resolve_maestro_llm(project_maestro_llm_id=None)
        assert result == 7

    def test_all_unset_returns_none(self, monkeypatch):
        """Returns None when no LLM is configured anywhere."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", None)

        with patch("app.database.get_system_setting", return_value=None):
            result = ca._resolve_maestro_llm(project_maestro_llm_id=None)
        assert result is None


# ---------------------------------------------------------------------------
# Test: ConsultAgent run (mocked LLM)
# ---------------------------------------------------------------------------

class TestConsultAgentRun:
    def test_returns_answer_on_first_text_response(self, monkeypatch):
        """ConsultAgent returns the first text response from the LLM."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", 1)

        async def _fake_call_llm(messages, **kwargs):
            return _llm_resp("You should use a connection pool here.")

        async def _run():
            with patch("app.agent.llm_client.call_llm", new=_fake_call_llm):
                with patch("app.database.get_llm", return_value=MagicMock(
                    base_url="http://localhost:8008/v1",
                    model="test-model",
                    max_context=4096,
                )):
                    return await ca.run_consult_agent(
                        question="Should I use connection pooling?",
                        task_id="t1",
                        caller_llm_id=1,
                        budget_id=1,
                        project_name=None,
                        project_maestro_llm_id=None,
                    )

        result = asyncio.run(_run())
        assert result == "You should use a connection pool here."

    def test_returns_error_when_no_llm_configured(self, monkeypatch):
        """ConsultAgent returns an ERROR string when no LLM is resolvable."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", None)

        async def _run():
            with patch("app.database.get_system_setting", return_value=None):
                return await ca.run_consult_agent(
                    question="Any question",
                    task_id="t2",
                    caller_llm_id=None,
                    budget_id=1,
                    project_name=None,
                    project_maestro_llm_id=None,
                )

        result = asyncio.run(_run())
        assert result.startswith("ERROR:")
        assert "LLM" in result or "llm" in result.lower()

    def test_tool_call_handled_before_answer(self, monkeypatch):
        """ConsultAgent processes a tool call turn before returning the text answer."""
        import app.agent.consult_agent as ca
        monkeypatch.setattr(ca, "ORCHESTRATION_LLM_ID", 1)

        call_count = {"n": 0}

        async def _fake_call_llm(messages, **kwargs):
            call_count["n"] += 1
            if call_count["n"] == 1:
                # First call: tool call
                return {
                    "choices": [
                        {
                            "message": {
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "tc1",
                                        "function": {
                                            "name": "list_tasks",
                                            "arguments": '{"project": "MyProject"}',
                                        },
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                }
            # Second call: text answer
            return _llm_resp("Based on the tasks, use approach X.")

        async def _fake_dispatch(name, args, **kwargs):
            return "task list result"

        async def _run():
            with patch("app.agent.llm_client.call_llm", new=_fake_call_llm):
                with patch("app.agent.tools.async_dispatch_tool", new=_fake_dispatch):
                    with patch("app.database.get_llm", return_value=MagicMock(
                        base_url="http://localhost:8008/v1",
                        model="test-model",
                        max_context=4096,
                    )):
                        return await ca.run_consult_agent(
                            question="What tasks are in progress?",
                            task_id="t3",
                            caller_llm_id=1,
                            budget_id=1,
                            project_name="MyProject",
                            project_maestro_llm_id=None,
                        )

        result = asyncio.run(_run())
        assert result == "Based on the tasks, use approach X."
        assert call_count["n"] == 2
