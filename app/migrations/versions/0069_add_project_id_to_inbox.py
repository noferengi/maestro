description = "add project_id to inbox_messages"


def up(conn):
    conn.execute("ALTER TABLE inbox_messages ADD COLUMN project_id TEXT")


def down(conn):
    if conn.is_postgres:
        conn.execute("ALTER TABLE inbox_messages DROP COLUMN project_id")
        return

    # SQLite <3.35 has no DROP COLUMN — recreate without project_id.
    conn.execute("""
        CREATE TABLE inbox_messages_old (
            id          TEXT    PRIMARY KEY,
            subject     TEXT    NOT NULL,
            source_type TEXT    NOT NULL DEFAULT 'intake_result',
            task_id     TEXT,
            task_title  TEXT,
            outcome     TEXT,
            data_json   TEXT,
            read        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO inbox_messages_old
            SELECT id, subject, source_type, task_id, task_title, outcome, data_json, read, created_at
            FROM inbox_messages
    """)
    conn.execute("DROP TABLE inbox_messages")
    conn.execute("ALTER TABLE inbox_messages_old RENAME TO inbox_messages")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_inbox_read ON inbox_messages (read)"
    )
    conn.commit()
