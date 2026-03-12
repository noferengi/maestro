"""Integration test for Maestro REPL."""

import tempfile
import unittest
from pathlib import Path

import dags
import repl


class TestIntegration(unittest.TestCase):
    """Integration test cases for Maestro REPL."""

    def test_dag_task_flow(self):
        """Test complete DAG task flow from PENDING to ACCEPTED."""
        dag = repl.create_sample_dag()
        
        # Verify initial state
        self.assertEqual(len(dag.tasks), 4)
        
        # Check ready tasks - task-1 should be ready (no prerequisites)
        ready = dag.get_ready_tasks()
        self.assertEqual(len(ready), 1)
        
        task1 = dag.get_task("task-1")
        self.assertTrue(dag.is_task_ready(task1))
        
        # Task-2 should not be ready (depends on task-1 which is PENDING)
        task2 = dag.get_task("task-2")
        self.assertFalse(dag.is_task_ready(task2))
        
        # Accept task-1 (use force_accept to bypass ACTIVE/VERIFYING for testing)
        self.assertTrue(dag.force_accept("task-1"))
        self.assertEqual(task1.state, dags.TaskState.ACCEPTED)
        
        # Now task-2 should be ready (task-1 is ACCEPTED)
        self.assertTrue(dag.is_task_ready(task2))
        
        # DAG should not be complete yet
        self.assertFalse(dag.is_complete())

    def test_checkpoint_manager_integration(self):
        """Test CheckpointManager integration with DAG."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            
            # Create and save DAG
            dag = repl.create_sample_dag()
            dag.save(dag_path)
            
            # Create checkpoint manager
            checkpoint_manager = repl.CheckpointManager(tmpdir)
            
            # Initialize git repo
            checkpoint_manager._run_git_command("init")
            checkpoint_manager._run_git_command("config", "user.email", "test@example.com")
            checkpoint_manager._run_git_command("config", "user.name", "Test User")
            
            # Create and add a file to checkpoint
            test_file = Path(tmpdir) / "test_integration.txt"
            test_file.write_text("Integration test data")
            
            # Perform checkpoint
            success, msg = checkpoint_manager.checkpoint("Integration test checkpoint", ["test_integration.txt"])
            self.assertTrue(success)
            self.assertIn("Checkpoint", msg)
            
            # Verify git commit was created
            success, stdout, _ = checkpoint_manager._run_git_command("log", "--oneline")
            self.assertTrue(success)
            self.assertIn("Integration test checkpoint", stdout)

    def test_repl_checkpoint_on_accept(self):
        """Test that REPL creates checkpoint when task is accepted."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            
            # Initialize DAG
            dag = repl.create_sample_dag()
            
            # Create checkpoint manager
            checkpoint_manager = repl.CheckpointManager(tmpdir)
            
            # Initialize git repo
            checkpoint_manager._run_git_command("init")
            checkpoint_manager._run_git_command("config", "user.email", "test@example.com")
            checkpoint_manager._run_git_command("config", "user.name", "Test User")
            
            # Create REPL with custom checkpoint manager
            repl_instance = repl.MaestroREPL(dag, checkpoint_manager, tmpdir)
            
            # Transition task-1 to ACCEPTED via ACTIVE -> VERIFYING -> ACCEPTED
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.ACTIVE, "Task started"))
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.VERIFYING, "Task verified"))
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.ACCEPTED, "Task completed"))
            
            # Verify DAG was saved
            self.assertTrue(dag_path.exists())
            
            # Verify git commit was created (from checkpoint on ACCEPTED)
            success, stdout, _ = checkpoint_manager._run_git_command("log", "--oneline")
            self.assertTrue(success)
            self.assertIn("task-1", stdout.lower())

    def test_repl_full_workflow(self):
        """Test complete REPL workflow with checkpointing."""
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"
            
            # Initialize git repo
            checkpoint_manager = repl.CheckpointManager(tmpdir)
            checkpoint_manager._run_git_command("init")
            checkpoint_manager._run_git_command("config", "user.email", "test@example.com")
            checkpoint_manager._run_git_command("config", "user.name", "Test User")
            
            # Create sample DAG
            dag = repl.create_sample_dag()
            
            # Initialize REPL
            repl_instance = repl.MaestroREPL(dag, checkpoint_manager, tmpdir)
            
            # Simulate running through tasks
            # In a real implementation, agents would be invoked here
            # For this test, we manually transition tasks
            
            # Accept task-1 (no prerequisites) via ACTIVE -> VERIFYING -> ACCEPTED
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.ACTIVE, "Task 1 started"))
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.VERIFYING, "Task 1 verified"))
            self.assertTrue(repl_instance._transition_task("task-1", dags.TaskState.ACCEPTED, "Task 1 completed"))
            
            # Now task-2 and task-3 should be ready
            task2 = dag.get_task("task-2")
            task3 = dag.get_task("task-3")
            self.assertTrue(dag.is_task_ready(task2))
            self.assertTrue(dag.is_task_ready(task3))
            
            # Accept task-2 via ACTIVE -> VERIFYING -> ACCEPTED
            self.assertTrue(repl_instance._transition_task("task-2", dags.TaskState.ACTIVE, "Task 2 started"))
            self.assertTrue(repl_instance._transition_task("task-2", dags.TaskState.VERIFYING, "Task 2 verified"))
            self.assertTrue(repl_instance._transition_task("task-2", dags.TaskState.ACCEPTED, "Task 2 completed"))
            
            # Accept task-3 via ACTIVE -> VERIFYING -> ACCEPTED
            self.assertTrue(repl_instance._transition_task("task-3", dags.TaskState.ACTIVE, "Task 3 started"))
            self.assertTrue(repl_instance._transition_task("task-3", dags.TaskState.VERIFYING, "Task 3 verified"))
            self.assertTrue(repl_instance._transition_task("task-3", dags.TaskState.ACCEPTED, "Task 3 completed"))
            
            # Now task-4 should be ready
            task4 = dag.get_task("task-4")
            self.assertTrue(dag.is_task_ready(task4))
            
            # Accept task-4 via ACTIVE -> VERIFYING -> ACCEPTED
            self.assertTrue(repl_instance._transition_task("task-4", dags.TaskState.ACTIVE, "Task 4 started"))
            self.assertTrue(repl_instance._transition_task("task-4", dags.TaskState.VERIFYING, "Task 4 verified"))
            self.assertTrue(repl_instance._transition_task("task-4", dags.TaskState.ACCEPTED, "Task 4 completed"))
            
            # DAG should be complete
            self.assertTrue(dag.is_complete())
            
            # Verify commits were created
            success, stdout, _ = checkpoint_manager._run_git_command("log", "--oneline")
            self.assertTrue(success)
            # Count commits (should be at least 4 - one per task)
            commit_count = len(stdout.strip().split("\n"))
            self.assertGreaterEqual(commit_count, 4)


if __name__ == "__main__":
    unittest.main()
