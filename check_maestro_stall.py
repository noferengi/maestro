import time
from datetime import datetime
import sqlite3
from pathlib import Path

DB_PATH = Path("data/kanban.db")

def check_stall():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    now = time.time()
    
    # Get all projects
    projects = conn.execute("SELECT * FROM projects").fetchall()
    for project in projects:
        project_name = project['name']
        project_id = project['id']
        
        # Get last transition result
        result = conn.execute("""
            SELECT MAX(transition_results.created_at) 
            FROM transition_results 
            JOIN tasks ON transition_results.task_id = tasks.id 
            WHERE tasks.project_id = ? AND tasks.is_active = 1
        """, (project_id,)).fetchone()
        
        last_tr_time = None
        if result and result[0]:
            tr_dt_str = result[0]
            try:
                tr_dt = datetime.fromisoformat(tr_dt_str)
                # Assume DB is UTC
                last_tr_time = tr_dt.timestamp()
            except Exception as e:
                print(f"Error parsing {tr_dt_str}: {e}")
        
        if last_tr_time:
            diff = now - last_tr_time
            print(f"Project: {project_name}")
            print(f"  Now (POSIX): {now}")
            print(f"  Last TR (POSIX): {last_tr_time}")
            print(f"  Diff: {diff} seconds")
            print(f"  Stalled (>300)? {diff > 300}")
        else:
            print(f"Project: {project_name} - No activity found")

if __name__ == "__main__":
    check_stall()
