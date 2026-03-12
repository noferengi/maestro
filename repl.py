"""Maestro REPL: Primary control loop that navigates the task DAG."""

import subprocess
from pathlib import Path
from typing import Any

import config
import dags


class CheckpointManager:
    """Git-based persistence layer for task checkpointing."""

    def __init__(self, project_root: str | Path = "."):
        """
        Initialize the CheckpointManager.

        Args:
            project_root: Path to the project root directory.
        """
        self.project_root = Path(project_root)

    def _run_git_command(self, *args: str) -> tuple[bool, str, str]:
        """
        Run a git command and return success status with output.

        Returns:
            Tuple of (success, stdout, stderr).
        """
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.project_root,
                capture_output=True,
                text=True,
                check=False,
            )
            return (
                result.returncode == 0,
                result.stdout.strip(),
                result.stderr.strip(),
            )
        except FileNotFoundError:
            return False, "", "Git not found in PATH"
        except Exception as e:
            return False, "", str(e)

    def add_files(self, files: list[str] | None = None) -> tuple[bool, str]:
        """
        Run git add for specified files or all changes.

        Args:
            files: List of files to add, or None to add all changes.

        Returns:
            Tuple of (success, message).
        """
        if files:
            for file in files:
                success, stdout, stderr = self._run_git_command("add", file)
                if not success:
                    return False, f"Failed to add {file}: {stderr}"
            return True, f"Added {len(files)} file(s)"
        else:
            success, stdout, stderr = self._run_git_command("add", ".")
            if success:
                return True, "Added all changes"
            return False, f"git add failed: {stderr}"

    def commit(self, message: str) -> tuple[bool, str]:
        """
        Run git commit with the specified message.

        Args:
            message: Commit message.

        Returns:
            Tuple of (success, message).
        """
        # Escape double quotes in message
        safe_message = message.replace('"', '\\"')
        success, stdout, stderr = self._run_git_command(
            "commit", "-m", f'"{safe_message}"'
        )
        if success:
            return True, "Commit successful"
        return False, f"git commit failed: {stderr}"

    def push(self) -> tuple[bool, str]:
        """
        Run git push to remote repository.

        Returns:
            Tuple of (success, message).
        """
        success, stdout, stderr = self._run_git_command("push")
        if success:
            return True, "Push successful"
        return False, f"git push failed: {stderr}"

    def check_status(self) -> dict[str, Any]:
        """
        Check git status.

        Returns:
            Dictionary with git status information.
        """
        success, stdout, stderr = self._run_git_command("status", "--porcelain")
        if success:
            files = [line.strip() for line in stdout.split("\n") if line.strip()]
            return {
                "dirty": len(files) > 0,
                "untracked_files": [f for f in files if f.startswith("??")],
                "modified_files": [f for f in files if not f.startswith("??")],
            }
        return {"error": stderr}

    def checkpoint(self, message: str, files: list[str] | None = None) -> tuple[bool, str]:
        """
        Perform a full checkpoint: add, commit, and optionally push.

        Args:
            message: Commit message.
            files: Files to include in checkpoint.

        Returns:
            Tuple of (success, message).
        """
        # Add files
        success, msg = self.add_files(files)
        if not success:
            return False, msg

        # Commit
        success, msg = self.commit(message)
        if not success:
            return False, msg

        return True, "Checkpoint successful"


