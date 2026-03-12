"""Fix linting issue."""
with open("test_integration.py", "r") as f:
    content = f.read()

# Remove the unused dag_path variable
old = """    def test_repl_full_workflow(self):
        \"\"\"Test complete REPL workflow with checkpointing.\"\"\"
        with tempfile.TemporaryDirectory() as tmpdir:
            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"

            # Initialize git repo
            checkpoint_manager = repl.CheckpointManager(tmpdir)"""

new = """    def test_repl_full_workflow(self):
        \"\"\"Test complete REPL workflow with checkpointing.\"\"\"
        with tempfile.TemporaryDirectory() as tmpdir:
            # Initialize git repo
            checkpoint_manager = repl.CheckpointManager(tmpdir)"""

content = content.replace(old, new)

with open("test_integration.py", "w") as f:
    f.write(content)

print("Fixed linting issue")
