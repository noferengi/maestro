"""
Migration 0014 — Optimization results table for optimization pipeline audit trail.

Records baseline/post reports, proposal scores, judge evaluations, and the
winning proposal selection for each optimization cycle.
"""

description = "Optimization results table for optimization pipeline audit trail"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimization_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            baseline_report TEXT,
            proposals TEXT,
            judge_scores TEXT,
            winning_proposal_index INTEGER,
            winning_score REAL,
            post_report TEXT,
            improvement_summary TEXT,
            outcome TEXT NOT NULL,
            total_prompt_tokens INTEGER DEFAULT 0,
            total_completion_tokens INTEGER DEFAULT 0,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_optimization_results_task ON optimization_results(task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS optimization_results")
    conn.execute("DROP INDEX IF EXISTS idx_optimization_results_task")
    conn.commit()
