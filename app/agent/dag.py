"""
app/agent/dag.py
----------------
DAG Resolver for the Maestro Orchestrator.

Consumes a flat list of task dicts (as returned by the Kanban DB / API)
and provides:
  • get_ready_tasks()       - tasks whose prerequisites are all done
  • get_next_task()         - single highest-priority ready task
  • build_execution_order() - topological sort producing parallelizable batches
  • validate_dag()          - cycle detection + missing-prereq checks

Tasks are expected to have at minimum:
  {
    "id": str,
    "type": str,              # Kanban column (planning/development/review/completed/architecture)
    "position": int,          # ordering hint within a column
    "prerequisites": list[str],  # list of prerequisite task IDs
    ...
  }

The "done" states recognised as satisfying a prerequisite are:
  completed, accepted  (case-insensitive)
"""

from __future__ import annotations

import logging
from collections import defaultdict, deque
from typing import Any

from app.agent.config import PIPELINE_COLUMN_ORDER, PIPELINE_DONE_STATUSES

logger = logging.getLogger(__name__)

# Status / column names that count as "this task is done"
_DONE_STATUSES: frozenset[str] = PIPELINE_DONE_STATUSES

# Canonical type-order for priority tie-breaking (lower index = higher priority)
_TYPE_ORDER: list[str] = PIPELINE_COLUMN_ORDER


def _is_done(task: dict) -> bool:
    """Return True if a task is in a terminal completed state."""
    return (task.get("type") or "").lower() in _DONE_STATUSES


