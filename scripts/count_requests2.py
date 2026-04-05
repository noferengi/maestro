#!/usr/bin/env python
"""Count HTTP requests around dispatch time."""
import os
from datetime import datetime

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(project_root, "logs", "maestro.log")

with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

task_id = "task-1774564423.800108"

# Find ALL dispatches of this task
dispatches = []
for i, line in enumerate(lines):
    if task_id in line and "Dispatching task" in line:
        # Parse timestamp
        timestamp_str = line.split("T")[0] + "T" + line.split("T")[1].split(" ")[0]
        try:
            timestamp = datetime.fromisoformat(timestamp_str.replace("T", " "))
            dispatches.append((i, timestamp, line.strip()))
        except:
            pass

print("="*80)
print(f"  ALL DISPATCHES FOR {task_id}")
print("="*80)

for i, (idx, ts, line) in enumerate(dispatches):
    print(f"\n[{i}] {idx}: {ts}")
    print(f"    {line}")

# Use the most recent dispatch
if dispatches:
    idx, ts, line = dispatches[-1]
    print(f"\n" + "="*80)
    print(f"  HTTP REQUEST COUNT FOR {task_id} (most recent dispatch)")
    print("="*80)
    
    print(f"\nDispatch line: {idx}")
    print(f"Line content: {line}")
    
    # Count HTTP requests after dispatch (within 1 hour)
    count = 0
    print(f"\nHTTP 200 OK requests after dispatch (within 1 hour):")
    for i in range(idx + 1, min(idx + 3600, len(lines))):
        if "HTTP Request" in lines[i] and "200 OK" in lines[i]:
            count += 1
            print(f"  {count}: {lines[i].rstrip()}")
    
    print(f"\nTotal HTTP 200 OK requests: {count}")
    
    # Check for task advancement messages
    print(f"\nTask advancement messages after dispatch:")
    advancement_count = 0
    for i in range(idx + 1, min(idx + 3600, len(lines))):
        if "advanced to" in lines[i].lower() and task_id in lines[i]:
            advancement_count += 1
            print(f"  {advancement_count}: {lines[i].rstrip()}")
    
    print(f"\nTotal task advancement messages: {advancement_count}")
    
    # Check for any errors
    print(f"\nHTTP errors after dispatch:")
    error_count = 0
    for i in range(idx + 1, min(idx + 3600, len(lines))):
        if "HTTP" in lines[i] and ("Error" in lines[i] or "error" in lines[i]):
            error_count += 1
            print(f"  {error_count}: {lines[i].rstrip()}")
    
    print(f"\nTotal HTTP errors: {error_count}")
