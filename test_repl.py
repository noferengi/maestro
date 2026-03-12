"""Tests for repl.py - Maestro REPL and DAG management."""

import tempfile
import unittest
from pathlib import Path

import config
import dags
import repl


class TestTaskNode(unittest.TestCase):
    """Test cases for TaskNode class."""

    def test_task_node_creation(self):
        """Test basic task node creation."""
        task = dags.TaskNode(
            task_id="test-1",
            description="Test task",
            prerequisites=["pre-1"],
            state=dags.TaskState.PENDING,
            retries=0,
            agent_type="coding",
        )
        self.assertEqual(task.task_id, "test-1")
        self.assertEqual(task.description, "Test task")
        self.assertEqual(task.prerequisites, ["pre-1"])
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.agent_type, "coding")

    def test_task_node_default_values(self):
        """Test task node with default values."""
        task = dags.TaskNode(task_id="test-1", description="Test task")
        self.assertEqual(task.prerequisites, [])
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.agent_type, "coding")

    def test_task_node_to_dict(self):
        """Test task node serialization to dict."""
        task = dags.TaskNode(
            task_id="test-1",
            description="Test task",
            prerequisites=["pre-1"],
            state=dags.TaskState.ACTIVE,
            retries=2,
            agent_type="debugging",
        )
        result = task.to_dict()
        expected = {
            "task_id": "test-1",
            "description": "Test task",
            "prerequisites": ["pre-1"],
            "state": "ACTIVE",
            "retries": 2,
            "agent_type": "debugging",
        }
        self.assertEqual(result, expected)

    def test_task_node_from_dict(self):
        """Test task node deserialization from dict."""
        data = {
            "task_id": "test-1",
            "description": "Test task",
            "prerequisites": ["pre-1"],
            "state": "ACTIVE",
            "retries": 2,
            "agent_type": "debugging",
        }
        task = dags.TaskNode.from_dict(data)
        self.assertEqual(task.task_id, "test-1")
        self.assertEqual(task.description, "Test task")
        self.assertEqual(task.prerequisites, ["pre-1"])
        self.assertEqual(task.state, dags.TaskState.ACTIVE)
        self.assertEqual(task.retries, 2)
        self.assertEqual(task.agent_type, "debugging")

    def test_task_node_from_dict_minimal(self):
        """Test task node from dict with minimal data."""
        data = {
            "task_id": "test-1",
            "description": "Test task",
            "state": "PENDING",
        }
        task = dags.TaskNode.from_dict(data)
        self.assertEqual(task.task_id, "test-1")
        self.assertEqual(task.description, "Test task")
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.prerequisites, [])
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.agent_type, "coding")


