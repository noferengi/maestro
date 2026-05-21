"""
Tests for GAP 10 — Multi-model routing by task type.

Covers:
  - resolve_llm_for_task resolution order (human-pin, routing table, project default, ini fallback)
  - _check_model_block_timeout (under threshold / over threshold)
  - Routing CRUD API round-trip
  - llm_pinned flag behavior via task edit API
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.agent.config import resolve_llm_for_task
from app.database import (
    get_routing_table,
    upsert_routing_entry,
    delete_routing_entry,
    get_task,
    upsert_project,
    create_task,
    get_project,
)
from app.database.crud_tasks import mark_dispatch_waiting, set_task_blocked_on_model
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id, project_id, llm_id=None, llm_pinned=False, stage_key="planning"):
    t = MagicMock()
    t.id = task_id
    t.project_id = project_id
    t.llm_id = llm_id
    t.llm_pinned = llm_pinned
    t.stage_key = stage_key
    t.type = stage_key
    return t


def _make_project(project_id, llm_id=None):
    p = MagicMock()
    p.id = project_id
    p.llm_id = llm_id
    return p


# ---------------------------------------------------------------------------
# Phase 1 — resolve_llm_for_task
# ---------------------------------------------------------------------------

def test_resolve_llm_human_pinned():
    """Human-pinned task returns its own llm_id regardless of routing table."""
    task = _make_task("t1", project_id=1, llm_id=5, llm_pinned=True)
    with patch("app.database.crud_projects.get_routing_table", return_value={"planning": 3}):
        with patch("app.database.crud_projects.get_project_by_id", return_value=_make_project(1, llm_id=2)):
            result = resolve_llm_for_task(task, "planning")
    assert result == 5


def test_resolve_llm_routing_table_wins():
    """Routing table entry beats project default."""
    task = _make_task("t2", project_id=1, llm_id=None, llm_pinned=False)
    with patch("app.database.crud_projects.get_routing_table", return_value={"planning": 3}):
        with patch("app.database.crud_projects.get_project_by_id", return_value=_make_project(1, llm_id=1)):
            result = resolve_llm_for_task(task, "planning")
    assert result == 3


def test_resolve_llm_project_default():
    """No routing entry — project default is returned."""
    task = _make_task("t3", project_id=1, llm_id=None, llm_pinned=False)
    with patch("app.database.crud_projects.get_routing_table", return_value={}):
        with patch("app.database.crud_projects.get_project_by_id", return_value=_make_project(1, llm_id=2)):
            result = resolve_llm_for_task(task, "planning")
    assert result == 2


def test_resolve_llm_ini_fallback():
    """No routing, no project llm_id — returns DEFAULT_LLM_ID."""
    task = _make_task("t4", project_id=1, llm_id=None, llm_pinned=False)
    with patch("app.database.crud_projects.get_routing_table", return_value={}):
        with patch("app.database.crud_projects.get_project_by_id", return_value=_make_project(1, llm_id=None)):
            with patch("app.agent.config.DEFAULT_LLM_ID", 99):
                result = resolve_llm_for_task(task, "planning")
    assert result == 99


def test_resolve_llm_no_project():
    """Task with no project_id falls through to DEFAULT_LLM_ID."""
    task = _make_task("t5", project_id=None, llm_id=None, llm_pinned=False)
    with patch("app.agent.config.DEFAULT_LLM_ID", 7):
        result = resolve_llm_for_task(task, "planning")
    assert result == 7


# ---------------------------------------------------------------------------
# Phase 2 — _check_model_block_timeout (unit — mocked DB)
# ---------------------------------------------------------------------------

def test_block_timeout_sets_waiting_when_none():
    """When dispatch_waiting_since is None, mark_dispatch_waiting is called."""
    from app.agent.scheduler import _check_model_block_timeout

    mock_task = MagicMock()
    mock_task.dispatch_waiting_since = None

    # _check_model_block_timeout uses local imports — patch at the source module
    with patch("app.database.get_task", return_value=mock_task):
        with patch("app.database.crud_tasks.mark_dispatch_waiting") as mw:
            with patch("app.database.crud_tasks.set_task_blocked_on_model") as mb:
                _check_model_block_timeout("t1", 3)
                mw.assert_called_once_with("t1")
                mb.assert_not_called()


def test_block_timeout_under_threshold():
    """Task waiting 1 min, threshold 30 min — no blocked_on_model_id set."""
    from app.agent.scheduler import _check_model_block_timeout

    mock_task = MagicMock()
    mock_task.dispatch_waiting_since = datetime.utcnow() - timedelta(minutes=1)

    with patch("app.database.get_task", return_value=mock_task):
        with patch("app.database.crud_tasks.mark_dispatch_waiting") as mw:
            with patch("app.database.crud_tasks.set_task_blocked_on_model") as mb:
                with patch("app.agent.config.MODEL_BLOCK_TIMEOUT_MINUTES", 30):
                    _check_model_block_timeout("t1", 3)
                    mw.assert_not_called()
                    mb.assert_not_called()


def test_block_timeout_over_threshold():
    """Task waiting 31 min, threshold 30 min — blocked_on_model_id should be set."""
    from app.agent.scheduler import _check_model_block_timeout

    mock_task = MagicMock()
    mock_task.dispatch_waiting_since = datetime.utcnow() - timedelta(minutes=31)

    with patch("app.database.get_task", return_value=mock_task):
        with patch("app.database.crud_tasks.mark_dispatch_waiting") as mw:
            with patch("app.database.crud_tasks.set_task_blocked_on_model") as mb:
                with patch("app.agent.config.MODEL_BLOCK_TIMEOUT_MINUTES", 30):
                    _check_model_block_timeout("t1", 3)
                    mw.assert_not_called()
                    mb.assert_called_once_with("t1", 3)


# ---------------------------------------------------------------------------
# Phase 3 — Routing CRUD via API
# ---------------------------------------------------------------------------

def _get_or_create_project(name="RoutingTestProject"):
    p = get_project(name)
    if not p:
        p = upsert_project(name, path=None, llm_id=None, budget_id=None)
    return p


def test_routing_crud_upsert_get_delete():
    """Round-trip: upsert → get → verify → delete → verify gone."""
    from app.database import create_llm, get_all_llms

    project = _get_or_create_project("RoutingCRUDTest")
    assert project is not None

    # Reuse an existing LLM or create one for the test
    llms = get_all_llms()
    if llms:
        llm_id_a, llm_id_b = llms[0].id, llms[-1].id
    else:
        llm_a = create_llm(address="127.0.0.1", port=9901, model="test-model-a")
        llm_b = create_llm(address="127.0.0.1", port=9902, model="test-model-b")
        assert llm_a is not None
        assert llm_b is not None
        llm_id_a, llm_id_b = llm_a.id, llm_b.id

    upsert_routing_entry(project.id, "planning", llm_id_a)
    table = get_routing_table(project.id)
    assert table.get("planning") == llm_id_a

    # Second upsert on the same stage should update (not duplicate)
    upsert_routing_entry(project.id, "planning", llm_id_b)
    table = get_routing_table(project.id)
    assert table.get("planning") == llm_id_b

    deleted = delete_routing_entry(project.id, "planning")
    assert deleted is True
    table = get_routing_table(project.id)
    assert "planning" not in table

    # Delete a non-existent entry returns False
    deleted = delete_routing_entry(project.id, "planning")
    assert deleted is False


def test_api_routing_roundtrip():
    """PUT → GET → DELETE → GET (via HTTP API)."""
    from app.database import get_all_llms, create_llm
    project = _get_or_create_project("APIRoutingTest")
    name = project.name

    llms = get_all_llms()
    llm_id = llms[0].id if llms else create_llm(
        address="127.0.0.1", port=9910, model="api-routing-test-model"
    ).id

    put_resp = client.put(f"/api/projects/{name}/routing/planning",
                          json={"llm_id": llm_id})
    assert put_resp.status_code == 200
    assert put_resp.json().get("ok") is True

    get_resp = client.get(f"/api/projects/{name}/routing")
    assert get_resp.status_code == 200
    table = get_resp.json()
    assert table.get("planning") == llm_id

    del_resp = client.delete(f"/api/projects/{name}/routing/planning")
    assert del_resp.status_code == 200

    get_resp2 = client.get(f"/api/projects/{name}/routing")
    assert "planning" not in get_resp2.json()


# ---------------------------------------------------------------------------
# Phase 4 — llm_pinned flag via task edit API
# ---------------------------------------------------------------------------

def test_llm_pinned_set_on_manual_edit():
    """PUT /api/tasks/{id} with llm_id sets llm_pinned=True."""
    from app.database import get_all_llms, get_all_budgets, create_llm, create_budget
    llms = get_all_llms()
    llm = llms[0] if llms else create_llm(
        address="127.0.0.1", port=9911, model="pin-test-model"
    )
    budgets = get_all_budgets()
    budget = budgets[0] if budgets else create_budget("pin-test-budget")

    project = _get_or_create_project("PinnedFlagTest")
    task = create_task(
        title="Pin test task",
        task_type="planning",
        description="testing pin",
        project_id=project.id,
        llm_id=None,
        budget_id=budget.id,
    )
    assert task is not None

    resp = client.put(f"/api/tasks/{task.id}", json={"llm_id": llm.id})
    assert resp.status_code == 200

    updated = get_task(task.id)
    assert updated.llm_pinned is True
    assert updated.llm_id == llm.id