class DAGResolver:
    """
    Resolves scheduling order for a set of Kanban tasks connected by
    prerequisite relationships.

    Args:
        tasks: List of task dicts as returned by the Kanban API / database.
    """

    def __init__(self, tasks: list[dict]) -> None:
        self._tasks: list[dict] = tasks
        self._by_id: dict[str, dict] = {t["id"]: t for t in tasks if "id" in t}
        # parent_task_id → [child task IDs]: built from subdivision hierarchy.
        # Used by _is_effectively_done to delegate completion to children.
        self._children_by_parent: dict[str, list[str]] = defaultdict(list)
        for t in tasks:
            pid = t.get("parent_task_id")
            if pid:
                self._children_by_parent[pid].append(t["id"])

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_ready_tasks(self) -> list[dict]:
        """
        Return all tasks that are ready to execute: their status is not
        already done/active, AND all of their prerequisites are in a done state.
        """
        ready: list[dict] = []
        for task in self._tasks:
            if _is_done(task):
                continue
            task_type = (task.get("type") or "").lower()
            # Permanently terminal states - never dispatch
            if task_type in ("completed", "cancelled", "subdividing"):
                continue
            # indev / conceptual_review / optimization / final_review are
            # mid-pipeline stages.  They are NOT excluded here: if there is no
            # live thread for them (fresh startup, crash recovery) the
            # _active_sessions guard in scheduler._tick() re-dispatches them.
            # Excluding them here was the cause of orphaned-after-restart tasks
            # never being recovered.
            # Skip Big Idea parents that have children - the scheduler works on
            # the children directly.  The parent unblocks downstream dependents
            # via _is_effectively_done once all active children complete.
            if task.get("id") in self._children_by_parent:
                continue
            if self._all_prerequisites_done(task):
                ready.append(task)
        return ready

    def get_next_task(self) -> dict | None:
        """
        Return the single highest-priority ready task.

        Priority order:
          1. Lowest position value within its column.
          2. Column order: architecture → planning → development → review.
          3. Alphabetical ID as final tiebreaker.
        """
        ready = self.get_ready_tasks()
        if not ready:
            return None
        return min(ready, key=self._priority_key)

    def build_execution_order(self) -> list[list[dict]]:
        """
        Topological sort that returns batches (waves) of tasks that can be
        executed in parallel within each wave.

        Uses Kahn's algorithm on the prerequisite graph.
        Returns a list of batches where each batch is a list of task dicts
        that have no inter-dependencies within the batch.

        Returns an empty list if the graph has a cycle (call validate_dag()
        to surface the error message).
        """
        # Build in-degree map (only count prerequisite edges that exist in our set)
        in_degree: dict[str, int] = {t["id"]: 0 for t in self._tasks if "id" in t}
        dependents: dict[str, list[str]] = defaultdict(list)  # prereq_id → [task_ids]

        for task in self._tasks:
            tid = task.get("id")
            if not tid:
                continue
            for prereq_id in task.get("prerequisites") or []:
                if prereq_id in self._by_id:
                    in_degree[tid] = in_degree.get(tid, 0) + 1
                    dependents[prereq_id].append(tid)
                # Prerequisites not in this set are ignored (assumed done externally)

        queue: deque[str] = deque(
            tid for tid, deg in in_degree.items() if deg == 0
        )
        batches: list[list[dict]] = []

        while queue:
            batch_ids = list(queue)
            queue.clear()
            batch = [self._by_id[tid] for tid in batch_ids if tid in self._by_id]
            batch.sort(key=self._priority_key)
            batches.append(batch)

            for tid in batch_ids:
                for dependent_id in dependents.get(tid, []):
                    in_degree[dependent_id] -= 1
                    if in_degree[dependent_id] == 0:
                        queue.append(dependent_id)

        # If any node still has in_degree > 0, there's a cycle
        if any(deg > 0 for deg in in_degree.values()):
            logger.error(
                "DAG has cycle(s) - topological sort aborted for task set: %s",
                [t["id"] for t in self._tasks if "id" in t],
            )
            return []  # Caller should check validate_dag() for details

        return batches

    def validate_dag(self) -> list[str]:
        """
        Validate the DAG structure and return a list of human-readable errors.

        Checks:
          • Missing prerequisites (prereq ID not in task set).
          • Cycles in the prerequisite graph.
          • Duplicate task IDs.
        """
        errors: list[str] = []

        # Duplicate IDs
        seen_ids: set[str] = set()
        for task in self._tasks:
            tid = task.get("id")
            if not tid:
                errors.append(f"Task is missing an 'id' field: {task}")
                continue
            if tid in seen_ids:
                errors.append(f"Duplicate task ID: '{tid}'")
            seen_ids.add(tid)

        # Missing prerequisites
        for task in self._tasks:
            tid = task.get("id", "?")
            for prereq_id in task.get("prerequisites") or []:
                if prereq_id not in self._by_id:
                    errors.append(
                        f"Task '{tid}' has unknown prerequisite '{prereq_id}'."
                    )

        # Cycle detection (DFS)
        WHITE, GRAY, BLACK = 0, 1, 2
        color: dict[str, int] = {t["id"]: WHITE for t in self._tasks if "id" in t}
        cycle_found: list[str] = []

        def dfs(node_id: str, path: list[str]) -> bool:
            """Return True if a cycle is found."""
            color[node_id] = GRAY
            path.append(node_id)
            task = self._by_id.get(node_id)
            if task:
                for prereq_id in task.get("prerequisites") or []:
                    if prereq_id not in color:
                        continue
                    if color[prereq_id] == GRAY:
                        cycle_path = path[path.index(prereq_id) :] + [prereq_id]
                        cycle_found.append(" → ".join(cycle_path))
                        return True
                    if color[prereq_id] == WHITE:
                        if dfs(prereq_id, path):
                            return True
            path.pop()
            color[node_id] = BLACK
            return False

        for task in self._tasks:
            tid = task.get("id")
            if tid and color.get(tid) == WHITE:
                if dfs(tid, []):
                    errors.append(f"Cycle detected: {cycle_found[-1]}")

        for e in errors:
            logger.warning("DAG validation error: %s", e)
        return errors

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _is_effectively_done(self, task_id: str, _visited: frozenset | None = None) -> bool:
        """True if a task is completed/accepted, OR if it is a Big Idea parent
        whose every active (non-cancelled) child is recursively effectively done.

        This allows a subdivided parent to satisfy a prerequisite edge once all
        its children finish, without the parent itself needing to reach
        'completed' state.  Active children that are themselves Big Idea parents
        are resolved the same way recursively.

        Conservative rules:
          - Unknown task ID → False.
          - All children cancelled (no active children) → False (something went
            wrong; don't silently unblock downstream work).
          - Cycle guard via _visited frozenset.
        """
        if _visited is None:
            _visited = frozenset()
        if task_id in _visited:
            return False  # cycle guard
        task = self._by_id.get(task_id)
        if task is None:
            return False  # unknown → conservative
        if _is_done(task):
            return True
        children = self._children_by_parent.get(task_id)
        if not children:
            return False  # not done and no children to delegate to
        active_children = [
            cid for cid in children
            if (self._by_id.get(cid, {}).get("type") or "").lower() != "cancelled"
        ]
        if not active_children:
            return False  # all children cancelled - keep parent blocked
        return all(
            self._is_effectively_done(cid, _visited | {task_id})
            for cid in active_children
        )

    def _all_prerequisites_done(self, task: dict) -> bool:
        """Return True if all prerequisites for task are in a done state."""
        for prereq_id in task.get("prerequisites") or []:
            if not self._is_effectively_done(prereq_id):
                return False
        return True

    def _priority_key(self, task: dict) -> tuple[int, int, str]:
        """
        Sorting key: (type_order_index, position, id).
        Lower = higher priority.
        """
        task_type = (task.get("type") or "").lower()
        try:
            type_idx = _TYPE_ORDER.index(task_type)
        except ValueError:
            type_idx = len(_TYPE_ORDER)
        position = task.get("position") or 0
        return (type_idx, position, task.get("id") or "")
