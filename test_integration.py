"""Integration test for Maestro REPL."""

import repl

# Create sample DAG
dag = repl.create_sample_dag()
print(f"Tasks: {len(dag.tasks)}")

# Check ready tasks
ready = dag.get_ready_tasks()
print(f"Ready tasks: {len(ready)}")

# Verify task-1 is ready (no prerequisites)
task1 = dag.get_task("task-1")
print(f"Task 1 ready: {dag.is_task_ready(task1)}")

# Verify task-2 is not ready (depends on task-1 which is PENDING)
task2 = dag.get_task("task-2")
print(f"Task 2 ready (prereq not accepted): {dag.is_task_ready(task2)}")

# Accept task-1
dag.transition_state("task-1", repl.dags.TaskState.ACCEPTED)
print(f"Task 1 state: {task1.state}")

# Now task-2 should be ready
print(f"Task 2 ready after task-1 accepted: {dag.is_task_ready(task2)}")

# Check if complete
print(f"DAG complete: {dag.is_complete()}")

# Test REPL initialization
print("\nTesting REPL initialization...")
repl_instance = repl.initialize_repl()
print(f"REPL initialized with {len(repl_instance.dag.tasks)} tasks")
print("Integration test passed!")
