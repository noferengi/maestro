#!/usr/bin/env python
"""Diagnose stuck scheduler tasks - budget entries."""
import os
import sqlite3

# Paths
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(project_root, "data", "kanban.db")

conn = sqlite3.connect(db_path)
cursor = conn.cursor()

print("="*80)
print("  STUCK TASK - BUDGET ANALYSIS")
print("="*80)

# Get the stuck task
task_id = "task-1774564423.800108"

# Get task info
cursor.execute("""
    SELECT id, title, type, updated_at 
    FROM tasks 
    WHERE id = ?
""", (task_id,))

task = cursor.fetchone()
print(f"\nTask: {task[0]}")
print(f"Title: {task[1]}")
print(f"Type: {task[2]}")
print(f"Last Updated: {task[3]}")

# Get budget entries for this task
print(f"\nBudget entries for {task_id}:")
cursor.execute("""
    SELECT id, created_at, prompt_data, response_data 
    FROM budget_entries 
    WHERE task_id = ?
    ORDER BY created_at DESC
    LIMIT 20
""", (task_id,))

entries = cursor.fetchall()
print(f"  Total entries: {len(entries)}")

if entries:
    latest = entries[0]
    print(f"  Latest entry: {latest[1]}")
    print(f"  Latest prompt_data length: {len(latest[2]) if latest[2] else 0}")
    print(f"  Latest response_data length: {len(latest[3]) if latest[3] else 0}")

# Get transition results
print(f"\nTransition results for {task_id}:")
cursor.execute("""
    SELECT id, transition, outcome, created_at, 
           vote_summary
    FROM transition_results 
    WHERE task_id = ?
    ORDER BY created_at DESC
    LIMIT 10
""", (task_id,))

results = cursor.fetchall()
print(f"  Total results: {len(results)}")

if results:
    print("  Most recent:")
    for result in results[:5]:
        print(f"    - Transition: {result[1]}, Outcome: {result[2]}, Created: {result[3]}")
        summary = result[4]
        if summary:
            print(f"      Summary: {summary[:200]}...")

# Get component results (for planning tasks)
print(f"\nComponent results for {task_id}:")
cursor.execute("""
    SELECT id, component_name, status, created_at, 
           prompt_tokens, completion_tokens
    FROM component_results 
    WHERE task_id = ?
    ORDER BY created_at DESC
    LIMIT 20
""", (task_id,))

components = cursor.fetchall()
print(f"  Total components: {len(components)}")

if components:
    print("  Most recent:")
    for comp in components[:10]:
        print(f"    - {comp[1]}: status={comp[2]}, created={comp[3]}, prompt={comp[4]}, completion={comp[5]}")

# Check if there are any errors in component results
print(f"\nComponent results with errors:")
cursor.execute("""
    SELECT id, component_name, error_detail, created_at
    FROM component_results 
    WHERE task_id = ? AND error_detail IS NOT NULL
    ORDER BY created_at DESC
    LIMIT 10
""", (task_id,))

failed = cursor.fetchall()
print(f"  Components with errors: {len(failed)}")
for f in failed[:5]:
    print(f"    - {f[1]}: {f[2][:100]}...")

conn.close()

print("\n" + "="*80)
print("  END OF ANALYSIS")
print("="*80)
