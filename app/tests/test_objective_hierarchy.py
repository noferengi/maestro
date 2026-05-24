"""
Tests for Gap 4 — Goal Memory / Persistent Objectives.

Covers:
  1. test_complete_objective_cascade       — completing last active child auto-completes parent
  2. test_complete_objective_no_cascade    — completing one of two children does NOT complete parent
  3. test_append_evidence_accumulates      — append twice; both entries present in output
  4. test_append_evidence_does_not_overwrite — append 3×; all three entries survive
  5. test_get_evidence_placeholder         — no evidence returns placeholder string
  6. test_objective_tree_structure         — get_objective_tree returns nested dict
  7. test_objective_to_dict_includes_new_fields — parent_id and created_by in dict
  8. test_prompt_injection_with_objective  — task with objective_id gets injection block
  9. test_prompt_injection_without_objective — task without objective_id gets no injection
"""

from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.database import (
    SessionLocal,
    create_objective, list_objectives, get_objective, complete_objective,
    get_objective_tree, append_objective_evidence, get_objective_evidence,
    objective_to_dict,
    AutopilotObjective, Project,
    upsert_project, create_task,
    store_document, get_document,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project(name: str) -> Project:
    p = upsert_project(name)
    assert p is not None
    return p


def _make_obj(project_id: int, description: str = "Test objective", **kwargs) -> AutopilotObjective:
    obj = create_objective(project_id, description, **kwargs)
    assert obj is not None
    return obj


# ---------------------------------------------------------------------------
# 1. Cascade completion — last child completes parent
# ---------------------------------------------------------------------------

class TestCompletionCascade:
    def test_complete_objective_cascade(self):
        p = _make_project("test-cascade-1")
        parent = _make_obj(p.id, "Parent objective")
        child1 = _make_obj(p.id, "Child 1", parent_id=parent.id)
        child2 = _make_obj(p.id, "Child 2", parent_id=parent.id)

        complete_objective(child1.id)

        # Parent still active — child2 not done yet
        parent_now = get_objective(parent.id)
        assert parent_now.status == "active"

        complete_objective(child2.id)

        # Now parent should be cascaded to complete
        parent_final = get_objective(parent.id)
        assert parent_final.status == "complete"
        assert parent_final.completed_at is not None

    def test_complete_objective_no_cascade(self):
        p = _make_project("test-cascade-2")
        parent = _make_obj(p.id, "Parent")
        child1 = _make_obj(p.id, "Child 1", parent_id=parent.id)
        _make_obj(p.id, "Child 2", parent_id=parent.id)  # not completed

        complete_objective(child1.id)

        parent_now = get_objective(parent.id)
        assert parent_now.status == "active"

    def test_complete_objective_idempotent(self):
        p = _make_project("test-cascade-3")
        obj = _make_obj(p.id, "Solo objective")
        complete_objective(obj.id)
        complete_objective(obj.id)  # second call should be a no-op
        result = get_objective(obj.id)
        assert result.status == "complete"


# ---------------------------------------------------------------------------
# 2. Evidence log helpers
# ---------------------------------------------------------------------------

class TestEvidenceLog:
    def test_append_evidence_accumulates(self):
        p = _make_project("test-evidence-1")
        obj = _make_obj(p.id, "Evidence test")

        append_objective_evidence(obj.id, "First finding")
        append_objective_evidence(obj.id, "Second finding")

        evidence = get_objective_evidence(obj.id)
        assert "First finding" in evidence
        assert "Second finding" in evidence

    def test_append_evidence_does_not_overwrite(self):
        p = _make_project("test-evidence-2")
        obj = _make_obj(p.id, "Overwrite test")

        append_objective_evidence(obj.id, "Entry A")
        append_objective_evidence(obj.id, "Entry B")
        append_objective_evidence(obj.id, "Entry C")

        evidence = get_objective_evidence(obj.id)
        assert "Entry A" in evidence
        assert "Entry B" in evidence
        assert "Entry C" in evidence

    def test_append_evidence_includes_timestamp(self):
        p = _make_project("test-evidence-3")
        obj = _make_obj(p.id, "Timestamp test")

        append_objective_evidence(obj.id, "Timestamped entry")

        evidence = get_objective_evidence(obj.id)
        # Timestamp format: YYYY-MM-DD HH:MM UTC
        import re
        assert re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2} UTC", evidence)

    def test_get_evidence_placeholder_when_empty(self):
        p = _make_project("test-evidence-4")
        obj = _make_obj(p.id, "No evidence yet")

        evidence = get_objective_evidence(obj.id)
        assert evidence == "(no evidence recorded yet)"

    def test_get_evidence_missing_objective(self):
        evidence = get_objective_evidence(99999999)
        assert evidence == "(objective not found)"

    def test_append_evidence_uses_correct_doc_key(self):
        p = _make_project("test-evidence-5")
        obj = _make_obj(p.id, "Key check")

        append_objective_evidence(obj.id, "Some note")

        # Verify the document key convention
        doc = get_document(p.id, f"objective:{obj.id}:evidence")
        assert doc is not None
        assert "Some note" in doc["content"]

    def test_append_evidence_missing_objective_returns_false(self):
        result = append_objective_evidence(99999999, "should fail")
        assert result is False


