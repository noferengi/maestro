# DEPRECATED: Uses SQLite direct. Use MCP tools (diagnose_task, get_budget_trace) instead.
import sqlite3
import json
from pathlib import Path
from datetime import datetime

DB_PATH = Path("data/kanban.db")

def get_conn():
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

def main():
    conn = get_conn()
    project = "StoryOrchestrator"
    
    print(f"--- Diagnostic Report for Project: {project} ---")
    
    # 1. Stage Distribution
    stages = conn.execute("""
        SELECT type, COUNT(*) as cnt 
        FROM tasks t 
        JOIN projects p ON t.project_id = p.id 
        WHERE p.name = ? AND t.is_active = 1 
        GROUP BY type
    """, (project,)).fetchall()
    print("\n[Stage Distribution]")
    for s in stages:
        print(f"{s['type']}: {s['cnt']}")

    # 2. Active Sessions
    active = conn.execute("""
        SELECT s.task_id, t.title, t.type, s.agent_type, s.started_at 
        FROM agent_sessions s
        JOIN tasks t ON s.task_id = t.id
        JOIN projects p ON t.project_id = p.id
        WHERE p.name = ? AND s.ended_at IS NULL
    """, (project,)).fetchall()
    print("\n[Active Sessions]")
    for a in active:
        print(f"Task: {a['task_id']} | Stage: {a['type']} | Agent: {a['agent_type']} | Started: {a['started_at']}")

    # 3. Recent Activity (last 10 budget entries for the project)
    recent = conn.execute("""
        SELECT b.task_id, b.agent_name, b.created_at, b.response_data
        FROM budget_entries b
        JOIN tasks t ON b.task_id = t.id
        JOIN projects p ON t.project_id = p.id
        WHERE p.name = ?
        ORDER BY b.id DESC LIMIT 10
    """, (project,)).fetchall()
    print("\n[Recent Project Activity]")
    for r in recent:
        try:
            data = json.loads(r['response_data'] or "{}")
            finish_reason = data.get("choices", [{}])[0].get("finish_reason", "N/A")
        except:
            finish_reason = "Error parsing"
        print(f"{r['created_at']} | Task: {r['task_id']} | Agent: {r['agent_name']} | Finish: {finish_reason}")

    conn.close()

if __name__ == "__main__":
    main()
