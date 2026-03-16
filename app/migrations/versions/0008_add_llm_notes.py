"""
Migration 0008: Add notes column to llms table.

Free-text field for the user to record model details — exact finetune,
sub-model version, quantisation, etc.
"""

description = "Add notes column to llms table"


def up(conn):
    conn.execute(
        "ALTER TABLE llms ADD COLUMN notes TEXT NOT NULL DEFAULT ''"
    )
    conn.commit()


def down(conn):
    # SQLite < 3.35 can't DROP COLUMN; rebuild table
    conn.execute("""
        CREATE TABLE llms_backup (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            address TEXT NOT NULL DEFAULT 'localhost',
            port INTEGER NOT NULL DEFAULT 8008,
            model TEXT NOT NULL DEFAULT '',
            settings JSON,
            parallel_sessions INTEGER NOT NULL DEFAULT 1,
            max_context INTEGER NOT NULL DEFAULT 4096,
            UNIQUE(address, port, model)
        )
    """)
    conn.execute("""
        INSERT INTO llms_backup (id, address, port, model, settings, parallel_sessions, max_context)
        SELECT id, address, port, model, settings, parallel_sessions, max_context FROM llms
    """)
    conn.execute("DROP TABLE llms")
    conn.execute("ALTER TABLE llms_backup RENAME TO llms")
    conn.commit()
