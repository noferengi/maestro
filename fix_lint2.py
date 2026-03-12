"""Fix linting issue - remove unused dag_path variable."""
with open("test_integration.py", "r") as f:
    content = f.read()

# Remove the unused dag_path variable
old_line = '            dag_path = Path(tmpdir) / ".maestro" / "task_dag.json"\n'
content = content.replace(old_line, '')

with open("test_integration.py", "w") as f:
    f.write(content)

print("Removed unused dag_path variable")
