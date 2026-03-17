"""
Tests for batch reorder API, descendant tree, and Big Idea flagging.
"""

import pytest
import os
import sys
import json
from datetime import datetime

# Ensure app directory is on path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestBatchReorderTasks:
    """batch_reorder_tasks processes multiple moves atomically."""

    def test_batch_reorder_basic(self):
        from database import (
            SessionLocal, Task, batch_reorder_tasks, Base, engine,
        )
        # Use a fresh in-memory approach: just test the function logic
        db = SessionLocal()
        try:
            # Create test tasks
            t1 = Task(id="test-br-1", title="T1", type="planning", position=0, project="TestBR")
            t2 = Task(id="test-br-2", title="T2", type="planning", position=1, project="TestBR")
            t3 = Task(id="test-br-3", title="T3", type="development", position=0, project="TestBR")
            db.add_all([t1, t2, t3])
            db.commit()
        finally:
            db.close()

        # Batch reorder
        moves = [
            {"task_id": "test-br-1", "position": 2, "type": "planning"},
            {"task_id": "test-br-2", "position": 0, "type": "planning"},
            {"task_id": "test-br-3", "position": 5, "type": "development"},
        ]
        result = batch_reorder_tasks(moves)
        assert result is True

        # Verify
        db = SessionLocal()
        try:
            t1 = db.query(Task).filter(Task.id == "test-br-1").first()
            t2 = db.query(Task).filter(Task.id == "test-br-2").first()
            t3 = db.query(Task).filter(Task.id == "test-br-3").first()
            assert t1.position == 2
            assert t2.position == 0
            assert t3.position == 5
        finally:
            # Cleanup
            db.query(Task).filter(Task.id.in_(["test-br-1", "test-br-2", "test-br-3"])).delete(synchronize_session=False)
            db.commit()
            db.close()


class TestGetDescendantTree:
    """get_descendant_tree returns correct tree structure."""

    def test_descendant_tree(self):
        from database import (
            SessionLocal, Task, get_descendant_tree,
        )
        db = SessionLocal()
        try:
            parent = Task(id="test-dt-parent", title="Parent", type="idea", position=0, project="TestDT")
            child1 = Task(id="test-dt-child1", title="Child 1", type="planning", position=0, project="TestDT", parent_task_id="test-dt-parent")
            child2 = Task(id="test-dt-child2", title="Child 2", type="development", position=1, project="TestDT", parent_task_id="test-dt-parent")
            grandchild = Task(id="test-dt-gc1", title="Grandchild", type="idea", position=0, project="TestDT", parent_task_id="test-dt-child1")
            db.add_all([parent, child1, child2, grandchild])
            db.commit()
        finally:
            db.close()

        tree = get_descendant_tree("test-dt-parent")
        ids = [n["id"] for n in tree]
        assert "test-dt-child1" in ids
        assert "test-dt-child2" in ids
        assert "test-dt-gc1" in ids
        assert len(tree) == 3

        # Check depth
        depths = {n["id"]: n["depth"] for n in tree}
        assert depths["test-dt-child1"] == 1
        assert depths["test-dt-child2"] == 1
        assert depths["test-dt-gc1"] == 2

        # Cleanup
        db = SessionLocal()
        try:
            db.query(Task).filter(Task.id.in_([
                "test-dt-parent", "test-dt-child1", "test-dt-child2", "test-dt-gc1"
            ])).delete(synchronize_session=False)
            db.commit()
        finally:
            db.close()


class TestSetBigIdeaFlag:
    """Big Idea flag set correctly during subdivision."""

    def test_set_flag(self):
        from database import (
            SessionLocal, Task, set_big_idea_flag,
        )
        db = SessionLocal()
        try:
            task = Task(id="test-bi-1", title="Big Idea Test", type="idea", position=0, project="TestBI")
            db.add(task)
            db.commit()
        finally:
            db.close()

        result = set_big_idea_flag("test-bi-1")
        assert result is True

        db = SessionLocal()
        try:
            task = db.query(Task).filter(Task.id == "test-bi-1").first()
            assert task.is_big_idea is True
        finally:
            db.query(Task).filter(Task.id == "test-bi-1").delete(synchronize_session=False)
            db.commit()
            db.close()

    def test_set_flag_nonexistent(self):
        from database import set_big_idea_flag
        result = set_big_idea_flag("nonexistent-task-xyz")
        assert result is False
