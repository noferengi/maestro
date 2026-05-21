"""
Tests for the ReflectionAgent (GAP 6).

Covers:
  - _build_context_message includes task title and description
  - get_task_history_recent clamps max_turns to [1, 50]
  - LLM resolution chain: reflection_llm_id > ORCHESTRATION_LLM_ID > project default
  - _store_reflection_report stores at the correct key
  - _on_terminal stores the report and advances with condition='pass'

Patch targets for lazy imports (from X import Y inside function bodies):
  get_task             → "app.database.get_task"
  list_documents       → "app.agent.doc_store.list_documents"
  store_document       → "app.agent.doc_store.store_document"
  get_budget_entries   → "app.database.get_budget_entries"
  advance_stage is a module-level import → "app.agent.reflection_agent.advance_stage"
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock, MagicMock, patch, call as mcall


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@dataclass
class _FakeStageConfig:
    stage_key: str = "test_reflection"
    label: str = "Reflection"
    agent_type: str = "reflection_agent"
    position: int = 5
    config: dict | None = None
    template_id: int = 1
    stage_id: int = 42


def _make_task(title="Test Task", description="A test description", project="proj"):
    t = MagicMock()
    t.title = title
    t.description = description
    t.content = {}
    t.project = project
    return t


# ---------------------------------------------------------------------------
# 1. _build_context_message
# ---------------------------------------------------------------------------

class TestBuildContextMessage:
    def test_includes_task_title_and_description(self):
        """_build_context_message must embed task title and description."""
        from app.agent.reflection_agent import _build_context_message

        stage_config = _FakeStageConfig(stage_key="review_stage")

        with patch("app.database.get_task", return_value=_make_task("Implement OAuth", "Add Google login")), \
             patch("app.agent.doc_store.list_documents", return_value=[]):
            msg = _build_context_message("task-1", stage_config, 20)

        assert "Implement OAuth" in msg
        assert "Add Google login" in msg
        assert "task-1" in msg
        assert "review_stage" in msg

    def test_includes_prior_stage_output(self):
        """final_output in task.content is surfaced in the message."""
        from app.agent.reflection_agent import _build_context_message

        task = _make_task()
        task.content = {"final_output": "def foo(): return 42"}
        stage_config = _FakeStageConfig()

        with patch("app.database.get_task", return_value=task), \
             patch("app.agent.doc_store.list_documents", return_value=[]):
            msg = _build_context_message("task-1", stage_config, 5)

        assert "def foo(): return 42" in msg
        assert "final_output" in msg

    def test_includes_prior_reflection_reports_from_other_stages(self):
        """Documents tagged 'reflection' for the task appear in context."""
        from app.agent.reflection_agent import _build_context_message

        stage_config = _FakeStageConfig(stage_key="current_stage")
        prior_doc = {
            "key": "reflection:task-1:earlier_stage",
            "content": '{"confidence": 0.6, "issues": []}',
        }

        with patch("app.database.get_task", return_value=_make_task()), \
             patch("app.agent.doc_store.list_documents", return_value=[prior_doc]):
            msg = _build_context_message("task-1", stage_config, 5)

        assert "reflection:task-1:earlier_stage" in msg
        assert "Prior Reflection Reports" in msg

    def test_current_stage_report_not_in_prior_list(self):
        """The reflection report for the current stage is excluded from 'prior' list."""
        from app.agent.reflection_agent import _build_context_message

        stage_config = _FakeStageConfig(stage_key="current_stage")
        current_doc = {"key": "reflection:task-1:current_stage", "content": "{}"}

        with patch("app.database.get_task", return_value=_make_task()), \
             patch("app.agent.doc_store.list_documents", return_value=[current_doc]):
            msg = _build_context_message("task-1", stage_config, 5)

        # Current stage key should not appear in the 'Prior Reflection Reports' block
        assert "Prior Reflection Reports" not in msg


# ---------------------------------------------------------------------------
# 2. get_task_history_recent tool
# ---------------------------------------------------------------------------

class TestGetTaskHistoryRecentTool:
    def test_clamps_max_turns_above_50(self):
        """max_turns=100 must be clamped to 50 before the DB query."""
        from app.agent.tools import get_task_history_recent

        with patch("app.database.get_budget_entries", return_value=[]) as mock_get:
            get_task_history_recent("task-1", max_turns=100)

        mock_get.assert_called_once_with(task_id="task-1", limit=50)

    def test_clamps_max_turns_below_1(self):
        """max_turns=0 must be clamped to 1."""
        from app.agent.tools import get_task_history_recent

        with patch("app.database.get_budget_entries", return_value=[]) as mock_get:
            get_task_history_recent("task-1", max_turns=0)

        mock_get.assert_called_once_with(task_id="task-1", limit=1)

    def test_returns_valid_json_list(self):
        """Result is a JSON-parseable list with expected fields."""
        from app.agent.tools import get_task_history_recent

        entry = MagicMock()
        entry.id = 99
        entry.agent_name = "generic:indev"
        entry.created_at = None
        entry.prompt_cost = 100
        entry.generation_cost = 50
        entry.response_data = json.dumps({
            "choices": [{"message": {"content": "I'll write the code."}, "finish_reason": "stop"}]
        })

        with patch("app.database.get_budget_entries", return_value=[entry]):
            result = get_task_history_recent("task-1", max_turns=5)

        parsed = json.loads(result)
        assert isinstance(parsed, list)
        assert len(parsed) == 1
        assert parsed[0]["entry_id"] == 99
        assert parsed[0]["finish_reason"] == "stop"
        assert "I'll write" in parsed[0]["content_preview"]

    def test_content_preview_capped_at_500_chars(self):
        """content_preview must not exceed 500 characters."""
        from app.agent.tools import get_task_history_recent

        entry = MagicMock()
        entry.id = 1
        entry.agent_name = "a"
        entry.created_at = None
        entry.prompt_cost = 10
        entry.generation_cost = 5
        entry.response_data = json.dumps({
            "choices": [{"message": {"content": "x" * 2000}, "finish_reason": "stop"}]
        })

        with patch("app.database.get_budget_entries", return_value=[entry]):
            result = get_task_history_recent("task-1", max_turns=5)

        parsed = json.loads(result)
        assert len(parsed[0]["content_preview"]) <= 500


# ---------------------------------------------------------------------------
# 3. LLM resolution chain
# ---------------------------------------------------------------------------

class TestLlmResolution:
    def test_stage_reflection_llm_id_wins(self):
        """reflection_llm_id in stage config takes priority over everything."""
        from app.agent.reflection_agent import _resolve_reflection_llm

        stage_config = _FakeStageConfig(config={"reflection_llm_id": 7})
        with patch("app.agent.reflection_agent.ORCHESTRATION_LLM_ID", 3):
            result = _resolve_reflection_llm(stage_config, fallback_llm_id=1)
        assert result == 7

    def test_orchestration_llm_id_fallback(self):
        """When no stage reflection_llm_id, ORCHESTRATION_LLM_ID is used."""
        from app.agent.reflection_agent import _resolve_reflection_llm

        stage_config = _FakeStageConfig(config={})
        with patch("app.agent.reflection_agent.ORCHESTRATION_LLM_ID", 3):
            result = _resolve_reflection_llm(stage_config, fallback_llm_id=1)
        assert result == 3

    def test_project_default_fallback(self):
        """When no stage llm or orchestration llm, falls back to passed llm_id."""
        from app.agent.reflection_agent import _resolve_reflection_llm

        stage_config = _FakeStageConfig(config={})
        with patch("app.agent.reflection_agent.ORCHESTRATION_LLM_ID", None):
            result = _resolve_reflection_llm(stage_config, fallback_llm_id=5)
        assert result == 5

    def test_none_when_all_unset(self):
        """Returns None when every LLM source is absent."""
        from app.agent.reflection_agent import _resolve_reflection_llm

        stage_config = _FakeStageConfig(config={})
        with patch("app.agent.reflection_agent.ORCHESTRATION_LLM_ID", None):
            result = _resolve_reflection_llm(stage_config, fallback_llm_id=None)
        assert result is None


# ---------------------------------------------------------------------------
# 4. _store_reflection_report
# ---------------------------------------------------------------------------

class TestStoreReflectionReport:
    def test_correct_key_format(self):
        """Report is stored at reflection:{task_id}:{stage_key}."""
        from app.agent.reflection_agent import _store_reflection_report

        with patch("app.database.get_task", return_value=_make_task(project="my-proj")), \
             patch("app.agent.doc_store.store_document") as mock_store:
            _store_reflection_report("task-42", "reflection_stage", '{"confidence": 0.8}')

        mock_store.assert_called_once()
        args = mock_store.call_args[0]
        assert args[0] == "my-proj"
        assert args[1] == "reflection:task-42:reflection_stage"

    def test_tags_include_reflection(self):
        """Stored document has 'reflection' in its tags."""
        from app.agent.reflection_agent import _store_reflection_report

        with patch("app.database.get_task", return_value=_make_task()), \
             patch("app.agent.doc_store.store_document") as mock_store:
            _store_reflection_report("t1", "s1", "{}")

        _, kwargs = mock_store.call_args
        assert "reflection" in kwargs.get("tags", [])

    def test_no_crash_when_task_missing(self):
        """Silently skips when task is not found (no exception)."""
        from app.agent.reflection_agent import _store_reflection_report

        with patch("app.database.get_task", return_value=None):
            _store_reflection_report("ghost", "stage", "{}")  # must not raise

    def test_same_key_for_repeated_calls(self):
        """Calling twice with same task_id + stage_key produces the same doc key (upsert)."""
        from app.agent.reflection_agent import _store_reflection_report

        captured_keys = []

        def _capture(project, key, content, **kw):
            captured_keys.append(key)

        with patch("app.database.get_task", return_value=_make_task()), \
             patch("app.agent.doc_store.store_document", side_effect=_capture):
            _store_reflection_report("t1", "stage_x", '{"confidence": 0.7}')
            _store_reflection_report("t1", "stage_x", '{"confidence": 0.9}')

        assert captured_keys[0] == captured_keys[1] == "reflection:t1:stage_x"

    def test_different_stages_produce_different_keys(self):
        """Two reflection stages on one task produce different document keys."""
        from app.agent.reflection_agent import _store_reflection_report

        captured_keys = []

        def _capture(project, key, content, **kw):
            captured_keys.append(key)

        with patch("app.database.get_task", return_value=_make_task()), \
             patch("app.agent.doc_store.store_document", side_effect=_capture):
            _store_reflection_report("t1", "stage_a", "{}")
            _store_reflection_report("t1", "stage_b", "{}")

        assert captured_keys[0] == "reflection:t1:stage_a"
        assert captured_keys[1] == "reflection:t1:stage_b"


# ---------------------------------------------------------------------------
# 5. _on_terminal: stores report and advances with condition='pass'
# ---------------------------------------------------------------------------

class TestReflectionAgentOnTerminal:
    """Test _on_terminal directly without running the full agent loop."""

    def _make_agent(self, stage_config=None, llm_id=1, budget_id=1):
        from app.agent.reflection_agent import ReflectionAgent

        if stage_config is None:
            stage_config = _FakeStageConfig(stage_key="my_reflection", config={})

        # ReflectionAgent.__init__ calls AgentLoop.__init__ which just stores params;
        # patch resolve_reflection_llm's dependency so no DB needed for constructor
        with patch("app.agent.reflection_agent.ORCHESTRATION_LLM_ID", None):
            agent = ReflectionAgent(
                task_id="task-term-1",
                stage_config=stage_config,
                llm_id=llm_id,
                budget_id=budget_id,
            )
        return agent

    def test_on_terminal_stores_report_and_advances(self):
        """_on_terminal stores JSON report at correct key and calls advance_stage('pass')."""
        agent = self._make_agent()
        report_payload = {
            "confidence": 0.85,
            "issues": [{"severity": "note", "finding": "Variable x is ambiguous."}],
            "uncertain_about": [],
        }
        agent._terminal_signal = {"signal": "ACCEPTED", "payload": report_payload}

        stored = {}
        advanced = []

        def _capture_store(project, key, content, **kw):
            stored[key] = content

        with patch("app.database.get_task", return_value=_make_task(project="proj")), \
             patch("app.agent.doc_store.store_document", side_effect=_capture_store), \
             patch("app.agent.reflection_agent.advance_stage", side_effect=lambda tid, cond: advanced.append(cond)):
            result = asyncio.run(agent._on_terminal())

        assert result["condition"] == "pass"
        assert advanced == ["pass"]
        assert "reflection:task-term-1:my_reflection" in stored
        stored_data = json.loads(stored["reflection:task-term-1:my_reflection"])
        assert abs(stored_data["confidence"] - 0.85) < 0.001

    def test_on_terminal_with_empty_payload_stores_fallback(self):
        """When submit_work sends no payload, a fallback report is stored."""
        agent = self._make_agent()
        agent._terminal_signal = {"signal": "ACCEPTED"}  # no payload

        stored = {}

        def _capture_store(project, key, content, **kw):
            stored[key] = content

        with patch("app.database.get_task", return_value=_make_task(project="proj")), \
             patch("app.agent.doc_store.store_document", side_effect=_capture_store), \
             patch("app.agent.reflection_agent.advance_stage"):
            result = asyncio.run(agent._on_terminal())

        assert result["condition"] == "pass"
        key = "reflection:task-term-1:my_reflection"
        assert key in stored
        fallback = json.loads(stored[key])
        assert "uncertain_about" in fallback

    def test_on_max_turns_stores_incomplete_report_and_advances(self):
        """_on_max_turns stores a zero-confidence report and still advances."""
        agent = self._make_agent()

        stored = {}

        def _capture_store(project, key, content, **kw):
            stored[key] = content

        with patch("app.database.get_task", return_value=_make_task(project="proj")), \
             patch("app.agent.doc_store.store_document", side_effect=_capture_store), \
             patch("app.agent.reflection_agent.advance_stage"):
            result = asyncio.run(agent._on_max_turns())

        assert result["condition"] == "pass"
        key = "reflection:task-term-1:my_reflection"
        assert key in stored
        data = json.loads(stored[key])
        assert data["confidence"] == 0.0
        assert "Max turns" in data["uncertain_about"][0]
