"""Tests for repl.py - Maestro REPL and DAG management."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import config
import app.models.dags as dags
import app.services.repl as repl


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _git_ok(stdout=""):
    """Return a successful (True, stdout, "") git stub result."""
    return (True, stdout, "")


def _git_fail(stderr="git error"):
    """Return a failed (False, '', stderr) git stub result."""
    return (False, "", stderr)


def _make_git_stub(*responses):
    """
    Return a side_effect callable for patch.object on _run_git_command.

    Each call consumes the next response; the last response repeats.
    Default (no args) -> always returns _git_ok().
    """
    queue = list(responses) if responses else [_git_ok()]

    def _stub(*args):
        return queue.pop(0) if len(queue) > 1 else queue[0]

    return _stub


# ---------------------------------------------------------------------------
# TaskNode
# ---------------------------------------------------------------------------

class TestTaskNode(unittest.TestCase):
    """Test cases for TaskNode class."""

    def test_task_node_creation(self):
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
        task = dags.TaskNode(task_id="test-1", description="Test task")
        self.assertEqual(task.prerequisites, [])
        self.assertEqual(task.state, dags.TaskState.PENDING)
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.agent_type, "coding")

    def test_task_node_to_dict(self):
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
        self.assertEqual(task.state, dags.TaskState.ACTIVE)
        self.assertEqual(task.retries, 2)

    def test_task_node_from_dict_minimal(self):
        data = {"task_id": "test-1", "description": "Test task", "state": "PENDING"}
        task = dags.TaskNode.from_dict(data)
        self.assertEqual(task.prerequisites, [])
        self.assertEqual(task.retries, 0)
        self.assertEqual(task.agent_type, "coding")


# ---------------------------------------------------------------------------
# TaskDAG
# ---------------------------------------------------------------------------

class TestTaskDAG(unittest.TestCase):
    """Test cases for TaskDAG class."""

    def test_dag_creation(self):
        dag = dags.TaskDAG()
        self.assertEqual(dag.tasks, [])
        self.assertEqual(dag._task_map, {})

    def test_dag_with_initial_tasks(self):
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        dag = dags.TaskDAG([task1, task2])
        self.assertEqual(len(dag.tasks), 2)
        self.assertEqual(dag.get_task("task-1"), task1)

    def test_add_task(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        self.assertEqual(len(dag.tasks), 1)
        self.assertEqual(dag.get_task("task-1"), task)

    def test_get_task_not_found(self):
        self.assertIsNone(dags.TaskDAG().get_task("non-existent"))

    def test_is_task_ready_no_prerequisites(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        dag.add_task(task)
        self.assertTrue(dag.is_task_ready(task))

    def test_is_task_ready_prerequisites_not_completed(self):
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        dag.add_task(task1)
        dag.add_task(task2)
        self.assertFalse(dag.is_task_ready(task2))

    def test_is_task_ready_prerequisites_completed(self):
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        task1.state = dags.TaskState.ACCEPTED
        dag.add_task(task1)
        dag.add_task(task2)
        self.assertTrue(dag.is_task_ready(task2))

    def test_is_task_ready_not_pending(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("task-1", "Task 1")
        task.state = dags.TaskState.ACTIVE
        dag.add_task(task)
        self.assertFalse(dag.is_task_ready(task))

    def test_get_ready_tasks(self):
        dag = dags.TaskDAG()
        task1 = dags.TaskNode("task-1", "Task 1")
        task2 = dags.TaskNode("task-2", "Task 2", prerequisites=["task-1"])
        task3 = dags.TaskNode("task-3", "Task 3", prerequisites=["non-existent"])
        dag.add_task(task1)
        dag.add_task(task2)
        dag.add_task(task3)
        ready = dag.get_ready_tasks()
        self.assertEqual(len(ready), 1)
        self.assertIn(task1, ready)

    def test_get_active_tasks(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.ACTIVE))
        dag.add_task(dags.TaskNode("t2", "T2", state=dags.TaskState.PENDING))
        self.assertEqual(len(dag.get_active_tasks()), 1)

    def test_get_accepted_tasks(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.ACCEPTED))
        dag.add_task(dags.TaskNode("t2", "T2", state=dags.TaskState.PENDING))
        self.assertEqual(len(dag.get_accepted_tasks()), 1)

    def test_is_complete_all_accepted(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.ACCEPTED))
        dag.add_task(dags.TaskNode("t2", "T2", state=dags.TaskState.ACCEPTED))
        self.assertTrue(dag.is_complete())

    def test_is_complete_not_complete(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.ACCEPTED))
        dag.add_task(dags.TaskNode("t2", "T2", state=dags.TaskState.PENDING))
        self.assertFalse(dag.is_complete())

    def test_transition_state_valid(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1")
        dag.add_task(task)
        self.assertTrue(dag.transition_state("t1", dags.TaskState.ACTIVE))
        self.assertEqual(task.state, dags.TaskState.ACTIVE)

    def test_transition_state_invalid(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1", state=dags.TaskState.ACCEPTED)
        dag.add_task(task)
        self.assertFalse(dag.transition_state("t1", dags.TaskState.PENDING))

    def test_transition_state_task_not_found(self):
        self.assertFalse(dags.TaskDAG().transition_state("non-existent", dags.TaskState.ACTIVE))

    def test_transition_state_reject_to_pending(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1", state=dags.TaskState.REJECTED, retries=1)
        dag.add_task(task)
        self.assertTrue(dag.transition_state("t1", dags.TaskState.PENDING))
        self.assertEqual(task.state, dags.TaskState.PENDING)

    def test_mark_as_reverted(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.REJECTED))
        self.assertTrue(dag.mark_as_reverted("t1"))
        self.assertEqual(dag.get_task("t1").state, dags.TaskState.REVERTED)

    def test_to_dict(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1"))
        result = dag.to_dict()
        self.assertIn("tasks", result)
        self.assertEqual(result["tasks"][0]["task_id"], "t1")

    def test_from_dict(self):
        data = {"tasks": [{"task_id": "t1", "description": "T1",
                           "prerequisites": [], "state": "PENDING",
                           "retries": 0, "agent_type": "coding"}]}
        dag = dags.TaskDAG.from_dict(data)
        self.assertEqual(dag.tasks[0].task_id, "t1")

    def test_save_and_load(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            dag = dags.TaskDAG()
            dag.add_task(dags.TaskNode("t1", "T1"))
            dag.save(dag_path)
            loaded = dags.TaskDAG.load(dag_path)
            self.assertEqual(loaded.tasks[0].task_id, "t1")


# ---------------------------------------------------------------------------
# CheckpointManager - git calls are stubbed, we verify logic and call args
# ---------------------------------------------------------------------------

class TestCheckpointManager(unittest.TestCase):
    """
    CheckpointManager wraps git via _run_git_command.  We test the logic of
    add_files / commit / push / check_status / checkpoint by stubbing that
    single boundary method.  We are not testing git itself.
    """

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self.manager = repl.CheckpointManager(self._tmpdir)

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _patch_git(self, *responses):
        """patch.object context manager that stubs _run_git_command."""
        return patch.object(self.manager, '_run_git_command',
                            side_effect=_make_git_stub(*responses))

    # --- construction ---

    def test_checkpoint_manager_creation(self):
        self.assertEqual(self.manager.project_root, Path(self._tmpdir))

    def test_checkpoint_manager_creation_with_path_object(self):
        m = repl.CheckpointManager(Path(self._tmpdir))
        self.assertEqual(m.project_root, Path(self._tmpdir))

    # --- _run_git_command itself (subprocess boundary) ---

    def test_run_git_command_success(self):
        """_run_git_command parses returncode=0 subprocess result as success."""
        mock_proc = MagicMock()
        mock_proc.returncode = 0
        mock_proc.stdout = "On branch main\n"
        mock_proc.stderr = ""
        with patch("subprocess.run", return_value=mock_proc) as mock_run, \
             patch.object(self.manager, '_is_maestro_repo', return_value=False):
            success, stdout, stderr = self.manager._run_git_command("status", "--porcelain")
        self.assertTrue(success)
        self.assertEqual(stdout, "On branch main")
        cmd = mock_run.call_args[0][0]
        self.assertEqual(cmd, ["git", "status", "--porcelain"])

    def test_run_git_command_failure(self):
        """_run_git_command returns failure on non-zero returncode."""
        mock_proc = MagicMock()
        mock_proc.returncode = 128
        mock_proc.stdout = ""
        mock_proc.stderr = "not a git repository"
        with patch("subprocess.run", return_value=mock_proc), \
             patch.object(self.manager, '_is_maestro_repo', return_value=False):
            success, stdout, stderr = self.manager._run_git_command("status")
        self.assertFalse(success)
        self.assertIn("git", stderr.lower())

    # --- add_files ---

    def test_add_files_single_file(self):
        with self._patch_git(_git_ok()) as mock_git:
            success, msg = self.manager.add_files(["test.txt"])
        self.assertTrue(success)
        self.assertIn("Added", msg)
        mock_git.assert_called_once_with("add", "test.txt")

    def test_add_files_multiple_files(self):
        with self._patch_git(_git_ok(), _git_ok()) as mock_git:
            success, msg = self.manager.add_files(["file1.txt", "file2.txt"])
        self.assertTrue(success)
        self.assertEqual(mock_git.call_count, 2)
        mock_git.assert_any_call("add", "file1.txt")
        mock_git.assert_any_call("add", "file2.txt")

    def test_add_files_all_changes(self):
        with self._patch_git(_git_ok()) as mock_git:
            success, msg = self.manager.add_files(None)
        self.assertTrue(success)
        self.assertIn("all changes", msg.lower())
        mock_git.assert_called_once_with("add", ".")

    def test_add_files_failure(self):
        with self._patch_git(_git_fail("pathspec did not match")):
            success, msg = self.manager.add_files(["nonexistent.txt"])
        self.assertFalse(success)
        self.assertIn("Failed to add", msg)

    # --- commit ---

    def test_commit_success(self):
        with self._patch_git(_git_ok("1 file changed")) as mock_git:
            success, msg = self.manager.commit("Initial commit")
        self.assertTrue(success)
        self.assertIn("Commit", msg)
        cmd_args = mock_git.call_args[0]
        self.assertEqual(cmd_args[0], "commit")
        self.assertIn("Initial commit", " ".join(cmd_args))

    def test_commit_with_special_characters_in_message(self):
        with self._patch_git(_git_ok()) as mock_git:
            success, _ = self.manager.commit('Task with "quotes" and special chars')
        self.assertTrue(success)
        # Quotes must be escaped before being passed to git
        full_cmd = " ".join(mock_git.call_args[0])
        self.assertIn('\\"', full_cmd)

    def test_commit_failure_no_changes(self):
        with self._patch_git(_git_fail("nothing to commit")):
            success, msg = self.manager.commit("Empty commit")
        self.assertFalse(success)
        self.assertIn("failed", msg.lower())

    # --- push ---

    def test_push_success(self):
        with self._patch_git(_git_ok()) as mock_git:
            success, msg = self.manager.push()
        self.assertTrue(success)
        mock_git.assert_called_once_with("push")

    def test_push_failure_no_remote(self):
        with self._patch_git(_git_fail("No configured push destination")):
            success, msg = self.manager.push()
        self.assertFalse(success)
        self.assertIn("failed", msg.lower())

    # --- check_status ---

    def test_check_status_clean_repo(self):
        with self._patch_git(_git_ok("")):
            status = self.manager.check_status()
        self.assertFalse(status["dirty"])
        self.assertEqual(status["untracked_files"], [])
        self.assertEqual(status["modified_files"], [])

    def test_check_status_dirty_repo(self):
        # porcelain output: untracked + modified
        with self._patch_git(_git_ok("?? untracked.txt\n M modified.py")):
            status = self.manager.check_status()
        self.assertTrue(status["dirty"])
        self.assertEqual(len(status["untracked_files"]), 1)
        self.assertEqual(len(status["modified_files"]), 1)

    def test_check_status_with_error(self):
        with self._patch_git(_git_fail("not a git repository")):
            status = self.manager.check_status()
        self.assertIn("error", status)

    # --- checkpoint (add + commit) ---

    def test_checkpoint_full_success(self):
        # add call (True) then commit call (True)
        with self._patch_git(_git_ok(), _git_ok()) as mock_git:
            success, msg = self.manager.checkpoint("Checkpoint test", ["file1.txt", "file2.txt"])
        self.assertTrue(success)
        self.assertIn("Checkpoint", msg)
        # first call is add, second is commit
        self.assertEqual(mock_git.call_args_list[0], call("add", "file1.txt"))
        self.assertEqual(mock_git.call_args_list[1], call("add", "file2.txt"))

    def test_checkpoint_add_fails(self):
        with self._patch_git(_git_fail("pathspec did not match")):
            success, msg = self.manager.checkpoint("Test", ["nonexistent.txt"])
        self.assertFalse(success)
        self.assertIn("Failed to add", msg)

    def test_checkpoint_commit_fails(self):
        # add succeeds, commit fails
        with self._patch_git(_git_ok(), _git_fail("nothing to commit")):
            success, msg = self.manager.checkpoint("Second checkpoint")
        self.assertFalse(success)

    def test_checkpoint_no_files_all_changes(self):
        # add . (True) then commit (True)
        with self._patch_git(_git_ok(), _git_ok()) as mock_git:
            success, msg = self.manager.checkpoint("Checkpoint all changes")
        self.assertTrue(success)
        self.assertEqual(mock_git.call_args_list[0], call("add", "."))


# ---------------------------------------------------------------------------
# MaestroREPL - CheckpointManager is always mocked
# ---------------------------------------------------------------------------

def _make_repl(dag: dags.TaskDAG) -> repl.MaestroREPL:
    """Create a MaestroREPL with a mocked CheckpointManager so tests never touch git."""
    mock_cm = MagicMock(spec=repl.CheckpointManager)
    mock_cm.checkpoint.return_value = (True, "mocked checkpoint")
    return repl.MaestroREPL(dag, checkpoint_manager=mock_cm)


class TestMaestroREPL(unittest.TestCase):
    """Test cases for MaestroREPL class."""

    def test_repl_creation(self):
        dag = dags.TaskDAG()
        r = _make_repl(dag)
        self.assertEqual(r.dag, dag)
        self.assertIsNotNone(r.checkpoint_manager)
        self.assertFalse(r.running)

    def test_select_next_task_empty_dag(self):
        self.assertIsNone(_make_repl(dags.TaskDAG())._select_next_task())

    def test_select_next_task_one_ready_task(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1")
        dag.add_task(task)
        self.assertEqual(_make_repl(dag)._select_next_task(), task)

    def test_select_next_task_multiple_ready_tasks(self):
        dag = dags.TaskDAG()
        t1 = dags.TaskNode("t1", "T1")
        t2 = dags.TaskNode("t2", "T2")
        dag.add_task(t1)
        dag.add_task(t2)
        self.assertIn(_make_repl(dag)._select_next_task(), [t1, t2])

    def test_select_next_task_prerequisites_not_met(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", prerequisites=["non-existent"]))
        t2 = dags.TaskNode("t2", "T2")
        dag.add_task(t2)
        self.assertEqual(_make_repl(dag)._select_next_task(), t2)

    def test_transition_task_success(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1")
        dag.add_task(task)
        r = _make_repl(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            r.project_root = Path(tmpdir)
            self.assertTrue(r._transition_task("t1", dags.TaskState.ACTIVE, "started"))
        self.assertEqual(task.state, dags.TaskState.ACTIVE)

    def test_transition_task_not_found(self):
        self.assertFalse(_make_repl(dags.TaskDAG())._transition_task("x", dags.TaskState.ACTIVE, ""))

    def test_transition_task_invalid_state(self):
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.ACCEPTED))
        r = _make_repl(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            r.project_root = Path(tmpdir)
            self.assertFalse(r._transition_task("t1", dags.TaskState.PENDING, ""))

    def test_transition_task_accepted_calls_checkpoint(self):
        """Transitioning to ACCEPTED must call checkpoint_manager.checkpoint."""
        dag = dags.TaskDAG()
        dag.add_task(dags.TaskNode("t1", "T1", state=dags.TaskState.VERIFYING))
        r = _make_repl(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            r.project_root = Path(tmpdir)
            r._transition_task("t1", dags.TaskState.ACCEPTED, "done")
        r.checkpoint_manager.checkpoint.assert_called_once()
        msg = r.checkpoint_manager.checkpoint.call_args[0][0]
        self.assertIn("t1", msg)

    def test_mark_failed_retry(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1", state=dags.TaskState.REJECTED, retries=1)
        dag.add_task(task)
        r = _make_repl(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            r.project_root = Path(tmpdir)
            self.assertTrue(r._mark_failed("t1", "failure"))
        self.assertEqual(task.state, dags.TaskState.PENDING)

    def test_mark_failed_max_retries_exceeded(self):
        dag = dags.TaskDAG()
        task = dags.TaskNode("t1", "T1", state=dags.TaskState.REJECTED,
                             retries=config.ProjectConstants.MAX_FAILURE_RETRIES)
        dag.add_task(task)
        r = _make_repl(dag)
        with tempfile.TemporaryDirectory() as tmpdir:
            r.project_root = Path(tmpdir)
            self.assertTrue(r._mark_failed("t1", "too many failures"))
        self.assertEqual(task.state, dags.TaskState.REVERTED)

    def test_mark_failed_task_not_found(self):
        self.assertFalse(_make_repl(dags.TaskDAG())._mark_failed("x", "reason"))

    def test_create_sample_dag(self):
        dag = repl.create_sample_dag()
        self.assertEqual(len(dag.tasks), 4)
        ids = [t.task_id for t in dag.tasks]
        for i in range(1, 5):
            self.assertIn(f"task-{i}", ids)

    def test_initialize_repl_new_dag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            r = repl.initialize_repl(dag_path)
            self.assertEqual(len(r.dag.tasks), 4)
            self.assertTrue(dag_path.exists())

    def test_initialize_repl_existing_dag(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            dag = dags.TaskDAG()
            dag.add_task(dags.TaskNode("custom", "Custom"))
            dag.save(dag_path)
            r = repl.initialize_repl(dag_path)
            self.assertEqual(r.dag.tasks[0].task_id, "custom")


if __name__ == "__main__":
    unittest.main()
