
import sqlite3
import json
import os

DB_PATH = "data/kanban.db"

def check_stuck_jobs():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    print("--- Tasks with LLM activity in last 4 hours ---")
    cur.execute("""
        SELECT task_id, COUNT(*) as calls, MAX(created_at) as last_call
        FROM budget_entries
        WHERE created_at > datetime('now', '-4 hours')
        GROUP BY task_id
    """)
    for row in cur.fetchall():
        print(f"Task {row['task_id']}: {row['calls']} calls, last at {row['last_call']}")

    print("\n--- File Summary Jobs (Running) ---")
    cur.execute("SELECT id, file_path, status, created_at, llm_id FROM file_summary_jobs WHERE status = 'running'")
    for row in cur.fetchall():
        print(f"Job {row['id']}: {row['file_path']} ({row['status']}) created at {row['created_at']}, LLM {row['llm_id']}")

    print("\n--- Research Jobs (Running) ---")
    cur.execute("SELECT id, task_id, status, created_at, llm_id FROM research_jobs WHERE status = 'running'")
    for row in cur.fetchall():
        print(f"Job {row['id']}: Task {row['task_id']} ({row['status']}) created at {row['created_at']}, LLM {row['llm_id']}")

    print("\n--- Arch Gen Jobs (Running) ---")
    cur.execute("SELECT id, project, category, status, created_at, llm_id FROM arch_gen_jobs WHERE status = 'running'")
    for row in cur.fetchall():
        print(f"Job {row['id']}: {row['project']}/{row['category']} ({row['status']}) created at {row['created_at']}, LLM {row['llm_id']}")

    print("\n--- LLM Session Counts (if possible to infer) ---")
    # This is in-memory in the scheduler, so we can't see it directly from DB 
    # unless we look at what's currently active in the task list and assume it matches.
    
    conn.close()

if __name__ == "__main__":
    check_stuck_jobs()
