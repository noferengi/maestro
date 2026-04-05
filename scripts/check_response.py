#!/usr/bin/env python
"""Check LLM response content."""
import os
import json

project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_path = os.path.join(project_root, "data", "kanban.db")

conn = os.path.join(project_root, "venv", "Scripts", "python.exe")

import sqlite3
conn = sqlite3.connect(db_path)
cursor = conn.cursor()

task_id = "task-1774564423.800108"

print("="*80)
print(f"  LLM RESPONSE CONTENT FOR {task_id}")
print("="*80)

# Get budget entries (which contain prompt_data and response_data)
cursor.execute("""
    SELECT id, created_at, prompt_data, response_data 
    FROM budget_entries 
    WHERE task_id = ?
    ORDER BY created_at DESC
    LIMIT 20
""", (task_id,))

entries = cursor.fetchall()

print(f"\nLatest 20 budget entries:")
for entry in entries:
    entry_id, created_at, prompt_data, response_data = entry
    print(f"\n[{entry_id}] {created_at}")
    print(f"  Prompt length: {len(prompt_data) if prompt_data else 0}")
    print(f"  Response length: {len(response_data) if response_data else 0}")
    
    # Try to parse JSON
    if response_data:
        try:
            response = json.loads(response_data)
            if isinstance(response, dict):
                # Extract content from choices
                choices = response.get("choices", [])
                if choices:
                    choice = choices[0]
                    message = choice.get("message", {})
                    content = message.get("content", "")
                    print(f"  Response content preview: {content[:200]}...")
        except json.JSONDecodeError:
            print(f"  Response (raw): {response_data[:200]}...")

conn.close()
