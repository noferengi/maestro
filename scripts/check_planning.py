#!/usr/bin/env python
"""Check planning pipeline logs."""
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(project_root, "logs", "maestro.log")

with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Find the dispatch of task-1774564423.800108 in the recent log
print("="*80)
print("  PLANNING PIPELINE LOG ANALYSIS")
print("="*80)

# Look for this specific task
task_id = "task-1774564423.800108"
print(f"\nSearching for {task_id} in recent log...")

for i, line in enumerate(lines[-200:]):
    if task_id in line:
        # Print context (5 lines before and after)
        start = max(0, i - 5)
        end = min(len(lines[-200:]), i + 6)
        print(f"\n[{len(lines[-200:])-end+1}]")
        for j in range(start, end):
            marker = ">>>" if j == i else "   "
            print(f"{marker} {lines[-200+j].rstrip()}")

# Also look for any planning-related activity
print("\n" + "="*80)
print("  PLANNING-RELATED LOG ENTRIES")
print("="*80)

planning_lines = [line for line in lines[-500:] if "planning" in line.lower()]
print(f"Found {len(planning_lines)} planning-related entries")

for line in planning_lines[-30:]:
    print(line.rstrip())