class TestTaskDAG(unittest.TestCase):
    """Test cases for TaskDAG class."""

    def test_dag_creation(self):
        """Test basic DAG creation."""
        dag = dags.TaskDAG()
        self.assertEqual(dag.tasks, [])
        self.assertEqual(dag._task_map, {})

    def test_dag_with_initial_tasks(self):
        """Test DAG with initial tasks."""
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        dag = dags.TaskDAG([task1, task2])
        self.assertEqual(len(dag.tasks), 2)
        self.assertEqual(dag.get_task("task-1"), task1)
        self.assertEqual(dag.get_task("task-2"), task2)

    def test_add_task(self):
        """Test adding a task to DAG."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        self.assertEqual(len(dag.tasks), 1)
        self.assertEqual(dag.get_task("task-1"), task)

    def test_get_task_not_found(self):
        """Test getting a non-existent task."""
        dag = dags.TaskDAG()
        task = dag.get_task("non-existent")
        self.assertIsNone(task)

    def test_is_task_ready_no_prerequisites(self):
        """Test ready task with no prerequisites."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        self.assertTrue(dag.is_task_ready(task))

    def test_is_task_ready_prerequisites_not_completed(self):
        """Test ready task when prerequisites are not completed."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        dag.add_task(task1)
        dag.add_task(task2)
        # task-2 has task-1 as prereq but task-1 is PENDING, not ACCEPTED
        self.assertFalse(dag.is_task_ready(task2))

    def test_is_task_ready_prerequisites_completed(self):
        """Test ready task when all prerequisites are completed."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        task1.state = dags.TaskState.ACCEPTED
        dag.add_task(task1)
        dag.add_task(task2)
        self.assertTrue(dag.is_task_ready(task2))

    def test_is_task_ready_not_pending(self):
        """Test ready task when state is not PENDING."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        task.state = dags.TaskState.ACTIVE
        dag.add_task(task)
        self.assertFalse(dag.is_task_ready(task))

    def test_get_ready_tasks(self):
        """Test getting all ready tasks."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")  # PENDING (no prereqs) - READY
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])  # Ready (prereq pending) - NOT READY
        task3 = dags.TaskNode("task-3", "Task 3", prerequisites=["non-existent"])  # Not ready (missing prereq)
        dag.add_task(task1)
        dag.add_task(task2)
        dag.add_task(task3)
        ready = dag.get_ready_tasks()
        self.assertEqual(len(ready), 1)
        self.assertIn(task1, ready)

    def test_get_active_tasks(self):
        """Test getting active tasks."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACTIVE)
        task2 = dags.TaskNode("task-2", "Task 2", state=dags.TaskState.PENDING)
        dag.add_task(task1)
        dag.add_task(task2)
        active = dag.get_active_tasks()
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0], task1)

    def test_get_accepted_tasks(self):
        """Test getting accepted tasks."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED)
        task2 = dags.TaskNode("task-2", "Task 2", state=dags.TaskState.PENDING)
        dag.add_task(task1)
        dag.add_task(task2)
        accepted = dag.get_accepted_tasks()
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0], task1)

    def test_is_complete_all_accepted(self):
        """Test is_complete when all tasks are accepted."""
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED))
        dag.add_task(dags.TaskNode("task-2", "Task 2", state=dags.TaskState.ACCEPTED))
        self.assertTrue(dag.is_complete())

    def test_is_complete_not_complete(self):
        """Test is_complete when not all tasks are accepted."""
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED))
        dag.add_task(dags.TaskNode("task-2", "Task 2", state=dags.TaskState.PENDING))
        self.assertFalse(dag.is_complete())

    def test_transition_state_valid(self):
        """Test valid state transition."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        self.assertTrue(dag.transition_state("task-1", dags.TaskState.ACTIVE))
        self.assertEqual(task.state, dags.TaskState.ACTIVE)

    def test_transition_state_invalid(self):
        """Test invalid state transition."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED)
        dag.add_task(task)
        # ACCEPTED is terminal, cannot transition to PENDING
        self.assertFalse(dag.transition_state("task-1", dags.TaskState.PENDING))
        self.assertEqual(task.state, dags.TaskState.ACCEPTED)

    def test_transition_state_task_not_found(self):
        """Test transition for non-existent task."""
        dag = dags.TaskDAG()
        self.assertFalse(dag.transition_state("non-existent", dags.TaskState.ACTIVE))

    def test_transition_state_reject_to_pending(self):
        """Test REJECTED -> PENDING transition for retry."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED, retries=1)
        dag.add_task(task)
        self.assertTrue(dag.transition_state("task-1", dags.TaskState.PENDING))
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 1)  # retries unchanged on REJECTED -> PENDING transition

    def test_mark_as_reverted(self):
        """Test marking task as REVERTED."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED)
        dag.add_task(task)
        self.assertTrue(dag.mark_as_reverted("task-1"))
        self.assertEqual(task.state, dags.TaskState.REVERTED)

    def test_to_dict(self):
        """Test DAG serialization to dict."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        result = dag.to_dict()
        self.assertIn("tasks", result)
        self.assertEqual(len(result["tasks"]), 1)
        self.assertEqual(result["tasks"][0]["task_id"], "task-1")

    def test_from_dict(self):
        """Test DAG deserialization from dict."""
        data = {
            "tasks": [
                {
                    "task_id": "task-1",
                    "description": "Task 1",
                    "prerequisites": [],
                    "state": "PENDING",
                    "retries": 0,
                    "agent_type": "coding",
                }
            ]
        }
        dag = dags.TaskDAG.from_dict(data)
        self.assertEqual(len(dag.tasks), 1)
        self.assertEqual(dag.tasks[0].task_id, "task-1")
        self.assertEqual(dag.tasks[0].description, "Task 1")

    def test_save_and_load(self):
        """Test DAG persistence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            dag = dags.TaskDAG()
            task = dags.TaskNode("task-1", "Task 1")
            dag.add_task(task)
            dag.save(dag_path)
            loaded_dag = dags.TaskDAG.load(dag_path)
            self.assertEqual(len(loaded_dag.tasks), 1)
            self.assertEqual(loaded_dag.tasks[0].task_id, "task-1")


