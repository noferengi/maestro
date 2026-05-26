"""
Unit tests for Operation Fury Phase 8: multiplier_node / fan_out_child / fan_out_collapser.
"""
from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_task(task_id="t1", title="Test Task", content=None, project_id=1,
               prerequisites=None):
    t = MagicMock()
    t.id = task_id
    t.title = title
    t.description = "desc"
    t.content = content or {}
    t.project_id = project_id
    t.prerequisites = prerequisites or []
    t.project = "TestProject"
    return t


def _make_stage_config(agent_type="multiplier_node", stage_key="my_mult", config=None):
    from app.agent.pipeline_router import StageConfig
    return StageConfig(
        stage_key=stage_key,
        label=stage_key,
        agent_type=agent_type,
        position=0,
        config=config or {},
        template_id=1,
        stage_id=10,
    )


# ---------------------------------------------------------------------------
# multiplier_node tests
# ---------------------------------------------------------------------------

class TestMultiplierNode:
    """_run_multiplier_node creates N child tasks + 1 collapser and updates parent prereqs."""

    def _run(self, config, parent_content=None):
        from app.agent.stage_executors import _run_multiplier_node

        parent = _make_task(content=parent_content or {})
        created_tasks = []

        def fake_get_task(tid):
            return parent  # all get_task calls return parent for simplicity

        def fake_create_task(**kwargs):
            task_id = f"task-{len(created_tasks)}"
            t = _make_task(task_id=task_id)
            created_tasks.append(kwargs)
            return t

        update_calls = []

        with (
            patch("app.database.get_task", side_effect=fake_get_task),
            patch("app.database.update_task", side_effect=lambda tid, **kw: update_calls.append(kw)),
            patch("app.database.create_task", side_effect=fake_create_task),
            patch("app.agent.stage_executors._build_required_keys_preamble", return_value=""),
        ):
            cfg = _make_stage_config(config=config)
            _run_multiplier_node("t1", cfg, "http://llm", "model", 4096, 1, 1, None)

        return created_tasks, update_calls

    def test_creates_n_children_scalar_mode(self):
        tasks, _ = self._run({"n": 3, "agent_system_prompt": "Vote!", "collapser_mode": "vote_tally"})
        child_types = [k.get("task_type") for k in tasks]
        assert child_types.count("_fan_out_child") == 3
        assert child_types.count("_fan_out_collapser") == 1

    def test_per_agent_mode_uses_agent_names(self):
        agents_cfg = [
            {"name": "Reviewer Alpha", "system_prompt": "Be strict."},
            {"name": "Reviewer Beta",  "system_prompt": "Be lenient."},
        ]
        tasks, _ = self._run({"agents": agents_cfg, "collapser_mode": "vote_tally"})
        child_tasks = [k for k in tasks if k.get("task_type") == "_fan_out_child"]
        assert len(child_tasks) == 2
        names = [k["content"]["_fan_out_cfg"]["name"] for k in child_tasks]
        assert "Reviewer Alpha" in names
        assert "Reviewer Beta" in names

    def test_idempotency_guard_skips_if_children_exist(self):
        tasks, _ = self._run({"n": 2}, parent_content={"_multiplier_child_ids": ["existing"]})
        assert len(tasks) == 0, "Should create no tasks when idempotency guard fires"

    def test_collapser_has_child_prerequisites(self):
        tasks, _ = self._run({"n": 2})
        children = [k for k in tasks if k.get("task_type") == "_fan_out_child"]
        collapser = next((k for k in tasks if k.get("task_type") == "_fan_out_collapser"), None)
        assert collapser is not None
        # Collapser prerequisites should reference child task IDs
        # (tasks are created with IDs "task-0", "task-1", "task-2")
        prereqs = collapser.get("prerequisites", [])
        assert len(prereqs) == len(children), "Collapser should have one prerequisite per child"

    def test_collapser_cfg_includes_parent_info(self):
        tasks, _ = self._run({"n": 2, "output_key": "my_votes", "collapser_mode": "vote_tally"})
        collapser = next((k for k in tasks if k.get("task_type") == "_fan_out_collapser"), None)
        assert collapser is not None
        cfg = collapser["content"]["_collapser_cfg"]
        assert cfg["parent_task_id"] == "t1"
        assert cfg["output_key"] == "my_votes"
        assert cfg["collapser_mode"] == "vote_tally"


