"""Task DAG management and REPL control loop for The Maestro Orchestrator."""

import json
from enum import StrEnum
from pathlib import Path
from typing import Any



class TaskState(StrEnum):
    """Possible states for a task in the DAG."""

    PENDING = "PENDING"
    ACTIVE = "ACTIVE"
    VERIFYING = "VERIFYING"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    REVERTED = "REVERTED"  # Task moved back to design phase


class TaskNode:
    """Represents a single task node in the DAG."""

    def __init__(
        self,
        task_id: str,
        description: str,
        prerequisites: list[str] | None = None,
        state: TaskState = TaskState.PENDING,
        retries: int = 0,
        agent_type: str = "coding",
    ):
        """
        Initialize a task node.

        Args:
            task_id: Unique identifier for the task.
            description: Human-readable description of the task.
            prerequisites: List of task IDs that must be completed before this task.
            state: Current state of the task.
            retries: Number of failed attempts for this task.
            agent_type: Type of agent assigned to this task (e.g., 'coding', 'debugging').
        """
        self.task_id = task_id
        self.description = description
        self.prerequisites = prerequisites or []
        self.state = state
        self.retries = retries
        self.agent_type = agent_type

    def to_dict(self) -> dict[str, Any]:
        """Convert task node to dictionary for serialization."""
        return {
            "task_id": self.task_id,
            "description": self.description,
            "prerequisites": self.prerequisites,
            "state": self.state.value,
            "retries": self.retries,
            "agent_type": self.agent_type,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskNode":
        """Create a task node from a dictionary."""
        return cls(
            task_id=data["task_id"],
            description=data["description"],
            prerequisites=data.get("prerequisites", []),
            state=TaskState(data["state"]),
            retries=data.get("retries", 0),
            agent_type=data.get("agent_type", "coding"),
        )


class TaskDAG:
    """Manages the Task DAG and provides resolution logic."""

    def __init__(self, tasks: list[TaskNode] | None = None):
        """
        Initialize the Task DAG.

        Args:
            tasks: List of task nodes in the DAG.
        """
        self.tasks = tasks or []
        self._task_map = {task.task_id: task for task in self.tasks}

    def add_task(self, task: TaskNode) -> None:
        """Add a task to the DAG."""
        self.tasks.append(task)
        self._task_map[task.task_id] = task

    def get_task(self, task_id: str) -> TaskNode | None:
        """Get a task by ID."""
        return self._task_map.get(task_id)

    def get_all_tasks(self) -> list[TaskNode]:
        """Get all tasks in the DAG."""
        return self.tasks

    def is_task_ready(self, task: TaskNode) -> bool:
        """
        Check if a task is ready to be executed.

        A task is ready if:
        - Its state is PENDING
        - All its prerequisites are ACCEPTED
        """
        if task.state != TaskState.PENDING:
            return False

        for prereq_id in task.prerequisites:
            prereq = self._task_map.get(prereq_id)
            if prereq is None or prereq.state != TaskState.ACCEPTED:
                return False

        return True

    def get_ready_tasks(self) -> list[TaskNode]:
        """Get all tasks that are ready to be executed."""
        return [task for task in self.tasks if self.is_task_ready(task)]

    def get_active_tasks(self) -> list[TaskNode]:
        """Get all tasks currently in ACTIVE state."""
        return [task for task in self.tasks if task.state == TaskState.ACTIVE]

    def get_accepted_tasks(self) -> list[TaskNode]:
        """Get all tasks that have been ACCEPTED."""
        return [task for task in self.tasks if task.state == TaskState.ACCEPTED]

    def is_complete(self) -> bool:
        """Check if all tasks in the DAG are ACCEPTED."""
        return all(task.state == TaskState.ACCEPTED for task in self.tasks)

    def to_dict(self) -> dict[str, Any]:
        """Convert DAG to dictionary for serialization."""
        return {
            "tasks": [task.to_dict() for task in self.tasks],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskDAG":
        """Create a DAG from a dictionary."""
        tasks = [TaskNode.from_dict(task_data) for task_data in data.get("tasks", [])]
        return cls(tasks=tasks)

    def save(self, filepath: str | Path) -> None:
        """Save the DAG to a JSON file."""
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2)

    @classmethod
    def load(cls, filepath: str | Path) -> "TaskDAG":
        """Load a DAG from a JSON file."""
        path = Path(filepath)
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return cls.from_dict(data)

    def transition_state(self, task_id: str, new_state: TaskState) -> bool:
        """
        Transition a task to a new state.

        Returns True if the transition was successful, False otherwise.
        """
        task = self._task_map.get(task_id)
        if task is None:
            return False

        # Validate state transitions
        valid_transitions = {
            TaskState.PENDING: [TaskState.ACTIVE],
            TaskState.ACTIVE: [TaskState.VERIFYING, TaskState.REJECTED],
            TaskState.VERIFYING: [TaskState.ACCEPTED, TaskState.REJECTED],
            TaskState.REJECTED: [TaskState.PENDING],  # Can retry
            TaskState.ACCEPTED: [],  # Terminal state
            TaskState.REVERTED: [TaskState.PENDING],
        }

        if new_state not in valid_transitions.get(task.state, []):
            return False

        task.state = new_state
        if new_state == TaskState.ACTIVE:
            task.retries += 1

        return True

    def force_accept(self, task_id: str) -> bool:
        """
        Force accept a task (bypassing ACTIVE/VERIFYING states).
        Useful for testing or manual overrides.

        Returns True if the transition was successful.
        """
        task = self._task_map.get(task_id)
        if task is None:
            return False

        # Allow transition from any state to ACCEPTED
        task.state = TaskState.ACCEPTED
        return True

    def mark_as_reverted(self, task_id: str) -> bool:
        """Mark a task as REVERTED (back to design phase)."""
        task = self._task_map.get(task_id)
        if task is None:
            return False

        task.state = TaskState.REVERTED
        return True
