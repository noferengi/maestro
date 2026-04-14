
from datetime import datetime, timezone

def up(conn):
    # Add performance_improvement_plans table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS performance_improvement_plans (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL,
            origin_stage TEXT NOT NULL,
            requirements TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'active',
            verified_at TEXT,
            llm_id INTEGER,
            budget_id INTEGER,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            created_at TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (llm_id) REFERENCES llms(id),
            FOREIGN KEY (budget_id) REFERENCES budgets(id)
        )
    """)
    conn.commit()

def down(conn):
    conn.execute("DROP TABLE IF EXISTS performance_improvement_plans")
    conn.commit()

description = "Add performance_improvement_plans table for task demotion quality gates."