# ---------------------------------------------------------------------------
# _fan_out_collapser — vote_tally mode
# ---------------------------------------------------------------------------

class TestFanOutCollapsVoteTally:

    def _run_collapser(self, submissions, tally_strategy="majority", on_tie="reject"):
        from app.agent.stage_executors import _run_fan_out_collapser

        child_tasks = [_make_task(task_id=f"c{i}", content={"submission": s})
                       for i, s in enumerate(submissions)]
        child_ids = [c.id for c in child_tasks]
        collapser = _make_task(
            task_id="col-1",
            content={"_collapser_cfg": {
                "parent_task_id": "parent-1",
                "parent_stage_key": "my_mult",
                "child_ids": child_ids,
                "collapser_mode": "vote_tally",
                "tally_strategy": tally_strategy,
                "on_tie": on_tie,
                "output_key": "vote_result",
                "judge_system_prompt": "",
                "judge_max_turns": 5,
            }},
        )
        parent = _make_task(task_id="parent-1", content={})
        task_map = {c.id: c for c in child_tasks}
        task_map["col-1"] = collapser
        task_map["parent-1"] = parent

        advance_args = []

        db_mock = MagicMock()
        db_mock.add = MagicMock()
        db_mock.commit = MagicMock()
        db_mock.rollback = MagicMock()
        db_mock.close = MagicMock()

        with (
            patch("app.database.get_task", side_effect=lambda tid: task_map.get(tid)),
            patch("app.database.update_task"),
            patch("app.database.create_agent_session", return_value="sess-1"),
            patch("app.database.close_agent_session"),
            patch("app.agent.stage_executors.advance_stage",
                  side_effect=lambda tid, cond, **kw: advance_args.append(cond)),
            patch("app.database.session.SessionLocal", return_value=db_mock),
        ):
            _run_fan_out_collapser(
                "col-1",
                _make_stage_config(agent_type="_fan_out_collapser", stage_key="_fan_out_collapser"),
                "http://llm", "model", 4096, 1, 1, None,
            )

        return advance_args

    def test_majority_all_accepted_advances_pass(self):
        submissions = [
            {"verdict": "ACCEPTED", "confidence": 95, "justification": "Good"},
            {"verdict": "ACCEPTED", "confidence": 92, "justification": "Fine"},
            {"verdict": "ACCEPTED", "confidence": 93, "justification": "OK"},
        ]
        result = self._run_collapser(submissions)
        assert result == ["pass"], f"Expected pass, got {result}"

    def test_majority_any_rejected_fails(self):
        # tally_votes() (used by majority mode) rejects immediately on any REJECTED vote.
        # This matches voting_panel behaviour — majority is about tie-breaking pass-ish
        # outcomes, not about outvoting REJECTED verdicts.
        submissions = [
            {"verdict": "ACCEPTED", "confidence": 95, "justification": "Good"},
            {"verdict": "ACCEPTED", "confidence": 92, "justification": "Fine"},
            {"verdict": "REJECTED", "confidence": 10, "justification": "Bad"},
        ]
        result = self._run_collapser(submissions, tally_strategy="majority")
        assert result == ["fail"], f"Any REJECTED in majority mode should still fail, got {result}"

    def test_veto_any_rejected_fails(self):
        submissions = [
            {"verdict": "ACCEPTED", "confidence": 95, "justification": "Good"},
            {"verdict": "ACCEPTED", "confidence": 92, "justification": "Fine"},
            {"verdict": "REJECTED", "confidence": 10, "justification": "Critical flaw"},
        ]
        result = self._run_collapser(submissions, tally_strategy="veto")
        assert result == ["fail"], f"Veto strategy should fail on any REJECTED, got {result}"

    def test_all_rejected_fails(self):
        submissions = [
            {"verdict": "REJECTED", "confidence": 5, "justification": "No"},
            {"verdict": "REJECTED", "confidence": 2, "justification": "No"},
        ]
        result = self._run_collapser(submissions)
        assert result == ["fail"], f"All rejected should fail, got {result}"