class MaestroREPL:
    """The primary Maestro control loop (The Wiggum Loop)."""

    def __init__(
        self,
        dag: dags.TaskDAG,
        checkpoint_manager: CheckpointManager | None = None,
        project_root: str | Path = ".",
    ):
        """
        Initialize the Maestro REPL.

        Args:
            dag: The TaskDAG to manage.
            checkpoint_manager: Optional CheckpointManager for git persistence.
            project_root: Path to the project root directory.
        """
        self.dag = dag
        self.checkpoint_manager = checkpoint_manager or CheckpointManager(project_root)
        self.project_root = Path(project_root)
        self.running = False
        self.current_task: dags.TaskNode | None = None

    def _load_dag(self, filepath: str | Path) -> None:
        """Load DAG from file if not already loaded."""
        path = Path(filepath)
        if path.exists():
            self.dag = dags.TaskDAG.load(filepath)

    def _save_dag(self, filepath: str | Path) -> None:
        """Save DAG to file."""
        self.dag.save(filepath)

    def _select_next_task(self) -> dags.TaskNode | None:
        """
        Select the next ready task from the DAG.

        Returns:
            The next ready task, or None if no tasks are ready.
        """
        ready_tasks = self.dag.get_ready_tasks()

        if not ready_tasks:
            # Check for tasks in REJECTED state that can be retried
            rejected_tasks = [
                t for t in self.dag.get_all_tasks()
                if t.state == dags.TaskState.REJECTED
                and t.retries < config.ProjectConstants.MAX_FAILURE_RETRIES
            ]
            if rejected_tasks:
                # Pick the first rejected task for retry
                return rejected_tasks[0]

        return ready_tasks[0] if ready_tasks else None

    def _transition_task(
        self, task_id: str, new_state: dags.TaskState, message: str = ""
    ) -> bool:
        """
        Transition a task to a new state and checkpoint if accepted.

        Args:
            task_id: ID of the task to transition.
            new_state: Target state.
            message: Optional message for checkpoint.

        Returns:
            True if transition was successful.
        """
        task = self.dag.get_task(task_id)
        if task is None:
            return False

        success = self.dag.transition_state(task_id, new_state)
        if not success:
            return False

        # Save DAG state
        dag_path = self.project_root / ".maestro" / "task_dag.json"
        self._save_dag(dag_path)

        # Checkpoint on ACCEPTED state
        if new_state == dags.TaskState.ACCEPTED and message:
            checkpoint_msg = f"[Maestro] Task '{task_id}' accepted: {message}"
            self.checkpoint_manager.checkpoint(checkpoint_msg)

        return True

    def _mark_failed(self, task_id: str, reason: str) -> bool:
        """
        Mark a task as rejected and handle retries or revert.

        Args:
            task_id: ID of the failed task.
            reason: Reason for failure.

        Returns:
            True if task was marked as rejected.
        """
        task = self.dag.get_task(task_id)
        if task is None:
            return False

        # Check if we should retry or revert
        if task.retries < config.ProjectConstants.MAX_FAILURE_RETRIES:
            # Retry: transition back to PENDING
            return self._transition_task(task_id, dags.TaskState.PENDING, reason)
        else:
            # Max retries exceeded: revert to design phase
            self.dag.mark_as_reverted(task_id)
            self._save_dag(self.project_root / ".maestro" / "task_dag.json")

            checkpoint_msg = (
                f"[Maestro] Task '{task_id}' reverted after {task.retries} failures: {reason}"
            )
            self.checkpoint_manager.checkpoint(checkpoint_msg)

            return True

    def run(self, dag_path: str | Path = ".maestro/task_dag.json") -> None:
        """
        Execute the main REPL loop (The Wiggum Loop).

        The loop continues until all tasks are ACCEPTED or the system
        determines no further progress can be made.

        Args:
            dag_path: Path to the DAG JSON file.
        """
        self._load_dag(dag_path)
        self.running = True

        print(f"[Maestro] Starting REPL loop for {config.get_app_id()}")
        print(f"[Maestro] Project root: {self.project_root}")

        while self.running:
            # Check if all tasks are complete
            if self.dag.is_complete():
                print("[Maestro] All tasks accepted. REPL loop complete.")
                break

            # Get next ready task
            next_task = self._select_next_task()

            if next_task is None:
                # No tasks ready, check for unrecoverable state
                all_tasks = self.dag.get_all_tasks()
                terminal_states = [
                    t for t in all_tasks
                    if t.state in (dags.TaskState.ACCEPTED, dags.TaskState.REVERTED)
                ]

                if len(terminal_states) == len(all_tasks):
                    print("[Maestro] All tasks reached terminal state.")
                    print(f"[Maestro] Accepted: {len(self.dag.get_accepted_tasks())}")
                    print(f"[Maestro] Reverted: {len([t for t in all_tasks if t.state == dags.TaskState.REVERTED])}")
                else:
                    print("[Maestro] No tasks ready and some tasks in intermediate states.")
                    print(f"[Maestro] Active tasks: {len(self.dag.get_active_tasks())}")

                break

            # Start task
            self.current_task = next_task
            print(f"[Maestro] Starting task '{next_task.task_id}': {next_task.description}")
            print(f"[Maestro] Agent: {next_task.agent_type}, Prerequisites: {next_task.prerequisites or []}")

            if not self._transition_task(next_task.task_id, dags.TaskState.ACTIVE, "Task started"):
                print(f"[Maestro] Failed to start task '{next_task.task_id}'")
                break

            # In a real implementation, the agent would be invoked here
            # For now, we simulate task execution
            # TODO: Invoke appropriate agent (coding, debugging, etc.)

            # Simulate successful task completion for testing
            # TODO: Replace with actual agent invocation and verification
            print(f"[Maestro] Task '{next_task.task_id}' completed (simulated)")
            self._transition_task(next_task.task_id, dags.TaskState.ACCEPTED, "Task completed successfully")

        self.running = False

    def stop(self) -> None:
        """Stop the REPL loop."""
        self.running = False
        print("[Maestro] REPL loop stopped.")


def create_sample_dag() -> dags.TaskDAG:
    """
    Create a sample DAG for testing purposes.

    Returns:
        A TaskDAG with sample tasks.
    """
    dag = dags.TaskDAG()

    # Task hierarchy:
    # task-1 (no prerequisites)
    # task-2 (depends on task-1)
    # task-3 (depends on task-1)
    # task-4 (depends on task-2 and task-3)

    dag.add_task(dags.TaskNode(
        task_id="task-1",
        description="Initialize project structure and configuration",
        prerequisites=[],
        agent_type="coding",
    ))

    dag.add_task(dags.TaskNode(
        task_id="task-2",
        description="Implement core module A",
        prerequisites=["task-1"],
        agent_type="coding",
    ))

    dag.add_task(dags.TaskNode(
        task_id="task-3",
        description="Implement core module B",
        prerequisites=["task-1"],
        agent_type="coding",
    ))

    dag.add_task(dags.TaskNode(
        task_id="task-4",
        description="Integrate modules A and B",
        prerequisites=["task-2", "task-3"],
        agent_type="coding",
    ))

    return dag


def initialize_repl(dag_path: str | Path = ".maestro/task_dag.json") -> MaestroREPL:
    """
    Initialize and return a MaestroREPL instance.

    If no DAG exists at the path, creates a sample DAG.

    Args:
        dag_path: Path to the DAG JSON file.

    Returns:
        A configured MaestroREPL instance.
    """
    path = Path(dag_path)

    if path.exists():
        dag = dags.TaskDAG.load(dag_path)
    else:
        dag = create_sample_dag()
        path.parent.mkdir(parents=True, exist_ok=True)
        dag.save(dag_path)

    return MaestroREPL(dag)
