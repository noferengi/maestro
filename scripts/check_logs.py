#!/usr/bin/env python
"""Check maestro log for scheduler activity."""
import os

log_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "logs", "maestro.log")

if not os.path.exists(log_path):
    print(f"Log file not found: {log_path}")
    exit(1)

with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

# Last 50 lines
print("\n" + "="*70)
print("  LAST 50 LINES OF MAESTRO LOG")
print("="*70 + "\n")

for line in lines[-50:]:
    if "scheduler" in line.lower() or "tick" in line.lower() or "thread" in line.lower():
        print(line.rstrip())

# Also show any errors
print("\n" + "="*70)
print("  LAST 50 LINES (ALL)")
print("="*70 + "\n")

for line in lines[-50:]:
    print(line.rstrip())
