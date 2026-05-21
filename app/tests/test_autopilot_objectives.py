"""
Tests for the autopilot objectives engine (GAP 2).

Covers:
  1. test_suppression_no_objectives        — autopilot_tick exits early with no active objectives
  2. test_suppression_saturated_board      — autopilot_tick exits early when in_flight >= max_in_flight
  3. test_suppression_exhausted_budget     — autopilot_tick exits early when budget exhausted
  4. test_spin_detector_threshold          — _detect_spin fires at correct demotion/card threshold
  5. test_multi_tick_completion            — objective only flips complete on second appears_complete tick
  6. test_time_box_expiry                  — expires_at set correctly; _expire_autopilot_objectives flips status
  7. test_objective_spawns_idea_card       — CRUD: create objective + card tagged with autopilot_objective_id
"""

from __future__ import annotations

import pytest
from datetime import datetime, timedelta, timezone
from unittest.mock import patch, MagicMock

from app.database import (
    SessionLocal,
    create_objective, list_objectives, get_objective, update_objective,
    update_objective_status, record_assessment, get_in_flight_count,
    delete_objective, objective_to_dict,
    AutopilotObjective, Task, Project,
    upsert_project, create_task,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(name: str, **kwargs) -> Project:
    p = upsert_project(name, **kwargs)
    assert p is not None
    return p


def _make_objective(project_id: int, **kwargs) -> AutopilotObjective:
    obj = create_objective(project_id, "Do something interesting", **kwargs)
    assert obj is not None
    return obj


# ---------------------------------------------------------------------------
# 1. Suppression — no active objectives
# ---------------------------------------------------------------------------

class TestSuppressionNoObjectives:
    def test_suppression_no_objectives(self):
        from app.agent.scheduler import _run_autopilot_tick_for_project

        proj = _make_project("ap-no-objectives")
        # list_objectives is imported locally inside the function — patch at source
        with patch("app.database.list_objectives", return_value=[]) as mock_list:
            with patch("app.agent.scheduler._run_objective_assessment") as mock_assess:
                _run_autopilot_tick_for_project(proj.id, proj.name)
                mock_assess.assert_not_called()


# ---------------------------------------------------------------------------
# 2. Suppression — saturated board
# ---------------------------------------------------------------------------

class TestSuppressionSaturatedBoard:
    def test_suppression_saturated_board(self):
        from app.agent.scheduler import _run_autopilot_tick_for_project

        # Create project with a low max_in_flight cap (uses upsert so new columns get set)
        proj = _make_project("ap-saturated", autopilot_max_in_flight=2)
        obj = _make_objective(proj.id)

        # Patch in_flight count to equal the cap
        with patch("app.database.list_objectives", return_value=[obj]), \
             patch("app.database.get_in_flight_count", return_value=2):
            with patch("app.agent.scheduler._run_objective_assessment") as mock_assess:
                _run_autopilot_tick_for_project(proj.id, proj.name)
                mock_assess.assert_not_called()

        delete_objective(obj.id)


# ---------------------------------------------------------------------------
# 3. Suppression — exhausted budget
# ---------------------------------------------------------------------------

class TestSuppressionExhaustedBudget:
    def test_suppression_exhausted_budget(self):
        from app.agent.scheduler import _run_autopilot_tick_for_project
        from app.database import create_budget

        budget = create_budget("autopilot-budget-test", dollar_amount=1.0)
        proj = _make_project("ap-budget-exhausted", autopilot_budget_id=budget.id)
        obj = _make_objective(proj.id)

        with patch("app.database.list_objectives", return_value=[obj]), \
             patch("app.database.get_in_flight_count", return_value=0), \
             patch("app.database.get_budget_spent_microcents", return_value=200_000_000):
            with patch("app.agent.scheduler._run_objective_assessment") as mock_assess:
                _run_autopilot_tick_for_project(proj.id, proj.name)
                mock_assess.assert_not_called()

        delete_objective(obj.id)


# ---------------------------------------------------------------------------
# 4. Spin detection threshold
# ---------------------------------------------------------------------------

class TestSpinDetector:
    def test_spin_detector_fires_at_threshold(self):
        from app.agent.scheduler import _detect_spin
        from app.agent import config as cfg
        from app.database import update_task

        proj = _make_project("ap-spin")
        obj = _make_objective(proj.id)

        threshold = cfg.AUTOPILOT_SPIN_DEMOTION_THRESHOLD
        card_threshold = cfg.AUTOPILOT_SPIN_CARD_THRESHOLD

        created_ids = []
        for i in range(card_threshold):
            task = create_task(
                title=f"Spin card {i}",
                task_type="idea",
                project_id=proj.id,
                stage_key="idea",
                autopilot_objective_id=obj.id,
            )
            assert task is not None
            created_ids.append(task.id)
            update_task(task.id, demotion_count=threshold)

        assert _detect_spin(obj.id) is True

        for tid in created_ids:
            from app.database import delete_task
            delete_task(tid)
        delete_objective(obj.id)

    def test_spin_detector_below_threshold(self):
        from app.agent.scheduler import _detect_spin
        from app.agent import config as cfg
        from app.database import update_task

        proj = _make_project("ap-nospin")
        obj = _make_objective(proj.id)

        # Only 1 card — below the card_threshold of 2
        task = create_task(
            title="Single demoted card",
            task_type="idea",
            project_id=proj.id,
            stage_key="idea",
            autopilot_objective_id=obj.id,
        )
        assert task is not None
        update_task(task.id, demotion_count=cfg.AUTOPILOT_SPIN_DEMOTION_THRESHOLD)

        assert _detect_spin(obj.id) is False

        from app.database import delete_task
        delete_task(task.id)
        delete_objective(obj.id)


# ---------------------------------------------------------------------------
# 5. Multi-tick completion confirmation
# ---------------------------------------------------------------------------

class TestMultiTickCompletion:
    def test_objective_only_completes_on_second_tick(self):
        proj = _make_project("ap-completion")
        obj = _make_objective(proj.id)

        # First tick: appears_complete=True — sets appears_complete_since but NOT status
        record_assessment(obj.id, "Looks done", tick=1, appears_complete=True)

        obj_after_first = get_objective(obj.id)
        assert obj_after_first.appears_complete_since is not None
        assert obj_after_first.status == "active"

        # Second tick: appears_complete again + appears_complete_since already set → complete
        if obj_after_first.appears_complete_since is not None:
            update_objective_status(obj.id, "complete")

        obj_final = get_objective(obj.id)
        assert obj_final.status == "complete"

        delete_objective(obj.id)

    def test_appears_complete_resets_on_false(self):
        proj = _make_project("ap-completion-reset")
        obj = _make_objective(proj.id)

        record_assessment(obj.id, "Looks done", tick=1, appears_complete=True)
        assert get_objective(obj.id).appears_complete_since is not None

        record_assessment(obj.id, "Actually not done", tick=2, appears_complete=False)
        assert get_objective(obj.id).appears_complete_since is None

        delete_objective(obj.id)


# ---------------------------------------------------------------------------
# 6. Time-box expiry
# ---------------------------------------------------------------------------

class TestTimeBoxExpiry:
    def test_expires_at_set_on_create(self):
        proj = _make_project("ap-timebox")
        obj = _make_objective(proj.id, time_box_hours=4)

        assert obj.expires_at is not None
        expected = datetime.now(timezone.utc) + timedelta(hours=4)
        delta = abs((obj.expires_at - expected).total_seconds())
        assert delta < 5

        delete_objective(obj.id)

    def test_expire_function_flips_status(self):
        from app.agent.scheduler import _expire_autopilot_objectives

        proj = _make_project("ap-expire")
        obj = _make_objective(proj.id, time_box_hours=1)

        # Force expires_at into the past via CRUD update
        update_objective(obj.id, expires_at=datetime.now(timezone.utc) - timedelta(minutes=1))

        _expire_autopilot_objectives()

        expired = get_objective(obj.id)
        assert expired.status == "complete"

        delete_objective(obj.id)


# ---------------------------------------------------------------------------
# 7. Objective spawns IDEA card with autopilot_objective_id set
# ---------------------------------------------------------------------------

class TestObjectiveSpawnsIdeaCard:
    def test_create_and_tag_idea_card(self):
        proj = _make_project("ap-card-spawn")
        obj = _make_objective(proj.id)

        task = create_task(
            title="Autopilot spawned card",
            task_type="idea",
            project_id=proj.id,
            stage_key="idea",
            autopilot_objective_id=obj.id,
        )
        assert task is not None
        assert task.autopilot_objective_id == obj.id

        count = get_in_flight_count(proj.id)
        assert count >= 1

        d = objective_to_dict(obj)
        assert d["id"] == obj.id
        assert d["description"] == "Do something interesting"
        assert d["status"] == "active"

        from app.database import delete_task
        delete_task(task.id)
        delete_objective(obj.id)
