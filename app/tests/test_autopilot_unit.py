"""
Unit tests for Phase 7 — Autopilot / Mission system.

Covers:
  - _should_autopilot_dispatch() hour-range logic (simple and overnight)
  - MissionState.check_termination() for each condition
  - project settings CRUD round-trip
  - /api/settings/autopilot GET and POST endpoints
  - /api/projects/{name}/settings GET and POST endpoints
"""

from __future__ import annotations

import sys
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient
from app.main import app

client = TestClient(app)


# ---------------------------------------------------------------------------
# _should_autopilot_dispatch hour logic
# ---------------------------------------------------------------------------

def _should_dispatch_at(hour: int, start: int, stop: int, autopilot: str = "on") -> bool:
    """Call the real helper with patched DB settings and a pinned UTC hour."""
    settings = {
        "maestro_autopilot": autopilot,
        "autopilot_start_hour": start,
        "autopilot_stop_hour": stop,
    }

    import app.agent.scheduler as sched
    # Patch the DB lookup used inside _should_autopilot_dispatch
    with patch("app.agent.scheduler.datetime") as mock_dt:
        mock_dt.utcnow.return_value = datetime(2025, 1, 1, hour, 0, 0)
        # Also let datetime() constructor work normally
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        with patch("app.database.get_system_setting",
                   side_effect=lambda k, d=None: settings.get(k, d)):
            # Re-import to pick up the patched function via lazy import inside helper
            import importlib
            importlib.reload(sys.modules.get("app.agent.scheduler", sched))
            return sched._should_autopilot_dispatch()


