"""Debug test for DAG ready logic."""

import app.services.repl as repl

dag = repl.create_sample_dag()
task2 = dag.get_task("task-2")
print(f"Task 2 prereqs: {task2.prerequisites}")

# Use force_accept to go directly to ACCEPTED (for testing)
dag.force_accept("task-1")
task1 = dag.get_task("task-1")
print(f"Task 1 state after force_accept: {task1.state}")
print(f"Task 2 ready after task-1 accepted: {dag.is_task_ready(task2)}")
