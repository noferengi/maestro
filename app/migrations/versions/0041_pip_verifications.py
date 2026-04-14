
from datetime import datetime, timezone


def up(conn):
    # Verification audit trail — one row per (pip, stage, run)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pip_verifications (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            pip_id            INTEGER NOT NULL,
            task_id           TEXT    NOT NULL,
            checked_at_stage  TEXT    NOT NULL,
            outcome           TEXT    NOT NULL,
            summary           TEXT,
            findings          TEXT,
            agent_session_id  TEXT,
            created_at        TEXT    NOT NULL,
            FOREIGN KEY (pip_id)  REFERENCES performance_improvement_plans(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pip_verifications_pip_stage
        ON pip_verifications(pip_id, checked_at_stage, created_at DESC)
    """)
    # Record git commit at which the PIP was created — enables diff context in
    # pre-flight prompts.  'none' when the project has no commits yet.
    conn.execute("""
        ALTER TABLE performance_improvement_plans
        ADD COLUMN created_at_commit TEXT NOT NULL DEFAULT 'none'
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS pip_verifications")
    conn.execute("DROP INDEX IF EXISTS idx_pip_verifications_pip_stage")
    # SQLite does not support DROP COLUMN; leave created_at_commit in place.
    conn.commit()


description = "Add pip_verifications table and created_at_commit column on performance_improvement_plans"
