"""
Migration 0001: Initial schema
Creates the base tasks table (without prerequisites column).
"""

description = "Create initial tasks table"


def up(conn):
    """Create the tasks table."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tasks (
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
    conn.commit()


def down(conn):
    """Drop the tasks table."""
    conn.execute("DROP TABLE IF EXISTS tasks")
    conn.commit()
