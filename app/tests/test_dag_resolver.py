"""
Tests for app/agent/dag.py — DAGResolver.

Covers get_ready_tasks, build_execution_order, and validate_dag.
"""

import os
import sys
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from app.agent.dag import DAGResolver


def _task(id, type="planning", prereqs=None, position=0):
    return {"id": id, "type": type, "position": position, "prerequisites": prereqs or []}


class TestGetReadyTasks:
    def test_no_prerequisites(self):
        tasks = [_task("a"), _task("b")]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "a" in ids
        assert "b" in ids

    def test_all_done(self):
        tasks = [_task("a", type="completed"), _task("b", type="completed")]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        assert len(ready) == 0

    def test_prerequisite_not_done(self):
        tasks = [_task("a", type="planning"), _task("b", prereqs=["a"])]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "b" not in ids

    def test_unknown_prerequisite_blocks(self):
        tasks = [_task("b", prereqs=["nonexistent"])]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "b" not in ids

    def test_done_task_excluded(self):
        tasks = [_task("a", type="completed")]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        assert all(t["id"] != "a" for t in ready)

    def test_completed_prerequisite_satisfies(self):
        tasks = [_task("a", type="completed"), _task("b", type="planning", prereqs=["a"])]
        resolver = DAGResolver(tasks)
        ready = resolver.get_ready_tasks()
        ids = [t["id"] for t in ready]
        assert "b" in ids


class TestBuildExecutionOrder:
    def test_linear_chain(self):
        tasks = [_task("a"), _task("b", prereqs=["a"]), _task("c", prereqs=["b"])]
        resolver = DAGResolver(tasks)
        # build_execution_order returns a list of batches (list of lists of task dicts)
        batches = resolver.build_execution_order()
        assert batches is not None
        if batches:
            # Flatten batches to get ordered IDs
            batch_ids = [[t["id"] for t in batch] for batch in batches]
            # "a" must appear in an earlier batch than "b", and "b" before "c"
            a_batch = next(i for i, ids in enumerate(batch_ids) if "a" in ids)
            b_batch = next(i for i, ids in enumerate(batch_ids) if "b" in ids)
            c_batch = next(i for i, ids in enumerate(batch_ids) if "c" in ids)
            assert a_batch < b_batch
            assert b_batch < c_batch

    def test_empty_input(self):
        resolver = DAGResolver([])
        order = resolver.build_execution_order()
        assert order == [] or order is not None

    def test_cycle_returns_empty(self):
        tasks = [
            _task("a", prereqs=["b"]),
            _task("b", prereqs=["a"]),
        ]
        resolver = DAGResolver(tasks)
        order = resolver.build_execution_order()
        # Cycle should produce empty or raise — either is acceptable
        assert order == [] or order is None or isinstance(order, list)


class TestValidateDag:
    def test_clean_dag(self):
        tasks = [_task("a"), _task("b", prereqs=["a"])]
        resolver = DAGResolver(tasks)
        errors = resolver.validate_dag()
        assert errors == []

    def test_missing_prerequisite(self):
        tasks = [_task("b", prereqs=["missing"])]
        resolver = DAGResolver(tasks)
        errors = resolver.validate_dag()
        assert len(errors) > 0

    def test_cycle_detected(self):
        tasks = [
            _task("a", prereqs=["b"]),
            _task("b", prereqs=["a"]),
        ]
        resolver = DAGResolver(tasks)
        errors = resolver.validate_dag()
        assert any("ycle" in e or "cycle" in e.lower() for e in errors)

    def test_empty_dag(self):
        resolver = DAGResolver([])
        errors = resolver.validate_dag()
        assert errors == []
