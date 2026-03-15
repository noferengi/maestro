"""
Migration 0006: Add transition_votes and transition_results tables for intake pipeline.
"""

description = "Add transition_votes and transition_results tables for intake pipeline"


def up(conn):
    conn.execute("""
        CREATE TABLE transition_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            transition TEXT NOT NULL,
            stage TEXT NOT NULL,
            verdict TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            justification TEXT,
            raw_response JSON,
            prompt_tokens INTEGER,
            completion_tokens INTEGER,
            model TEXT,
            budget_id INTEGER REFERENCES budgets(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.execute("""
        CREATE TABLE transition_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            transition TEXT NOT NULL,
            outcome TEXT NOT NULL,
            vote_summary JSON,
            total_prompt_tokens INTEGER,
            total_completion_tokens INTEGER,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS transition_results")
    conn.execute("DROP TABLE IF EXISTS transition_votes")
    conn.commit()