class TestCheckpointManager(unittest.TestCase):
    """Test cases for CheckpointManager class."""

    def test_checkpoint_manager_creation(self):
        """Test CheckpointManager initialization."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            self.assertEqual(manager.project_root, Path(tmpdir))

    def test_git_command_not_found(self):
        """Test handling when git is not available."""
        # This test may not work in all environments, but tests error handling
        manager = repl.CheckpointManager("/")
        success, output = manager.add_files()
        # In a test environment without git, this should fail gracefully
        # In a real environment with git, it may succeed or fail for other reasons


class TestMaestroREPL(unittest.TestCase):
    """Test cases for MaestroREPL class."""

    def test_repl_creation(self):
        """Test MaestroREPL initialization."""
        dag = dags.TaskDAG()
        repl_instance = repl.MaestroREPL(dag)
        self.assertEqual(repl_instance.dag, dag)
        self.assertIsNotNone(repl_instance.checkpoint_manager)
        self.assertFalse(repl_instance.running)

    def test_select_next_task_empty_dag(self):
        """Test selecting next task from empty DAG."""
        dag = dags.TaskDAG()
        repl_instance = repl.MaestroREPL(dag)
        next_task = repl_instance._select_next_task()
        self.assertIsNone(next_task)

    def test_select_next_task_one_ready_task(self):
        """Test selecting next task from DAG with one ready task."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        repl_instance = repl.MaestroREPL(dag)
        next_task = repl_instance._select_next_task()
        self.assertEqual(next_task, task)

    def test_select_next_task_multiple_ready_tasks(self):
        """Test selecting first ready task when multiple are ready."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2")
        dag.add_task(task1)
        dag.add_task(task2)
        repl_instance = repl.MaestroREPL(dag)
        next_task = repl_instance._select_next_task()
        self.assertIn(next_task, [task1, task2])

    def test_select_next_task_prerequisites_not_met(self):
        """Test selecting next task when prerequisites not met."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1", prerequisites=["non-existent"])
        task2 = dags.TaskNode("task-2", "Task 2")
        dag.add_task(task1)
        dag.add_task(task2)
        repl_instance = repl.MaestroREPL(dag)
        next_task = repl_instance._select_next_task()
        self.assertEqual(next_task, task2)

    def test_transition_task_success(self):
        """Test successful task state transition."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        repl_instance = repl.MaestroREPL(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            _ = Path(tmpdir) / ".maestro" / "task_dag.json"
            success = repl_instance._transition_task("task-1", dags.TaskState.ACTIVE, "test message")
            self.assertTrue(success)
            self.assertEqual(task.state, dags.TaskState.ACTIVE)

    def test_transition_task_not_found(self):
        """Test transition for non-existent task."""
        dag = dags.TaskDAG()
        repl_instance = repl.MaestroREPL(dag)
        success = repl_instance._transition_task("non-existent", dags.TaskState.ACTIVE, "test")
        self.assertFalse(success)

    def test_transition_task_invalid_state(self):
        """Test transition to invalid state."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED)
        dag.add_task(task)
        repl_instance = repl.MaestroREPL(dag)
        success = repl_instance._transition_task("task-1", dags.TaskState.PENDING, "test")
        self.assertFalse(success)

    def test_mark_failed_retry(self):
        """Test marking task failed with retry available."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED, retries=1)
        dag.add_task(task)
        repl_instance = repl.MaestroREPL(dag)
        success = repl_instance._mark_failed("task-1", "Test failure")
        self.assertTrue(success)
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 1)  # retries unchanged when going PENDING directly

    def test_mark_failed_max_retries_exceeded(self):
        """Test marking task failed after max retries."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED, retries=config.ProjectConstants.MAX_FAILURE_RETRIES)
        dag.add_task(task)
        repl_instance = repl.MaestroREPL(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            _ = Path(tmpdir) / ".maestro" / "task_dag.json"
            success = repl_instance._mark_failed("task-1", "Test failure")
            self.assertTrue(success)
            self.assertEqual(task.state, dags.TaskState.REVERTED)

    def test_mark_failed_task_not_found(self):
        """Test marking non-existent task as failed."""
        dag = dags.TaskDAG()
        repl_instance = repl.MaestroREPL(dag)
        success = repl_instance._mark_failed("non-existent", "Test failure")
        self.assertFalse(success)

    def test_create_sample_dag(self):
        """Test creating a sample DAG."""
        dag = repl.create_sample_dag()
        self.assertEqual(len(dag.tasks), 4)
        task_ids = [t.task_id for t in dag.tasks]
        self.assertIn("task-1", task_ids)
        self.assertIn("task-2", task_ids)
        self.assertIn("task-3", task_ids)
        self.assertIn("task-4", task_ids)

    def test_initialize_repl_new_dag(self):
        """Test initializing REPL with no existing DAG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            repl_instance = repl.initialize_repl(dag_path)
            self.assertEqual(len(repl_instance.dag.tasks), 4)
            self.assertTrue(dag_path.exists())

    def test_initialize_repl_existing_dag(self):
        """Test initializing REPL with existing DAG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            # Create initial DAG
            dag = dags.TaskDAG()
            dag.add_task(dags.TaskNode("custom-task", "Custom task"))
            dag.save(dag_path)
            # Initialize REPL with existing DAG
            repl_instance = repl.initialize_repl(dag_path)
            self.assertEqual(len(repl_instance.dag.tasks), 1)
            self.assertEqual(repl_instance.dag.tasks[0].task_id, "custom-task")


if __name__ == "__main__":
    unittest.main()