# ---------------------------------------------------------------------------
# _fan_out_collapser — judge_select mode
# ---------------------------------------------------------------------------

class TestFanOutCollapsJudgeSelect:

    def test_judge_select_writes_winning_proposal(self):
        import asyncio
        from app.agent.stage_executors import _run_fan_out_collapser

        submissions = [
            {"proposal": "Approach A", "detail": "fast"},
            {"proposal": "Approach B", "detail": "thorough"},
        ]
        child_tasks = [_make_task(task_id=f"c{i}", content={"submission": s})
                       for i, s in enumerate(submissions)]
        child_ids = [c.id for c in child_tasks]
        collapser = _make_task(
            task_id="col-1",
            content={"_collapser_cfg": {
                "parent_task_id": "parent-1",
                "parent_stage_key": "my_mult",
                "child_ids": child_ids,
                "collapser_mode": "judge_select",
                "output_key": "winning_proposal",
                "judge_system_prompt": "Pick the best.",
                "judge_max_turns": 5,
                "tally_strategy": "majority",
                "on_tie": "reject",
            }},
        )
        parent = _make_task(task_id="parent-1", content={})
        task_map = {c.id: c for c in child_tasks}
        task_map["col-1"] = collapser
        task_map["parent-1"] = parent

        updated_parent_content = {}
        advance_cond = []

        async def fake_judge_run(self_agent):
            return {"selected_index": 1, "rationale": "B is better"}

        with (
            patch("app.database.get_task", side_effect=lambda tid: task_map.get(tid)),
            patch("app.database.update_task",
                  side_effect=lambda tid, **kw: updated_parent_content.update(kw.get("content", {}))),
            patch("app.database.create_agent_session", return_value="sess-1"),
            patch("app.database.close_agent_session"),
            patch("app.agent.stage_executors.advance_stage",
                  side_effect=lambda tid, cond, **kw: advance_cond.append(cond)),
            patch("app.agent.stage_executors._CollectorAgent.run", fake_judge_run),
        ):
            _run_fan_out_collapser(
                "col-1",
                _make_stage_config(agent_type="_fan_out_collapser", stage_key="_fan_out_collapser"),
                "http://llm", "model", 4096, 1, 1, None,
            )

        assert advance_cond == ["pass"], f"Judge select should always advance pass, got {advance_cond}"
        assert updated_parent_content.get("winning_proposal") == submissions[1]


# ---------------------------------------------------------------------------
# Registry checks
# ---------------------------------------------------------------------------

def test_multiplier_node_in_registry():
    from app.agent.agent_registry import AGENT_REGISTRY
    assert "multiplier_node" in AGENT_REGISTRY
    assert "_fan_out_child" in AGENT_REGISTRY
    assert "_fan_out_collapser" in AGENT_REGISTRY


def test_multiplier_node_registered_as_executor():
    """The executor must be registered in the pipeline router's handler map."""
    # Import scheduler to trigger all _reg_executor calls
    import app.agent.scheduler  # noqa: F401
    from app.agent.pipeline_router import _agent_type_executors
    assert "multiplier_node" in _agent_type_executors
    assert "_fan_out_child" in _agent_type_executors
    assert "_fan_out_collapser" in _agent_type_executors
