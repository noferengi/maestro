"""
Migration 0010 — Add subdivision support for recursive task decomposition.

Adds parent_task_id and subdivision_generation to tasks table,
and creates subdivision_records table for audit trail and retry context.
"""

description = "Add subdivision support (parent_task_id, subdivision_generation, subdivision_records)"


def up(conn):
    # Add columns to tasks table
    conn.execute("ALTER TABLE tasks ADD COLUMN parent_task_id TEXT REFERENCES tasks(id)")
    conn.execute("ALTER TABLE tasks ADD COLUMN subdivision_generation INTEGER NOT NULL DEFAULT 0")

    # Create subdivision_records table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS subdivision_records (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            parent_task_id    TEXT NOT NULL REFERENCES tasks(id),
            attempt_number    INTEGER NOT NULL DEFAULT 1,
            generation        INTEGER NOT NULL DEFAULT 1,
            child_task_ids    JSON NOT NULL,
            rejection_context JSON,
            agent_vote        JSON,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            status            TEXT NOT NULL DEFAULT 'active',
            created_at        DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # Indexes
    conn.execute("CREATE INDEX IF NOT EXISTS idx_tasks_parent_task_id ON tasks(parent_task_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_subdivision_parent ON subdivision_records(parent_task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS subdivision_records")
    # SQLite doesn't support DROP COLUMN before 3.35.0; recreate the table
    # For simplicity, just drop the indexes (columns will be ignored by old code)
    conn.execute("DROP INDEX IF EXISTS idx_tasks_parent_task_id")
    conn.execute("DROP INDEX IF EXISTS idx_subdivision_parent")
    conn.commit()
