"""
tests/test_research_agent.py
-----------------------------
Tests for the Research Agent system: tool restrictions, lives system,
verdict extraction, and mock LLM integration.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

from app.agent.config import RESEARCH_AGENT_TOOLS
from app.agent.mock_llm import MockLLM
from app.agent.research import (
    ResearchAgent,
    ResearchResult,
    _build_restricted_schemas,
    run_research,
    run_tiebreaker,
)
from app.agent.tools import TOOL_SCHEMAS, TOOL_REGISTRY


def _llm_patch(mock_llm: MockLLM):
    """Patch app.agent.research.call_llm to delegate to mock_llm.complete().

    The real call_llm raises ValueError when budget_id is None, which prevents
    tests that construct ResearchAgent without a budget from reaching the LLM.
    Patching at the research module level bypasses that enforcement.
    """
    async def _side_effect(messages, **kwargs):
        return mock_llm.complete(messages, tools=kwargs.get("tools"))
    return patch("app.agent.research.call_llm", new=AsyncMock(side_effect=_side_effect))


# ===================================================================
# Tool restriction tests
# ===================================================================


class TestToolRestrictions:
    """Verify research agents are limited to the correct tool set."""

    def test_research_agent_tools_list(self):
        """RESEARCH_AGENT_TOOLS contains only read-only tools."""
        expected = {
            "read_file", "read_file_harder", "count_lines",
            "search_files", "find_files", "list_directory",
            "git_status", "git_diff", "git_log", "git_blame", "git_show",
            "get_task", "list_tasks",
        }
        assert set(RESEARCH_AGENT_TOOLS) == expected

    def test_restricted_schemas_only_include_allowed_tools(self):
        """_build_restricted_schemas filters to only RESEARCH_AGENT_TOOLS."""
        schemas = _build_restricted_schemas()
        schema_names = {s["function"]["name"] for s in schemas}
        assert schema_names == set(RESEARCH_AGENT_TOOLS)

    def test_restricted_schemas_count_matches(self):
        """Number of restricted schemas matches RESEARCH_AGENT_TOOLS length."""
        schemas = _build_restricted_schemas()
        assert len(schemas) == len(RESEARCH_AGENT_TOOLS)

    def test_write_file_not_in_research_tools(self):
        """write_file is NOT available to research agents."""
        assert "write_file" not in RESEARCH_AGENT_TOOLS

    def test_append_file_not_in_research_tools(self):
        """append_file is NOT available to research agents."""
        assert "append_file" not in RESEARCH_AGENT_TOOLS

    def test_archive_file_not_in_research_tools(self):
        """archive_file is NOT available to research agents."""
        assert "archive_file" not in RESEARCH_AGENT_TOOLS

    def test_git_write_tools_not_in_research_tools(self):
        """Git write tools are NOT available to research agents."""
        git_write_tools = {"git_create_branch", "git_commit", "git_checkout"}
        for tool in git_write_tools:
            assert tool not in RESEARCH_AGENT_TOOLS, (
                f"'{tool}' should NOT be in RESEARCH_AGENT_TOOLS"
            )

    def test_git_read_tools_in_research_tools(self):
        """git_status, git_diff, git_log, git_blame ARE in the restricted set.

        Per user requirements, research agents should use dedicated git
        tools rather than run_shell for git operations. The restricted
        set includes these read-only git tools.
        """
        assert "git_status" in RESEARCH_AGENT_TOOLS
        assert "git_diff" in RESEARCH_AGENT_TOOLS
        assert "git_log" in RESEARCH_AGENT_TOOLS
        assert "git_blame" in RESEARCH_AGENT_TOOLS

    def test_task_mutation_tools_not_in_research_tools(self):
        """Task mutation tools are NOT available to research agents."""
        assert "update_task_status" not in RESEARCH_AGENT_TOOLS
        assert "append_task_history" not in RESEARCH_AGENT_TOOLS

    def test_get_task_in_research_tools(self):
        """get_task IS available to research agents (read-only task lookup)."""
        assert "get_task" in RESEARCH_AGENT_TOOLS
        assert "list_tasks" in RESEARCH_AGENT_TOOLS

    def test_all_research_tools_exist_in_registry(self):
        """Every tool in RESEARCH_AGENT_TOOLS exists in the tool registry."""
        for tool_name in RESEARCH_AGENT_TOOLS:
            assert tool_name in TOOL_REGISTRY, (
                f"'{tool_name}' is in RESEARCH_AGENT_TOOLS but missing from TOOL_REGISTRY"
            )

    def test_all_research_tools_have_schemas(self):
        """Every tool in RESEARCH_AGENT_TOOLS has a matching schema."""
        all_schema_names = {s["function"]["name"] for s in TOOL_SCHEMAS}
        for tool_name in RESEARCH_AGENT_TOOLS:
            assert tool_name in all_schema_names, (
                f"'{tool_name}' is in RESEARCH_AGENT_TOOLS but has no schema in TOOL_SCHEMAS"
            )

    def test_run_shell_not_in_research_tools(self):
        """run_shell is NOT in the research tools.

        Per user requirements, research agents should not have access
        to run_shell. They use dedicated read-only tools (including
        git_status, git_diff, git_log, git_blame) instead.
        """
        assert "run_shell" not in RESEARCH_AGENT_TOOLS


# ===================================================================
# Tool call blocking tests
# ===================================================================


class TestToolCallBlocking:
    """Verify that the research agent blocks non-allowed tool calls."""

    def test_handle_tool_calls_blocks_write_file(self, sample_task_context):
        """Calling write_file through a research agent returns an error."""
        agent = ResearchAgent(
            question="Test question",
            context=sample_task_context,
        )
        tool_calls = [
            {
                "id": "call_001",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps({"path": "hack.py", "content": "evil"}),
                },
            }
        ]
        results = agent._handle_tool_calls(tool_calls)
        assert len(results) == 1
        assert "ERROR" in results[0]["content"]
        assert "write_file" in results[0]["content"]
        assert "not available" in results[0]["content"]

    def test_handle_tool_calls_blocks_git_commit(self, sample_task_context):
        """Calling git_commit through a research agent returns an error."""
        agent = ResearchAgent(
            question="Test question",
            context=sample_task_context,
        )
        tool_calls = [
            {
                "id": "call_002",
                "function": {
                    "name": "git_commit",
                    "arguments": json.dumps({"message": "sneaky commit"}),
                },
            }
        ]
        results = agent._handle_tool_calls(tool_calls)
        assert "ERROR" in results[0]["content"]
        assert "git_commit" in results[0]["content"]

    def test_handle_tool_calls_blocks_update_task_status(self, sample_task_context):
        """Calling update_task_status through a research agent returns an error."""
        agent = ResearchAgent(
            question="Test question",
            context=sample_task_context,
        )
        tool_calls = [
            {
                "id": "call_003",
                "function": {
                    "name": "update_task_status",
                    "arguments": json.dumps({"task_id": "task-1", "new_status": "ACCEPTED"}),
                },
            }
        ]
        results = agent._handle_tool_calls(tool_calls)
        assert "ERROR" in results[0]["content"]

    def test_handle_tool_calls_allows_read_file(self, sample_task_context):
        """Calling read_file through a research agent dispatches normally."""
        agent = ResearchAgent(
            question="Test question",
            context=sample_task_context,
        )
        tool_calls = [
            {
                "id": "call_004",
                "function": {
                    "name": "read_file",
                    # Use a path that exists in the project
                    "arguments": json.dumps({"path": "pyproject.toml"}),
                },
            }
        ]
        results = agent._handle_tool_calls(tool_calls)
        assert len(results) == 1
        # Should NOT contain the "not available" error
        assert "not available" not in results[0]["content"]

    def test_handle_tool_calls_allows_list_directory(self, sample_task_context):
        """Calling list_directory through a research agent dispatches normally."""
        agent = ResearchAgent(
            question="Test question",
            context=sample_task_context,
        )
        tool_calls = [
            {
                "id": "call_005",
                "function": {
                    "name": "list_directory",
                    "arguments": json.dumps({"path": "."}),
                },
            }
        ]
        results = agent._handle_tool_calls(tool_calls)
        assert "not available" not in results[0]["content"]


# ===================================================================
# Vote extraction tests
# ===================================================================


class TestVoteExtraction:
    """Verify the _extract_vote method parses various JSON formats."""

    def _make_agent(self):
        return ResearchAgent(question="test", context={})

    def test_extract_fenced_json(self):
        """Extracts verdict from ```json ... ``` fenced block."""
        agent = self._make_agent()
        content = '```json\n{"verdict": "LIKELY", "confidence": 95, "justification": "ok"}\n```'
        vote = agent._extract_vote(content)
        assert vote is not None
        assert vote["verdict"] == "LIKELY"
        assert vote["confidence"] == 95

    def test_extract_bare_json(self):
        """Extracts verdict from bare JSON object in text."""
        agent = self._make_agent()
        content = 'Here is my verdict: {"verdict": "REJECTED", "confidence": 30, "justification": "no"}'
        vote = agent._extract_vote(content)
        assert vote is not None
        assert vote["verdict"] == "REJECTED"

    def test_extract_returns_none_for_no_json(self):
        """Returns None when content has no JSON."""
        agent = self._make_agent()
        vote = agent._extract_vote("I need to investigate further.")
        assert vote is None

    def test_extract_returns_none_for_empty_content(self):
        """Returns None for empty string."""
        agent = self._make_agent()
        assert agent._extract_vote("") is None

    def test_extract_returns_none_for_json_without_verdict(self):
        """Returns None if JSON lacks 'verdict' key."""
        agent = self._make_agent()
        vote = agent._extract_vote('{"status": "ok", "count": 5}')
        assert vote is None

    def test_extract_returns_none_for_json_without_confidence(self):
        """Returns None if JSON lacks 'confidence' key."""
        agent = self._make_agent()
        vote = agent._extract_vote('{"verdict": "LIKELY", "justification": "ok"}')
        assert vote is None


# ===================================================================
# Context building tests
# ===================================================================


class TestContextBuilding:
    """Verify life context construction."""

    def test_life_1_context_includes_question(self, sample_task_context):
        agent = ResearchAgent(
            question="Is WebSocket feasible?",
            context=sample_task_context,
        )
        ctx = agent._build_life_context(1)
        assert "Is WebSocket feasible?" in ctx
        assert "Investigation Question" in ctx

    def test_life_1_context_includes_context_json(self, sample_task_context):
        agent = ResearchAgent(
            question="test",
            context=sample_task_context,
        )
        ctx = agent._build_life_context(1)
        assert "task-42" in ctx
        assert "Context" in ctx

    def test_life_2_context_includes_previous_findings(self, sample_task_context):
        agent = ResearchAgent(
            question="test",
            context=sample_task_context,
        )
        agent._accumulated_findings = ["[Life 1] Found relevant module in app/agent/"]
        ctx = agent._build_life_context(2)
        assert "Previous Investigation Findings" in ctx
        assert "Found relevant module" in ctx
        assert "continued" in ctx

    def test_life_2_context_mentions_life_number(self, sample_task_context):
        agent = ResearchAgent(
            question="test",
            context=sample_task_context,
            max_lives=3,
        )
        ctx = agent._build_life_context(2)
        assert "life 2" in ctx

    def test_last_life_context_requires_verdict(self, sample_task_context):
        agent = ResearchAgent(
            question="test",
            context=sample_task_context,
            max_lives=3,
        )
        agent._accumulated_findings = ["finding1"]
        ctx = agent._build_life_context(3)
        assert "must render a verdict" in ctx


# ===================================================================
# Research agent integration tests (with mock LLM)
# ===================================================================


@pytest.mark.asyncio
class TestResearchAgentWithMockLLM:
    """Integration tests using MockLLM to simulate the LLM."""

    async def test_pass_scenario_returns_likely(self, mock_llm_pass, sample_task_context):
        """Pass scenario returns LIKELY verdict on first life."""
        agent = ResearchAgent(
            question="Is this feasible?",
            context=sample_task_context,
        )

        with _llm_patch(mock_llm_pass):
            result = await agent.run()

        assert isinstance(result, ResearchResult)
        assert result.vote["verdict"] == "LIKELY"
        assert result.lives_used == 1
        assert mock_llm_pass.call_count >= 1

    async def test_fail_scenario_returns_rejected(self, mock_llm_fail, sample_task_context):
        """Fail scenario returns REJECTED verdict."""
        agent = ResearchAgent(
            question="Is this feasible?",
            context=sample_task_context,
        )

        with _llm_patch(mock_llm_fail):
            result = await agent.run()

        assert result.vote["verdict"] == "REJECTED"
        assert result.lives_used == 1

    async def test_needs_research_triggers_second_life(
        self, mock_llm_needs_research, sample_task_context
    ):
        """NEEDS_RESEARCH on life 1 triggers life 2, which resolves."""
        agent = ResearchAgent(
            question="Is this feasible?",
            context=sample_task_context,
            max_lives=3,
        )

        with _llm_patch(mock_llm_needs_research):
            result = await agent.run()

        assert result.vote["verdict"] == "LIKELY"
        assert result.lives_used == 2
        assert "Life 1" in result.findings

    async def test_exhaust_lives_returns_not_suitable(
        self, mock_llm_exhaust_lives, sample_task_context
    ):
        """When all lives are exhausted, NOT_SUITABLE fallback is returned."""
        agent = ResearchAgent(
            question="Is this feasible?",
            context=sample_task_context,
            max_lives=2,
        )

        with _llm_patch(mock_llm_exhaust_lives):
            result = await agent.run()

        assert result.vote["verdict"] == "NOT_SUITABLE"
        assert result.lives_used == 2

    async def test_tool_call_then_verdict(self, sample_task_context):
        """Agent makes a tool call, gets result, then renders verdict."""
        mock = MockLLM(scenario="tool_then_verdict")
        agent = ResearchAgent(
            question="Check main.py",
            context=sample_task_context,
        )

        with _llm_patch(mock), patch("app.agent.research.dispatch_tool") as mock_dispatch:
            mock_dispatch.return_value = "file contents here"
            result = await agent.run()

        assert result.vote["verdict"] == "LIKELY"
        assert mock.call_count == 2  # tool call + verdict

    async def test_blocked_tool_returns_error_message(self, sample_task_context):
        """Agent tries write_file; gets error, then renders verdict."""
        mock = MockLLM(scenario="blocked_tool")
        agent = ResearchAgent(
            question="Test blocking",
            context=sample_task_context,
        )

        with _llm_patch(mock):
            result = await agent.run()

        assert result.vote["verdict"] == "LIKELY"
        # The agent should have received an error for the blocked tool
        assert mock.call_count == 2

    async def test_token_tracking(self, mock_llm_pass, sample_task_context):
        """Token counts are tracked across the run."""
        agent = ResearchAgent(
            question="Is this feasible?",
            context=sample_task_context,
        )

        with _llm_patch(mock_llm_pass):
            result = await agent.run()

        assert result.prompt_tokens > 0
        assert result.completion_tokens > 0


# ===================================================================
# Tiebreaker tests
# ===================================================================


@pytest.mark.asyncio
class TestTiebreakerAgent:
    """Tests for the tie-breaker research agent."""

    async def test_tiebreaker_uses_tiebreaker_prompt(self, sample_task_context):
        """Tiebreaker agent uses _TIEBREAKER_SYSTEM_PROMPT."""
        mock = MockLLM(scenario="tie")
        agent = ResearchAgent(
            question="Resolve the tie",
            context=sample_task_context,
            is_tiebreaker=True,
        )

        with _llm_patch(mock):
            result = await agent.run()

        assert result.vote["verdict"] == "LIKELY"

        # Verify the system prompt was the tiebreaker one
        first_call = mock.call_log[0]
        system_msg = first_call["messages"][0]
        assert "Tie-Breaker" in system_msg["content"]

    async def test_tiebreaker_not_uses_research_prompt(self, sample_task_context):
        """Non-tiebreaker agent uses _RESEARCH_SYSTEM_PROMPT (not tiebreaker)."""
        mock = MockLLM(scenario="pass")
        agent = ResearchAgent(
            question="Regular research",
            context=sample_task_context,
            is_tiebreaker=False,
        )

        with _llm_patch(mock):
            result = await agent.run()

        first_call = mock.call_log[0]
        system_msg = first_call["messages"][0]
        assert "Research Agent" in system_msg["content"]
        assert "Tie-Breaker" not in system_msg["content"]

    async def test_run_tiebreaker_convenience_function(self):
        """run_tiebreaker builds correct context from votes."""
        mock = MockLLM(scenario="tie")
        votes = [
            {"verdict": "LIKELY", "confidence": 93, "justification": "Looks good"},
            {"verdict": "NOT_SUITABLE", "confidence": 55, "justification": "Too risky"},
        ]

        with _llm_patch(mock):
            result = await run_tiebreaker(
                task_description="Add OAuth2 login",
                votes=votes,
                max_lives=1,
            )

        assert result.vote["verdict"] == "LIKELY"

        # Verify the context included vote information
        first_call = mock.call_log[0]
        user_msg = first_call["messages"][1]["content"]
        assert "tie" in user_msg.lower()
        assert "1 pass vs 1 fail" in user_msg


# ===================================================================
# Convenience function tests
# ===================================================================


@pytest.mark.asyncio
class TestConvenienceFunctions:
    """Tests for module-level convenience functions."""

    async def test_run_research_returns_research_result(self):
        """run_research() returns a ResearchResult."""
        mock = MockLLM(scenario="pass")

        with _llm_patch(mock):
            result = await run_research(
                question="Test question",
                context={"task_id": "test"},
                max_lives=1,
            )

        assert isinstance(result, ResearchResult)
        assert result.vote["verdict"] == "LIKELY"


# ===================================================================
# Tool dispatch integration
# ===================================================================


class TestToolDispatchForRestrictedSet:
    """Verify dispatch_tool works for each tool in RESEARCH_AGENT_TOOLS."""

    def test_dispatch_read_file_unknown_path(self):
        """dispatch_tool('read_file', ...) with non-existent path returns error."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("read_file", {"path": "nonexistent_file_xyz.txt"})
        assert "ERROR" in result or "not a file" in result

    def test_dispatch_list_directory_project_root(self):
        """dispatch_tool('list_directory', ...) returns directory listing."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("list_directory", {"path": "."})
        assert "FILE" in result or "DIR" in result

    def test_dispatch_search_files(self):
        """dispatch_tool('search_files', ...) searches file contents."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("search_files", {"pattern": "FastAPI", "directory": "."})
        # Should find FastAPI reference in main.py or pyproject.toml
        assert isinstance(result, str)

    def test_dispatch_find_files(self):
        """dispatch_tool('find_files', ...) finds files by glob."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("find_files", {"glob_pattern": "*.py", "directory": "."})
        assert isinstance(result, str)
        # Should find at least some .py files
        assert ".py" in result or "No files" in result

    def test_dispatch_git_status(self):
        """dispatch_tool('git_status', ...) returns git status."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("git_status", {})
        assert isinstance(result, str)
        # Should return something from git (branch info, status, etc.)
        assert len(result) > 0

    def test_dispatch_git_diff(self):
        """dispatch_tool('git_diff', ...) returns diff output."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("git_diff", {})
        assert isinstance(result, str)

    def test_dispatch_unknown_tool(self):
        """dispatch_tool with unknown tool returns error."""
        from app.agent.tools import dispatch_tool
        result = dispatch_tool("nonexistent_tool", {})
        assert "ERROR" in result
        assert "Unknown tool" in result
