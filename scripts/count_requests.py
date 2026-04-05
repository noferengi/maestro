#!/usr/bin/env python
"""Count HTTP requests around dispatch time."""
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(project_root, "logs", "maestro.log")

with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find the dispatch of task-1774564423.800108
task_id = "task-1774564423.800108"
dispatch_line = None

for i, line in enumerate(lines):
    if task_id in line and "Dispatching task" in line:
        dispatch_line = i
        break

if dispatch_line is None:
    print("Dispatch line not found!")
    exit(1)

print("="*80)
print(f"  HTTP REQUEST COUNT FOR {task_id}")
print("="*80)

print(f"\nDispatch line: {dispatch_line}")
print(f"Line content: {lines[dispatch_line].rstrip()}")

# Count HTTP requests after dispatch (within 1 hour)
dispatch_time = lines[dispatch_line]
print(f"\nHTTP 200 OK requests after dispatch:")

count = 0
for i in range(dispatch_line + 1, min(dispatch_line + 3600, len(lines))):
    if "HTTP Request" in lines[i] and "200 OK" in lines[i]:
        count += 1
        print(f"  {count}: {lines[i].rstrip()}")

print(f"\nTotal HTTP 200 OK requests: {count}")

# Check for any errors
print(f"\nHTTP errors after dispatch:")
error_count = 0
for i in range(dispatch_line + 1, min(dispatch_line + 3600, len(lines))):
    if "HTTP" in lines[i] and ("Error" in lines[i] or "error" in lines[i]):
        error_count += 1
        print(f"  {error_count}: {lines[i].rstrip()}")

print(f"\nTotal HTTP errors: {error_count}")

# Check for task advancement messages
print(f"\nTask advancement messages after dispatch:")
advancement_count = 0
for i in range(dispatch_line + 1, min(dispatch_line + 3600, len(lines))):
    if "advanced to" in lines[i].lower() and task_id in lines[i]:
        advancement_count += 1
        print(f"  {advancement_count}: {lines[i].rstrip()}")

print(f"\nTotal task advancement messages: {advancement_count}")
