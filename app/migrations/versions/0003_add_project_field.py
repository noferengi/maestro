"""
Migration 0003: Add project column to tasks table.
"""

description = "Add project TEXT column to tasks (default 'TheMaestro')"


def up(conn):
    """Add project column."""
    conn.execute("ALTER TABLE tasks ADD COLUMN project TEXT DEFAULT 'TheMaestro'")
    conn.commit()


def down(conn):
    """Remove project column using table-rebuild workaround (SQLite).

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
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            prerequisites JSON
        )
    """)
    conn.execute("""
        INSERT INTO tasks_new
            (id, title, type, description, owner, tags, content, history,
             position, created_at, updated_at, prerequisites)
        SELECT
            id, title, type, description, owner, tags, content, history,
            position, created_at, updated_at, prerequisites
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.commit()
