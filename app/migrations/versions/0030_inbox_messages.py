"""
Migration 0030 — inbox_messages table.

Stores persistent notification messages for the user — intake pipeline results,
agent alerts, and any other events worth reviewing later.
"""

description = "add inbox_messages table"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS inbox_messages (
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
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_inbox_read ON inbox_messages (read)"
    )
    conn.commit()
    print("[0030] Created inbox_messages table.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS inbox_messages")
    conn.commit()
    print("[0030] Dropped inbox_messages table.")
