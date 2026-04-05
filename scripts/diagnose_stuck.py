#!/usr/bin/env python
"""Diagnose stuck scheduler tasks."""
import os
import sqlite3

# Paths
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
log_path = os.path.join(project_root, "logs", "maestro.log")
db_path = os.path.join(project_root, "data", "kanban.db")

# Read log
with open(log_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

print("="*80)
print("  SCHEDULER STUCK TASK DIAGNOSIS")
print("="*80)

# 1. Find dispatch events and their corresponding completion events
print("\n[1] Looking for task dispatch/completion pairs...")

dispatch_events = []
completion_events = []

for line in lines:
    if "Dispatching task" in line:
        # Extract task ID
        for tid in ["task-1774564423.800108", "task-1775019392.795339"]:
            if tid in line:
                dispatch_events.append((line.strip(), tid))
                break
    elif "advanced to" in line.lower():
        completion_events.append(line.strip())

print(f"    Dispatch events found: {len(dispatch_events)}")
for event in dispatch_events:
    print(f"      - {event[0][:80]}...")

print(f"    Completion events found: {len(completion_events)}")
for event in completion_events:
    print(f"      - {event}")

# 2. Check task status in database
print("\n[2] Checking task status in database...")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

# Get the two active tasks
cursor.execute("""
    SELECT id, title, type, updated_at 
    FROM tasks 
    WHERE id IN ('task-1774564423.800108', 'task-1775019392.795339')
    ORDER BY id
""")

active_tasks = cursor.fetchall()
print("    Active tasks:")
for task in active_tasks:
    print(f"      ID: {task[0]}")
    print(f"      Title: {task[1]}")
    print(f"      Type: {task[2]}")
    print(f"      Status: {task[3]}")
    print(f"      Created: {task[4]}")
    print(f"      Updated: {task[5]}")

# Check for transition results
print("\n[3] Checking transition results for these tasks...")

cursor.execute("""
    SELECT tr.id, tr.task_id, tr.transition, tr.outcome, tr.completed_at,
           tr.total_prompt_tokens, tr.total_completion_tokens
    FROM transition_results tr
    WHERE tr.task_id IN ('task-1774564423.800108', 'task-1775019392.795339')
    ORDER BY tr.completed_at DESC
    LIMIT 5
""")

recent_results = cursor.fetchall()
print(f"    Recent transition results: {len(recent_results)}")
for result in recent_results:
    print(f"      ID: {result[0]}, Task: {result[1]}, Transition: {result[2]}, Outcome: {result[3]}")
    print(f"        Completed: {result[4]}, Prompt tokens: {result[5]}, Completion tokens: {result[6]}")

# 4. Check for errors in the log
print("\n[4] Checking for errors around dispatch time...")

for line in lines:
    if "13:40:19" in line or "13:40:20" in line or "13:40:21" in line:
        print(f"    {line.strip()}")

# 5. Check budget entries for these tasks
print("\n[5] Checking budget entries for these tasks...")

cursor.execute("""
    SELECT be.id, be.task_id, be.job_id, be.status, be.completed_at,
           be.prompt_tokens, be.completion_tokens
    FROM budget_entries be
    WHERE be.task_id IN ('task-1774564423.800108', 'task-1775019392.795339')
    ORDER BY be.created_at DESC
    LIMIT 10
""")

budget_entries = cursor.fetchall()
print(f"    Budget entries: {len(budget_entries)}")
for entry in budget_entries:
    print(f"      ID: {entry[0]}, Task: {entry[1]}, Job: {entry[2]}, Status: {entry[3]}")
    print(f"        Completed: {entry[4]}, Prompt: {entry[5]}, Completion: {entry[6]}")

conn.close()

print("\n" + "="*80)
print("  DIAGNOSIS COMPLETE")
print("="*80)
