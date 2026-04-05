"""Integration tests for Maestro REPL.

Git is mocked at the CheckpointManager level - we verify that the REPL
calls checkpoint with the right arguments, not that git actually ran.
"""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

import app.models.dags as dags
import app.services.repl as repl


def _mock_cm():
    """CheckpointManager stub: checkpoint always succeeds."""
    cm = MagicMock(spec=repl.CheckpointManager)
    cm.checkpoint.return_value = (True, "ok")
    return cm


class TestIntegration(unittest.TestCase):

    def test_dag_task_flow(self):
        """Complete DAG task flow from PENDING to ACCEPTED - no git involved."""
        dag = repl.create_sample_dag()
        self.assertEqual(len(dag.tasks), 4)

        ready = dag.get_ready_tasks()
        self.assertEqual(len(ready), 1)

        task1 = dag.get_task("task-1")
        self.assertTrue(dag.is_task_ready(task1))

        task2 = dag.get_task("task-2")
        self.assertFalse(dag.is_task_ready(task2))

        self.assertTrue(dag.force_accept("task-1"))
        self.assertEqual(task1.state, dags.TaskState.ACCEPTED)
        self.assertTrue(dag.is_task_ready(task2))
        self.assertFalse(dag.is_complete())

    def test_checkpoint_manager_integration(self):
        """CheckpointManager.checkpoint is called when a task is accepted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag = repl.create_sample_dag()
            cm = _mock_cm()
            r = repl.MaestroREPL(dag, cm, tmpdir)

            r._transition_task("task-1", dags.TaskState.ACTIVE, "started")
            r._transition_task("task-1", dags.TaskState.VERIFYING, "verified")
            r._transition_task("task-1", dags.TaskState.ACCEPTED, "Integration test checkpoint")

            # checkpoint must have been called exactly once (on ACCEPTED)
            cm.checkpoint.assert_called_once()
            msg = cm.checkpoint.call_args[0][0]
            self.assertIn("task-1", msg)
            self.assertIn("Integration test checkpoint", msg)

    def test_repl_checkpoint_on_accept(self):
        """REPL calls checkpoint_manager.checkpoint exactly once per ACCEPTED transition."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag = repl.create_sample_dag()
            cm = _mock_cm()
            r = repl.MaestroREPL(dag, cm, tmpdir)

            r._transition_task("task-1", dags.TaskState.ACTIVE, "Task started")
            r._transition_task("task-1", dags.TaskState.VERIFYING, "Task verified")
            r._transition_task("task-1", dags.TaskState.ACCEPTED, "Task completed")

            cm.checkpoint.assert_called_once()
            msg = cm.checkpoint.call_args[0][0]
            self.assertIn("task-1", msg.lower())

    def test_repl_full_workflow(self):
        """Accepting all 4 tasks produces 4 checkpoint calls and a complete DAG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag = repl.create_sample_dag()
            cm = _mock_cm()
            r = repl.MaestroREPL(dag, cm, tmpdir)

            def _accept(task_id, note):
                r._transition_task(task_id, dags.TaskState.ACTIVE, note)
                r._transition_task(task_id, dags.TaskState.VERIFYING, note)
                r._transition_task(task_id, dags.TaskState.ACCEPTED, note)

            _accept("task-1", "Task 1 completed")
            _accept("task-2", "Task 2 completed")
            _accept("task-3", "Task 3 completed")
            _accept("task-4", "Task 4 completed")

            self.assertTrue(dag.is_complete())
            # One checkpoint call per ACCEPTED transition
            self.assertEqual(cm.checkpoint.call_count, 4)


if __name__ == "__main__":
    unittest.main()
