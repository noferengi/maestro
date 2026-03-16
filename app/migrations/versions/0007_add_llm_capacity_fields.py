"""
Migration 0007: Add parallel_sessions and max_context columns to llms table.

These fields track how many concurrent sessions an LLM endpoint can handle
and the maximum context window size (in tokens) for generation requests.
"""

description = "Add parallel_sessions and max_context to llms table"


def up(conn):
    conn.execute(
        "ALTER TABLE llms ADD COLUMN parallel_sessions INTEGER NOT NULL DEFAULT 1"
    )
    conn.execute(
        "ALTER TABLE llms ADD COLUMN max_context INTEGER NOT NULL DEFAULT 4096"
    )
    conn.commit()


def down(conn):
    # SQLite doesn't support DROP COLUMN before 3.35.0; rebuild table
    conn.execute("""
        CREATE TABLE llms_backup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL DEFAULT 'localhost',
            port INTEGER NOT NULL DEFAULT 8008,
            model TEXT NOT NULL DEFAULT '',
            settings JSON,
            UNIQUE(address, port, model)
        )
    """)
    conn.execute("""
        INSERT INTO llms_backup (id, address, port, model, settings)
        SELECT id, address, port, model, settings FROM llms
    """)
    conn.execute("DROP TABLE llms")
    conn.execute("ALTER TABLE llms_backup RENAME TO llms")
    conn.commit()
