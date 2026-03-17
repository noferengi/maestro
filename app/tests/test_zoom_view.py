"""
Tests for the sub-board zoom view: descendants API and filtering logic.
"""

import pytest
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestDescendantsAPI:
    """Test /api/tasks/{id}/descendants returns correct tree structure."""

    def test_descendants_endpoint(self):
        from database import (
            SessionLocal, Task, get_descendant_tree,
        )
        db = SessionLocal()
        try:
            parent = Task(id="test-zv-parent", title="ZV Parent", type="idea", position=0, project="TestZV", is_big_idea=True)
            child1 = Task(id="test-zv-c1", title="ZV Child 1", type="planning", position=0, project="TestZV", parent_task_id="test-zv-parent")
            child2 = Task(id="test-zv-c2", title="ZV Child 2", type="development", position=1, project="TestZV", parent_task_id="test-zv-parent")
            gc1 = Task(id="test-zv-gc1", title="ZV Grandchild 1", type="idea", position=0, project="TestZV", parent_task_id="test-zv-c1")
            db.add_all([parent, child1, child2, gc1])
            db.commit()
        finally:
            db.close()

        tree = get_descendant_tree("test-zv-parent")

        # Should have 3 descendants
        assert len(tree) == 3
        ids = {n["id"] for n in tree}
        assert ids == {"test-zv-c1", "test-zv-c2", "test-zv-gc1"}

        # Verify types
        type_map = {n["id"]: n["type"] for n in tree}
        assert type_map["test-zv-c1"] == "planning"
        assert type_map["test-zv-c2"] == "development"
        assert type_map["test-zv-gc1"] == "idea"

        # Cleanup
        db = SessionLocal()
        try:
            db.query(Task).filter(Task.id.in_([
                "test-zv-parent", "test-zv-c1", "test-zv-c2", "test-zv-gc1"
            ])).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


class TestDescendantFiltering:
    """Test the descendant filtering logic (JS equivalent server-side test)."""

    def test_filter_to_descendants_only(self):
        """Simulate the JS filter: only show Big Idea + its descendants."""
        from database import SessionLocal, Task, get_descendant_tree

        db = SessionLocal()
        try:
            # Create a small tree plus an unrelated task
            parent = Task(id="test-filt-p", title="Filter Parent", type="idea", position=0, project="TestFilt", is_big_idea=True)
            child = Task(id="test-filt-c1", title="Filter Child", type="planning", position=0, project="TestFilt", parent_task_id="test-filt-p")
            unrelated = Task(id="test-filt-other", title="Unrelated", type="planning", position=1, project="TestFilt")
            db.add_all([parent, child, unrelated])
            db.commit()
        finally:
            db.close()

        # Get descendants of the parent
        descendants = get_descendant_tree("test-filt-p")
        descendant_ids = {d["id"] for d in descendants}

        # Simulate the filter
        all_task_ids = {"test-filt-p", "test-filt-c1", "test-filt-other"}
        filtered = {tid for tid in all_task_ids if tid == "test-filt-p" or tid in descendant_ids}

        assert "test-filt-p" in filtered
        assert "test-filt-c1" in filtered
        assert "test-filt-other" not in filtered

        # Cleanup
        db = SessionLocal()
        try:
            db.query(Task).filter(Task.id.in_([
                "test-filt-p", "test-filt-c1", "test-filt-other"
            ])).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


class TestEmptyDescendants:
    """Edge case: task with no children."""

    def test_empty_tree(self):
        from database import SessionLocal, Task, get_descendant_tree

        db = SessionLocal()
        try:
            task = Task(id="test-empty-desc", title="No Children", type="idea", position=0, project="TestEmpty")
            db.add(task)
            db.commit()
        finally:
            db.close()

        tree = get_descendant_tree("test-empty-desc")
        assert tree == []

        # Cleanup
        db = SessionLocal()
        try:
            db.query(Task).filter(Task.id == "test-empty-desc").delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()
