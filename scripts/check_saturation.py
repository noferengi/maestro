#!/usr/bin/env python
"""Check for context saturation warnings."""
import os

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(project_root, "logs", "maestro.log")

with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

task_id = "task-1774564423.800108"

print("="*80)
print(f"  CONTEXT SATURATION CHECK FOR {task_id}")
print("="*80)

# Check for saturation warnings
saturation_warnings = []
for i, line in enumerate(lines):
    if "context saturation" in line.lower() and task_id in line:
        saturation_warnings.append((i, line.strip()))

print(f"\nContext saturation warnings: {len(saturation_warnings)}")
for idx, line in saturation_warnings[-10:]:
    print(f"  [{idx}] {line}")

# Check for any warnings related to this task
print(f"\nWarnings for {task_id}:")
warning_count = 0
for i, line in enumerate(lines):
    if task_id in line and ("warning" in line.lower() or "WARNING" in line):
        warning_count += 1
        print(f"  [{i}] {line.rstrip()}")

print(f"\nTotal warnings: {warning_count}")

# Check for loop-related messages
print(f"\nLoop-related messages:")
loop_count = 0
for i, line in enumerate(lines[-5000:]):
    if task_id in line and ("loop" in line.lower() or "turn" in line.lower()):
        loop_count += 1
        if loop_count <= 20:
            print(f"  [{i}] {line.rstrip()}")

print(f"\nTotal loop messages: {loop_count}")