# ---------------------------------------------------------------------------
# 3. Objective tree
# ---------------------------------------------------------------------------

class TestObjectiveTree:
    def test_tree_structure(self):
        p = _make_project("test-tree-1")
        parent = _make_obj(p.id, "Root")
        child1 = _make_obj(p.id, "Child 1", parent_id=parent.id)
        child2 = _make_obj(p.id, "Child 2", parent_id=parent.id)

        tree = get_objective_tree(p.id)

        assert len(tree) == 1
        root = tree[0]
        assert root["id"] == parent.id
        assert len(root["children"]) == 2
        child_ids = {c["id"] for c in root["children"]}
        assert child_ids == {child1.id, child2.id}

    def test_tree_flat_when_no_hierarchy(self):
        p = _make_project("test-tree-2")
        obj1 = _make_obj(p.id, "Obj 1")
        obj2 = _make_obj(p.id, "Obj 2")

        tree = get_objective_tree(p.id)
        tree_ids = {n["id"] for n in tree}
        assert obj1.id in tree_ids
        assert obj2.id in tree_ids
        for node in tree:
            assert node["children"] == []

    def test_tree_includes_complete_objectives(self):
        p = _make_project("test-tree-3")
        obj = _make_obj(p.id, "Will be completed")
        complete_objective(obj.id)

        tree = get_objective_tree(p.id)
        assert any(n["id"] == obj.id for n in tree)


# ---------------------------------------------------------------------------
# 4. objective_to_dict includes new fields
# ---------------------------------------------------------------------------

class TestObjectiveToDict:
    def test_dict_includes_parent_id_and_created_by(self):
        p = _make_project("test-dict-1")
        parent = _make_obj(p.id, "Parent")
        child = _make_obj(p.id, "Child", parent_id=parent.id, created_by="maestro")

        d = objective_to_dict(child)
        assert d["parent_id"] == parent.id
        assert d["created_by"] == "maestro"

    def test_dict_defaults(self):
        p = _make_project("test-dict-2")
        obj = _make_obj(p.id, "Default obj")
        d = objective_to_dict(obj)
        assert d["parent_id"] is None
        assert d["created_by"] == "human"


# ---------------------------------------------------------------------------
# 5. Prompt injection (loop._build_messages)
# ---------------------------------------------------------------------------

class TestPromptInjection:
    """Verify that _build_messages injects objective context when the task has one."""

    def _run_build_messages(self, task_id: int, project_path: str) -> list:
        from app.agent.loop import MaestroLoop
        loop = MaestroLoop.__new__(MaestroLoop)
        loop.task_id = task_id
        loop.max_turns = 10
        loop.llm_base_url = "http://localhost:8008/v1"
        loop.llm_model = "test-model"
        loop.max_context = 8192
        loop.llm_id = 1
        loop.budget_id = 1
        loop.project_path = project_path
        loop._system_prompt_override = None
        loop._tool_schemas = []
        loop._required_input_keys = []
        loop._messages = []
        loop._turn = 0
        loop._consecutive_errors = 0
        loop._stop_requested = False
        loop._git_branch = None
        loop._files_changed = set()
        loop._last_prompt_tokens = 0
        loop._warnings_fired = set()
        loop._turn_warnings_fired = set()
        return loop._build_messages()

    def test_injection_present_when_objective_set(self):
        p = _make_project("test-inject-1")
        obj = _make_obj(p.id, "Investigate twin prime gaps")
        task = create_task(
            title="Research task",
            description="Some work",
            project_id=p.id,
            task_type="idea",
            stage_key="idea",
            autopilot_objective_id=obj.id,
        )

        messages = self._run_build_messages(str(task.id), "")
        full_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in messages
        )
        assert "AUTOPILOT OBJECTIVE" in full_text
        assert "Investigate twin prime gaps" in full_text

    def test_no_injection_when_no_objective(self):
        p = _make_project("test-inject-2")
        task = create_task(
            title="Human task",
            description="No objective",
            project_id=p.id,
            task_type="idea",
            stage_key="idea",
        )

        messages = self._run_build_messages(str(task.id), "")
        full_text = " ".join(
            m.get("content", "") if isinstance(m.get("content"), str) else ""
            for m in messages
        )
        assert "AUTOPILOT OBJECTIVE" not in full_text
