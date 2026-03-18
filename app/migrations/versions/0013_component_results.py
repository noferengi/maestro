"""
Migration 0013 — Component results table for development orchestrator audit trail.

Tracks individual component implementation steps within a task's development
cycle, including batch ordering, test results, and token usage.
"""

description = "Component results table for development orchestrator audit trail"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS component_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            component_name TEXT NOT NULL,
            step_order INTEGER NOT NULL,
            batch_number INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            files_changed TEXT,
            tests_passed INTEGER DEFAULT 0,
            turns_used INTEGER DEFAULT 0,
            error_detail TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_component_results_task ON component_results(task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS component_results")
    conn.execute("DROP INDEX IF EXISTS idx_component_results_task")
    conn.commit()
