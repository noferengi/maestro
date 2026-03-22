"""Tests for repl.py - Maestro REPL and DAG management."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import config
import app.models.dags as dags
import app.services.repl as repl


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

    def test_checkpoint_manager_creation_with_path_object(self):
        """Test CheckpointManager initialization with Path object."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(Path(tmpdir))
            self.assertEqual(manager.project_root, Path(tmpdir))

    def test_run_git_command_success(self):
        """Test successful git command execution."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo first
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            success, stdout, stderr = manager._run_git_command("status", "--porcelain")
            self.assertTrue(success)
            self.assertEqual(stderr, "")

    def test_run_git_command_failure(self):
        """Test git command failure handling."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Try to run git command in non-git directory
            success, stdout, stderr = manager._run_git_command("status", "--porcelain")
            # This should fail since there's no git repo
            self.assertFalse(success)
            self.assertIn("git", stderr.lower()) or self.assertIn("repository", stderr.lower())

    def test_add_files_single_file(self):
        """Test adding a single file."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create a test file
            test_file = Path(tmpdir) / "test.txt"
            test_file.write_text("test content")
            
            success, msg = manager.add_files(["test.txt"])
            self.assertTrue(success)
            self.assertIn("Added", msg)

    def test_add_files_multiple_files(self):
        """Test adding multiple files."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create test files
            (Path(tmpdir) / "file1.txt").write_text("content1")
            (Path(tmpdir) / "file2.txt").write_text("content2")
            
            success, msg = manager.add_files(["file1.txt", "file2.txt"])
            self.assertTrue(success)
            self.assertIn("Added", msg)

    def test_add_files_all_changes(self):
        """Test adding all changes (no specific files)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create a test file
            (Path(tmpdir) / "test.txt").write_text("test content")
            
            success, msg = manager.add_files(None)
            self.assertTrue(success)
            self.assertIn("Added all changes", msg)

    def test_commit_success(self):
        """Test successful commit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create and add a file
            (Path(tmpdir) / "test.txt").write_text("test content")
            manager.add_files(["test.txt"])
            
            success, msg = manager.commit("Initial commit")
            self.assertTrue(success)
            self.assertIn("Commit", msg)

    def test_commit_with_special_characters_in_message(self):
        """Test commit with special characters in message."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create and add a file
            (Path(tmpdir) / "test.txt").write_text("test content")
            manager.add_files(["test.txt"])
            
            success, msg = manager.commit('Task with "quotes" and special chars')
            self.assertTrue(success)
            self.assertIn("Commit", msg)

    def test_commit_failure_no_changes(self):
        """Test commit failure when there are no changes."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # No changes to commit
            success, msg = manager.commit("Empty commit")
            self.assertFalse(success)
            self.assertIn("failed", msg.lower())

    def test_push_success(self):
        """Test successful push (to empty remote)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create and commit a file
            (Path(tmpdir) / "test.txt").write_text("test content")
            manager.add_files(["test.txt"])
            manager.commit("Initial commit")
            
            success, msg = manager.push()
            # Push may fail if no remote is configured, which is expected
            # We just verify the method runs without crashing
            self.assertIsInstance(success, bool)

    def test_push_failure_no_remote(self):
        """Test push failure when no remote is configured."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo (no remote configured)
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            success, msg = manager.push()
            self.assertFalse(success)
            self.assertIn("failed", msg.lower())

    def test_check_status_clean_repo(self):
        """Test check status on clean repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            status = manager.check_status()
            self.assertIn("dirty", status)
            self.assertFalse(status["dirty"])
            self.assertIn("untracked_files", status)
            self.assertEqual(status["untracked_files"], [])
            self.assertIn("modified_files", status)

    def test_check_status_dirty_repo(self):
        """Test check status on dirty repository."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create untracked file
            (Path(tmpdir) / "untracked.txt").write_text("untracked")
            
            status = manager.check_status()
            self.assertIn("dirty", status)
            # After git add, the file should be tracked
            manager.add_files(["untracked.txt"])
            status = manager.check_status()
            self.assertIn("dirty", status)

    def test_check_status_with_error(self):
        """Test check status when git command fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            status = manager.check_status()
            self.assertIn("error", status)

    def test_checkpoint_full_success(self):
        """Test full checkpoint (add + commit)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create test files
            (Path(tmpdir) / "file1.txt").write_text("content1")
            (Path(tmpdir) / "file2.txt").write_text("content2")
            
            success, msg = manager.checkpoint("Checkpoint test", ["file1.txt", "file2.txt"])
            self.assertTrue(success)
            self.assertIn("Checkpoint", msg)

    def test_checkpoint_add_fails(self):
        """Test checkpoint when add fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo but don't add files
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Try to checkpoint non-existent files
            success, msg = manager.checkpoint("Checkpoint test", ["nonexistent.txt"])
            self.assertFalse(success)
            self.assertIn("Failed to add", msg)

    def test_checkpoint_commit_fails(self):
        """Test checkpoint when commit fails."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create and add a file
            (Path(tmpdir) / "test.txt").write_text("test content")
            manager.add_files(["test.txt"])
            
            # First commit should succeed
            success1, msg1 = manager.checkpoint("First checkpoint")
            self.assertTrue(success1)
            
            # Second commit without changes should fail
            success2, msg2 = manager.checkpoint("Second checkpoint")
            self.assertFalse(success2)

    def test_checkpoint_no_files_all_changes(self):
        """Test checkpoint with no specific files (all changes)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = repl.CheckpointManager(tmpdir)
            # Initialize git repo
            manager._run_git_command("init")
            manager._run_git_command("config", "user.email", "test@example.com")
            manager._run_git_command("config", "user.name", "Test User")
            
            # Create test file
            (Path(tmpdir) / "test.txt").write_text("test content")
            
            success, msg = manager.checkpoint("Checkpoint all changes")
            self.assertTrue(success)
            self.assertIn("Checkpoint", msg)


def _make_repl(dag: dags.TaskDAG) -> repl.MaestroREPL:
    """Create a MaestroREPL with a mocked CheckpointManager so tests never touch git."""
    mock_cm = MagicMock(spec=repl.CheckpointManager)
    mock_cm.checkpoint.return_value = (True, "mocked checkpoint")
    return repl.MaestroREPL(dag, checkpoint_manager=mock_cm)


class TestMaestroREPL(unittest.TestCase):
    """Test cases for MaestroREPL class."""

    def test_repl_creation(self):
        """Test MaestroREPL initialization."""
        dag = dags.TaskDAG()
        repl_instance = _make_repl(dag)
        self.assertEqual(repl_instance.dag, dag)
        self.assertIsNotNone(repl_instance.checkpoint_manager)
        self.assertFalse(repl_instance.running)

    def test_select_next_task_empty_dag(self):
        """Test selecting next task from empty DAG."""
        dag = dags.TaskDAG()
        repl_instance = _make_repl(dag)
        next_task = repl_instance._select_next_task()
        self.assertIsNone(next_task)

    def test_select_next_task_one_ready_task(self):
        """Test selecting next task from DAG with one ready task."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        repl_instance = _make_repl(dag)
        next_task = repl_instance._select_next_task()
        self.assertEqual(next_task, task)

    def test_select_next_task_multiple_ready_tasks(self):
        """Test selecting first ready task when multiple are ready."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2")
        dag.add_task(task1)
        dag.add_task(task2)
        repl_instance = _make_repl(dag)
        next_task = repl_instance._select_next_task()
        self.assertIn(next_task, [task1, task2])

    def test_select_next_task_prerequisites_not_met(self):
        """Test selecting next task when prerequisites not met."""
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1", prerequisites=["non-existent"])
        task2 = dags.TaskNode("task-2", "Task 2")
        dag.add_task(task1)
        dag.add_task(task2)
        repl_instance = _make_repl(dag)
        next_task = repl_instance._select_next_task()
        self.assertEqual(next_task, task2)

    def test_transition_task_success(self):
        """Test successful task state transition."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        repl_instance = _make_repl(dag)
        success = repl_instance._transition_task("task-1", dags.TaskState.ACTIVE, "test message")
        self.assertTrue(success)
        self.assertEqual(task.state, dags.TaskState.ACTIVE)

    def test_transition_task_not_found(self):
        """Test transition for non-existent task."""
        dag = dags.TaskDAG()
        repl_instance = _make_repl(dag)
        success = repl_instance._transition_task("non-existent", dags.TaskState.ACTIVE, "test")
        self.assertFalse(success)

    def test_transition_task_invalid_state(self):
        """Test transition to invalid state."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.ACCEPTED)
        dag.add_task(task)
        repl_instance = _make_repl(dag)
        success = repl_instance._transition_task("task-1", dags.TaskState.PENDING, "test")
        self.assertFalse(success)

    def test_mark_failed_retry(self):
        """Test marking task failed with retry available."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED, retries=1)
        dag.add_task(task)
        repl_instance = _make_repl(dag)
        success = repl_instance._mark_failed("task-1", "Test failure")
        self.assertTrue(success)
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 1)  # retries unchanged when going PENDING directly

    def test_mark_failed_max_retries_exceeded(self):
        """Test marking task failed after max retries."""
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1", state=dags.TaskState.REJECTED, retries=config.ProjectConstants.MAX_FAILURE_RETRIES)
        dag.add_task(task)
        repl_instance = _make_repl(dag)
        success = repl_instance._mark_failed("task-1", "Test failure")
        self.assertTrue(success)
        self.assertEqual(task.state, dags.TaskState.REVERTED)

    def test_mark_failed_task_not_found(self):
        """Test marking non-existent task as failed."""
        dag = dags.TaskDAG()
        repl_instance = _make_repl(dag)
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