class TestShouldAutopilotDispatch:
    """Hour-range gate logic."""

    def test_off_returns_false(self):
        """Autopilot flag 'off' always blocks dispatch."""
        settings = {"maestro_autopilot": "off", "autopilot_start_hour": 0, "autopilot_stop_hour": 24}
        with patch("app.database.get_system_setting",
                   side_effect=lambda k, d=None: settings.get(k, d)):
            from app.agent.scheduler import _should_autopilot_dispatch
            assert _should_autopilot_dispatch() is False

    def test_no_schedule_always_on(self):
        """stop_hour=24 means no restriction — any hour passes."""
        settings = {"maestro_autopilot": "on", "autopilot_start_hour": 0, "autopilot_stop_hour": 24}
        with patch("app.database.get_system_setting",
                   side_effect=lambda k, d=None: settings.get(k, d)):
            from app.agent.scheduler import _should_autopilot_dispatch
            assert _should_autopilot_dispatch() is True

    def _gate(self, hour: int, start: int, stop: int) -> bool:
        """Helper: pin UTC hour and call the gate."""
        settings = {"maestro_autopilot": "on",
                    "autopilot_start_hour": start,
                    "autopilot_stop_hour": stop}
        with patch("app.database.get_system_setting",
                   side_effect=lambda k, d=None: settings.get(k, d)), \
             patch("app.agent.scheduler.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2025, 1, 1, hour, 0, 0)
            from app.agent.scheduler import _should_autopilot_dispatch
            return _should_autopilot_dispatch()

    def test_simple_range_inside(self):
        assert self._gate(10, 9, 17) is True

    def test_simple_range_outside(self):
        assert self._gate(20, 9, 17) is False

    def test_simple_range_edge_start_inclusive(self):
        assert self._gate(9, 9, 17) is True

    def test_simple_range_edge_stop_exclusive(self):
        assert self._gate(17, 9, 17) is False

    def test_overnight_before_midnight(self):
        # 23:00–07:00, at 23:00 → inside
        assert self._gate(23, 23, 7) is True

    def test_overnight_after_midnight(self):
        # 23:00–07:00, at 02:00 → inside
        assert self._gate(2, 23, 7) is True

    def test_overnight_at_midnight(self):
        # 23:00–07:00, at 00:00 → inside
        assert self._gate(0, 23, 7) is True

    def test_overnight_outside(self):
        # 23:00–07:00, at 10:00 → outside
        assert self._gate(10, 23, 7) is False


# ---------------------------------------------------------------------------
# MissionState.check_termination
# ---------------------------------------------------------------------------

from app.agent.scheduler import MissionConfig, MissionState


class TestMissionTermination:
    def test_no_conditions_returns_none(self):
        ms = MissionState(config=MissionConfig())
        assert ms.check_termination() is None

    def test_time_limit_not_yet(self):
        ms = MissionState(config=MissionConfig(time_limit_seconds=3600))
        ms.started_at = datetime(2025, 1, 1, 10, 0, 0)
        with patch("app.agent.scheduler.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2025, 1, 1, 10, 30, 0)
            assert ms.check_termination() is None

    def test_time_limit_fires(self):
        ms = MissionState(config=MissionConfig(time_limit_seconds=3600))
        ms.started_at = datetime(2025, 1, 1, 8, 0, 0)
        with patch("app.agent.scheduler.datetime") as mock_dt:
            mock_dt.utcnow.return_value = datetime(2025, 1, 1, 10, 0, 1)
            assert ms.check_termination() == "time_limit"

    def test_token_budget_not_yet(self):
        ms = MissionState(config=MissionConfig(token_budget=1000))
        ms.tokens_used = 999
        assert ms.check_termination() is None

    def test_token_budget_fires(self):
        ms = MissionState(config=MissionConfig(token_budget=1000))
        ms.tokens_used = 1000
        assert ms.check_termination() == "token_budget"

    def test_card_count_not_yet(self):
        ms = MissionState(config=MissionConfig(card_count_target=5))
        ms.completed_cards = 4
        assert ms.check_termination() is None

    def test_card_count_fires(self):
        ms = MissionState(config=MissionConfig(card_count_target=5))
        ms.completed_cards = 5
        assert ms.check_termination() == "card_count"

    def test_goal_card_fires(self):
        ms = MissionState(config=MissionConfig(goal_card_id="task-xyz"))
        mock_task = MagicMock()
        mock_task.type = "completed"
        import app.database as db_mod
        orig = db_mod.get_task
        db_mod.get_task = lambda tid: mock_task
        try:
            assert ms.check_termination() == "goal_card"
        finally:
            db_mod.get_task = orig

    def test_goal_card_not_completed(self):
        ms = MissionState(config=MissionConfig(goal_card_id="task-abc"))
        mock_task = MagicMock()
        mock_task.type = "indev"
        import app.database as db_mod
        orig = db_mod.get_task
        db_mod.get_task = lambda tid: mock_task
        try:
            assert ms.check_termination() is None
        finally:
            db_mod.get_task = orig


# ---------------------------------------------------------------------------
# Project settings CRUD  (uses conftest test DB via autouse _db_rollback)
# ---------------------------------------------------------------------------

from app.database import get_project_setting, set_project_setting, get_all_project_settings, upsert_project, get_project


class TestProjectSettingsCRUD:
    def test_get_missing_returns_default(self):
        val = get_project_setting(999999, "nonexistent_key", "fallback")
        assert val == "fallback"

    def test_set_and_get(self):
        upsert_project("_ap_test_proj", path="/tmp/ap_test")
        proj = get_project("_ap_test_proj")
        assert proj is not None
        set_project_setting(proj.id, "autopilot_override", "force_off")
        assert get_project_setting(proj.id, "autopilot_override") == "force_off"

    def test_upsert_overwrites(self):
        upsert_project("_ap_test_proj2", path="/tmp/ap_test2")
        proj = get_project("_ap_test_proj2")
        set_project_setting(proj.id, "k", "v1")
        set_project_setting(proj.id, "k", "v2")
        assert get_project_setting(proj.id, "k") == "v2"

    def test_get_all(self):
        upsert_project("_ap_test_proj3", path="/tmp/ap_test3")
        proj = get_project("_ap_test_proj3")
        set_project_setting(proj.id, "key_a", "1")
        set_project_setting(proj.id, "key_b", "2")
        all_s = get_all_project_settings(proj.id)
        assert all_s.get("key_a") == "1"
        assert all_s.get("key_b") == "2"


# ---------------------------------------------------------------------------
# API endpoint smoke tests
# ---------------------------------------------------------------------------

class TestAutopilotAPI:
    def test_get_autopilot(self):
        r = client.get("/api/settings/autopilot")
        assert r.status_code == 200
        d = r.json()
        assert "autopilot" in d
        assert "start_hour" in d
        assert "stop_hour" in d

    def test_set_autopilot_off(self):
        r = client.post("/api/settings/autopilot", json={"autopilot": "off"})
        assert r.status_code == 200
        assert r.json()["autopilot"] == "off"

    def test_set_autopilot_on_with_mission(self):
        r = client.post("/api/settings/autopilot", json={
            "autopilot": "on",
            "mission": {"time_limit_seconds": 3600},
        })
        assert r.status_code == 200
        assert r.json()["autopilot"] == "on"
        # Clean up
        client.post("/api/settings/autopilot", json={"autopilot": "off"})

    def test_invalid_autopilot_value(self):
        r = client.post("/api/settings/autopilot", json={"autopilot": "maybe"})
        assert r.status_code == 400

    def test_project_settings_not_found(self):
        r = client.get("/api/projects/__nonexistent_proj_xyz__/settings")
        assert r.status_code == 404

    def test_project_settings_crud(self):
        upsert_project("_api_ap_proj", path="/tmp/api_ap")
        r = client.post("/api/projects/_api_ap_proj/settings",
                        json={"autopilot_override": "force_off"})
        assert r.status_code == 200
        r2 = client.get("/api/projects/_api_ap_proj/settings")
        assert r2.status_code == 200
        assert r2.json().get("autopilot_override") == "force_off"
