"""
Migration 0002: Add prerequisites column to tasks table.
"""

description = "Add prerequisites JSON column to tasks"


def up(conn):
    """Add prerequisites column."""
    conn.execute("ALTER TABLE tasks ADD COLUMN prerequisites JSON")
    conn.commit()


def down(conn):
    """Remove prerequisites column using table-rebuild workaround (SQLite).

    SQLite does not support DROP COLUMN in older versions.  We recreate the
    table without the column instead.
    """
    conn.execute("""
        CREATE TABLE tasks_new (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL,
            description TEXT,
            owner TEXT DEFAULT 'user',
            tags JSON,
            content JSON,
            history JSON,
            position INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        INSERT INTO tasks_new
            (id, title, type, description, owner, tags, content, history,
             position, created_at, updated_at)
        SELECT
            id, title, type, description, owner, tags, content, history,
            position, created_at, updated_at
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.commit()
