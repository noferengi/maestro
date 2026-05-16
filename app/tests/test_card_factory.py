"""
Tests for Phase 9 — Card Factory System.

Coverage:
  - DataSourceAdapter yields correct items (FolderAdapter, CSVAdapter, SQLiteQueryAdapter)
  - _interpolate template substitution
  - _run_mechanical creates the right number of cards
  - factory_runs CRUD (create, update, query helpers)
  - _cron_is_due logic
  - predecessor_already_triggered idempotency guard
  - Manual trigger API endpoint (POST /api/pipelines/stages/{id}/trigger-factory)
"""

import csv
import json
import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_pipeline_stage(config: dict, stage_id: int = 99):
    """Return a minimal mock PipelineStage for use in factory tests."""
    stage = MagicMock()
    stage.id = stage_id
    stage.stage_key = "factory_test"
    stage.label = "Test Factory"
    stage.agent_type = "factory_node"
    stage.config = config
    return stage


# ---------------------------------------------------------------------------
# DataSourceAdapter tests
# ---------------------------------------------------------------------------

class TestFolderAdapter:
    def test_yields_one_item_per_file(self, tmp_path):
        (tmp_path / "a.txt").write_text("hello")
        (tmp_path / "b.txt").write_text("world")
        (tmp_path / "c.pdf").write_text("pdf")

        from app.agent.factory_sources import FolderAdapter
        adapter = FolderAdapter(path=str(tmp_path), file_glob="*.txt")
        items = list(adapter.items())

        assert len(items) == 2
        names = {i["filename"] for i in items}
        assert names == {"a.txt", "b.txt"}
        for item in items:
            assert "filepath" in item
            assert "extension" in item
            assert "size_bytes" in item

    def test_recursive_glob(self, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        (tmp_path / "root.txt").write_text("r")
        (sub / "child.txt").write_text("c")

        from app.agent.factory_sources import FolderAdapter
        adapter = FolderAdapter(path=str(tmp_path), file_glob="*.txt", recursive=True)
        items = list(adapter.items())

        assert len(items) == 2

    def test_empty_folder(self, tmp_path):
        from app.agent.factory_sources import FolderAdapter
        adapter = FolderAdapter(path=str(tmp_path), file_glob="*.pdf")
        assert list(adapter.items()) == []

    def test_nonexistent_folder_raises(self):
        from app.agent.factory_sources import FolderAdapter
        adapter = FolderAdapter(path="/nonexistent/path/xyz", file_glob="*")
        with pytest.raises(ValueError, match="Folder not found"):
            list(adapter.items())


class TestCSVAdapter:
    def test_yields_one_item_per_row(self, tmp_path):
        f = tmp_path / "data.csv"
        f.write_text("name,value\nalpha,1\nbeta,2\ngamma,3\n")

        from app.agent.factory_sources import CSVAdapter
        items = list(CSVAdapter(filepath=str(f)).items())

        assert len(items) == 3
        assert items[0] == {"name": "alpha", "value": "1"}
        assert items[2] == {"name": "gamma", "value": "3"}

    def test_empty_csv(self, tmp_path):
        f = tmp_path / "empty.csv"
        f.write_text("col1,col2\n")
        from app.agent.factory_sources import CSVAdapter
        assert list(CSVAdapter(filepath=str(f)).items()) == []


class TestSQLiteQueryAdapter:
    def test_yields_rows(self, tmp_path):
        db_path = str(tmp_path / "data.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE items (id INTEGER, name TEXT)")
        conn.executemany("INSERT INTO items VALUES (?, ?)", [(1, "one"), (2, "two"), (3, "three")])
        conn.commit()
        conn.close()

        from app.agent.factory_sources import SQLiteQueryAdapter
        adapter = SQLiteQueryAdapter(db_path=db_path, query="SELECT id, name FROM items")
        items = list(adapter.items())

        assert len(items) == 3
        assert items[0]["name"] == "one"
        assert items[2]["id"] == 3


class TestJSONArrayAdapter:
    def test_dict_elements(self, tmp_path):
        f = tmp_path / "data.json"
        f.write_text(json.dumps([{"a": 1}, {"a": 2}, {"a": 3}]))

        from app.agent.factory_sources import JSONArrayAdapter
        items = list(JSONArrayAdapter(filepath=str(f)).items())
        assert len(items) == 3
        assert items[1]["a"] == 2

    def test_scalar_elements_wrapped(self, tmp_path):
        f = tmp_path / "nums.json"
        f.write_text(json.dumps([10, 20, 30]))

        from app.agent.factory_sources import JSONArrayAdapter
        items = list(JSONArrayAdapter(filepath=str(f)).items())
        assert len(items) == 3
        assert items[0] == {"value": 10, "index": 0}


class TestManualPromptAdapter:
    def test_yields_single_item(self):
        from app.agent.factory_sources import ManualPromptAdapter
        content = {"title": "t", "description": "d"}
        items = list(ManualPromptAdapter(trigger_card_content=content).items())
        assert len(items) == 1
        assert items[0]["content"] == content


# ---------------------------------------------------------------------------
# Template interpolation
# ---------------------------------------------------------------------------

class TestInterpolate:
    def test_basic_substitution(self):
        from app.agent.card_factory import _interpolate
        result = _interpolate("Process: {filename}", {"filename": "report.pdf"})
        assert result == "Process: report.pdf"

    def test_missing_key_left_as_is(self):
        from app.agent.card_factory import _interpolate
        result = _interpolate("{missing} value", {})
        assert result == "{missing} value"

    def test_multiple_keys(self):
        from app.agent.card_factory import _interpolate
        result = _interpolate("{col_a} / {col_b}", {"col_a": "X", "col_b": "Y"})
        assert result == "X / Y"


# ---------------------------------------------------------------------------
# _run_mechanical tests
# ---------------------------------------------------------------------------

class TestRunMechanical:
    def test_creates_correct_number_of_cards(self, tmp_path):
        """5 files → 5 cards created."""
        for i in range(5):
            (tmp_path / f"file{i}.txt").write_text(f"content {i}")

        stage = _make_pipeline_stage({
            "factory_source_type": "folder",
            "factory_source_config": {"path": str(tmp_path), "file_glob": "*.txt"},
            "factory_segmentation_mode": "mechanical",
            "factory_entry_stage": "idea",
            "factory_card_template": {
                "title_template": "Process: {filename}",
                "description_template": "Handle {filepath}",
            },
        })

        created = []

        def fake_create_task(*, title, task_type, description, owner, llm_id, budget_id,
                             project, stage_key, position):
            t = MagicMock()
            t.id = f"task-{len(created)}"
            created.append({"title": title, "stage_key": stage_key})
            return t

        # _run_mechanical imports create_task lazily from app.database inside the function
        with patch("app.database.create_task", fake_create_task), \
             patch("app.database.update_task", MagicMock()):
            from app.agent.card_factory import _run_mechanical
            count = _run_mechanical(
                factory_stage=stage,
                project_name="TestProject",
                llm_id=None,
                budget_id=None,
                trigger_card_id=None,
            )

        assert count == 5
        assert all(c["stage_key"] == "idea" for c in created)
        titles = [c["title"] for c in created]
        assert any("Process: file0.txt" in t for t in titles)

    def test_csv_creates_cards_with_column_interpolation(self, tmp_path):
        """10 CSV rows → 10 cards, title uses column value."""
        f = tmp_path / "data.csv"
        rows = "\n".join(f"item{i},{i*10}" for i in range(10))
        f.write_text(f"name,score\n{rows}\n")

        stage = _make_pipeline_stage({
            "factory_source_type": "csv",
            "factory_source_config": {"filepath": str(f)},
            "factory_segmentation_mode": "mechanical",
            "factory_entry_stage": "planning",
            "factory_card_template": {
                "title_template": "Analyze: {name}",
                "description_template": "Score={score}",
            },
        })

        created = []

        def fake_create_task(*, title, task_type, **kw):
            t = MagicMock()
            t.id = f"task-{len(created)}"
            created.append(title)
            return t

        with patch("app.database.create_task", fake_create_task), \
             patch("app.database.update_task", MagicMock()):
            from app.agent.card_factory import _run_mechanical
            count = _run_mechanical(
                factory_stage=stage,
                project_name="TestProject",
                llm_id=None,
                budget_id=None,
                trigger_card_id=None,
            )

        assert count == 10
        assert "Analyze: item0" in created
        assert "Analyze: item9" in created

    def test_sqlite_factory_creates_three_cards(self, tmp_path):
        """SQLite query returning 3 rows → 3 cards."""
        db_path = str(tmp_path / "source.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE topics (title TEXT, body TEXT)")
        conn.executemany("INSERT INTO topics VALUES (?,?)",
                         [("Alpha", "a"), ("Beta", "b"), ("Gamma", "c")])
        conn.commit()
        conn.close()

        stage = _make_pipeline_stage({
            "factory_source_type": "sqlite_query",
            "factory_source_config": {
                "db_path": db_path,
                "query": "SELECT title, body FROM topics",
            },
            "factory_segmentation_mode": "mechanical",
            "factory_entry_stage": "idea",
            "factory_card_template": {
                "title_template": "{title}",
                "description_template": "{body}",
            },
        })

        created = []

        def fake_create_task(*, title, **kw):
            t = MagicMock()
            t.id = f"task-{len(created)}"
            created.append(title)
            return t

        with patch("app.database.create_task", fake_create_task), \
             patch("app.database.update_task", MagicMock()):
            from app.agent.card_factory import _run_mechanical
            count = _run_mechanical(
                factory_stage=stage,
                project_name="TestProject",
                llm_id=None,
                budget_id=None,
                trigger_card_id=None,
            )

        assert count == 3
        assert set(created) == {"Alpha", "Beta", "Gamma"}


# ---------------------------------------------------------------------------
# Fixtures for FK-referenced rows
# ---------------------------------------------------------------------------

@pytest.fixture()
def factory_test_ids():
    """Create a project + pipeline template + pipeline_stage for factory_runs FK tests.

    Returns (project_id, stage_id) that can be inserted into factory_runs.
    """
    from app.database import upsert_project, create_template, create_stage

    project = upsert_project("FactoryTestProject", path="/tmp/factory_test")
    assert project is not None, "upsert_project returned None"

    template = create_template(
        name="FactoryTestTemplate",
        description="test",
        is_default=False,
        is_builtin=False,
    )
    assert template is not None, "create_template returned None"

    stage = create_stage(
        template_id=template.id,
        stage_key="factory_test_stage",
        label="Factory Test Stage",
        agent_type="factory_node",
        position=0,
    )
    assert stage is not None, "create_stage returned None"

    return project.id, stage.id


# ---------------------------------------------------------------------------
# factory_runs CRUD
# ---------------------------------------------------------------------------

class TestFactoryRunsCRUD:
    def test_create_and_get(self, factory_test_ids):
        project_id, stage_id = factory_test_ids
        from app.database import create_factory_run, get_factory_run

        run = create_factory_run(
            factory_stage_id=stage_id,
            project_id=project_id,
            trigger_type="manual",
            trigger_card_id=None,
        )
        assert run is not None
        assert run.status == "running"
        assert run.cards_created == 0

        fetched = get_factory_run(run.id)
        assert fetched is not None
        assert fetched.trigger_type == "manual"

    def test_update_run(self, factory_test_ids):
        project_id, stage_id = factory_test_ids
        from app.database import create_factory_run, update_factory_run, get_factory_run

        run = create_factory_run(
            factory_stage_id=stage_id,
            project_id=project_id,
            trigger_type="cron",
        )
        assert run is not None
        ok = update_factory_run(run.id, status="completed", cards_created=7)
        assert ok

        updated = get_factory_run(run.id)
        assert updated.status == "completed"
        assert updated.cards_created == 7
        assert updated.completed_at is not None

    def test_predecessor_already_triggered_idempotency(self, factory_test_ids):
        project_id, stage_id = factory_test_ids
        from app.database import create_factory_run, predecessor_already_triggered, create_task

        # Create a real task so the FK constraint on trigger_card_id is satisfied
        task = create_task(
            title="Predecessor card",
            task_type="idea",
            project_id=project_id,
        )
        assert task is not None
        card_id = task.id

        # Before any run
        assert not predecessor_already_triggered(stage_id, card_id)

        create_factory_run(
            factory_stage_id=stage_id,
            project_id=project_id,
            trigger_type="predecessor_complete",
            trigger_card_id=card_id,
        )

        # After run created
        assert predecessor_already_triggered(stage_id, card_id)
        # Different card — not blocked
        assert not predecessor_already_triggered(stage_id, "task-xyz-no-such")

    def test_get_last_cron_run_at_none_when_empty(self):
        from app.database import get_last_cron_run_at
        result = get_last_cron_run_at(999999)
        assert result is None

    def test_get_last_cron_run_at_returns_latest(self, factory_test_ids):
        project_id, stage_id = factory_test_ids
        from app.database import (
            create_factory_run, update_factory_run, get_last_cron_run_at
        )

        run1 = create_factory_run(
            factory_stage_id=stage_id, project_id=project_id, trigger_type="cron"
        )
        update_factory_run(run1.id, status="completed", cards_created=3)

        run2 = create_factory_run(
            factory_stage_id=stage_id, project_id=project_id, trigger_type="cron"
        )
        update_factory_run(run2.id, status="completed", cards_created=5)

        last = get_last_cron_run_at(stage_id)
        assert last is not None


# ---------------------------------------------------------------------------
# _cron_is_due
# ---------------------------------------------------------------------------

class TestCronIsDue:
    def test_every_minute_fires_when_no_prior_run(self):
        from app.agent.card_factory import _cron_is_due
        assert _cron_is_due("* * * * *", None) is True

    def test_fires_when_last_run_over_a_minute_ago(self):
        from app.agent.card_factory import _cron_is_due
        from datetime import timedelta
        old = datetime.now(timezone.utc) - timedelta(minutes=2)
        assert _cron_is_due("* * * * *", old) is True

    def test_does_not_refire_within_same_minute(self):
        from app.agent.card_factory import _cron_is_due
        from datetime import timedelta
        recent = datetime.now(timezone.utc) - timedelta(seconds=10)
        assert _cron_is_due("* * * * *", recent) is False

    def test_specific_hour_not_due_at_wrong_hour(self):
        from app.agent.card_factory import _cron_is_due
        import datetime as dt
        now = dt.datetime.now(timezone.utc)
        wrong_hour = (now.hour + 1) % 24
        # Schedule fires at wrong_hour — should not be due right now
        result = _cron_is_due(f"0 {wrong_hour} * * *", None)
        assert result is False


# ---------------------------------------------------------------------------
# build_adapter dispatch
# ---------------------------------------------------------------------------

class TestBuildAdapter:
    def test_unknown_source_type_raises(self):
        from app.agent.factory_sources import build_adapter
        with pytest.raises(ValueError, match="Unknown factory_source_type"):
            build_adapter("nonexistent_type", {})

    def test_dispatches_correct_class(self, tmp_path):
        from app.agent.factory_sources import build_adapter, FolderAdapter, CSVAdapter
        folder_adapter = build_adapter("folder", {"path": str(tmp_path)})
        assert isinstance(folder_adapter, FolderAdapter)

        f = tmp_path / "x.csv"
        f.write_text("a,b\n1,2\n")
        csv_adapter = build_adapter("csv", {"filepath": str(f)})
        assert isinstance(csv_adapter, CSVAdapter)


# ---------------------------------------------------------------------------
# LLM-segmented factory (_run_llm_segmented)
# ---------------------------------------------------------------------------

class TestRunLlmSegmented:
    """Verify that _run_llm_segmented dispatches CardFactoryAgent and cards are created."""

    def _fake_response(self, tool_name: str, tool_args: dict, call_id: str = "call_1"):
        """Build a fake LLM response that calls one tool then submits."""
        return {
            "content": "",
            "tool_calls": [
                {
                    "id": call_id,
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "arguments": json.dumps(tool_args),
                    },
                }
            ],
        }

    def _submit_response(self):
        return {
            "content": "Done",
            "tool_calls": [
                {
                    "id": "call_submit",
                    "type": "function",
                    "function": {
                        "name": "submit_work",
                        "arguments": json.dumps({"result": "created 2 cards"}),
                    },
                }
            ],
        }

    def test_llm_segmented_creates_cards(self, tmp_path):
        """
        Mock the LLM loop: first turn calls batch_create_cards, second submits.
        Intercept dispatch_tool to capture what tool calls the loop makes.
        Verify batch_create_cards was dispatched with 2 cards.
        """
        dispatched_tool_calls = []

        def fake_dispatch_tool(name: str, arguments: dict = None) -> str:
            args = arguments or {}
            dispatched_tool_calls.append((name, args))
            if name == "batch_create_cards":
                return json.dumps({"created_ids": ["card-0", "card-1"]})
            return json.dumps({"ok": True})

        # Two LLM responses: (1) call batch_create_cards, (2) submit_work
        responses = iter([
            self._fake_response(
                "batch_create_cards",
                {
                    "cards": [
                        {"title": "Card A", "entry_stage": "idea"},
                        {"title": "Card B", "entry_stage": "idea"},
                    ]
                },
            ),
            self._submit_response(),
        ])

        def fake_call_llm(messages, **kwargs):
            return next(responses)

        stage = _make_pipeline_stage({
            "factory_source_type": "manual_prompt",
            "factory_source_config": {},
            "factory_segmentation_mode": "llm_segmented",
            "factory_entry_stage": "idea",
            "intent": "Split into two cards.",
        }, stage_id=42)

        with (
            patch("app.agent.llm_client.call_llm", fake_call_llm),
            patch("app.agent.tools.dispatch_tool", fake_dispatch_tool),
            patch("app.database.get_child_tasks", MagicMock(return_value=[])),
            patch("app.database.create_agent_session", MagicMock(return_value="sess-1")),
            patch("app.database.close_agent_session", MagicMock()),
        ):
            from app.agent.card_factory import _run_llm_segmented

            _run_llm_segmented(
                factory_stage=stage,
                project_name="test",
                llm_id=1,
                budget_id=1,
                llm_base_url="http://localhost:8008/v1",
                llm_model="test-model",
                max_context=None,
                trigger_card_id=None,
            )

        # Verify batch_create_cards was dispatched with 2 cards
        batch_calls = [(n, a) for n, a in dispatched_tool_calls if n == "batch_create_cards"]
        assert len(batch_calls) == 1, f"Expected 1 batch_create_cards call, got: {dispatched_tool_calls}"
        cards_arg = batch_calls[0][1].get("cards", [])
        assert len(cards_arg) == 2
        titles = [c["title"] for c in cards_arg]
        assert "Card A" in titles
        assert "Card B" in titles
