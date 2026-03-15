"""
Migration 0004: Add llm and budget columns to tasks table.
"""

description = "Add llm JSON and budget TEXT columns to tasks"


def up(conn):
    """Add llm and budget columns."""
    conn.execute("ALTER TABLE tasks ADD COLUMN llm JSON")
    conn.execute("ALTER TABLE tasks ADD COLUMN budget TEXT DEFAULT ''")
    conn.commit()


def down(conn):
    """Remove llm and budget columns using table-rebuild workaround (SQLite)."""
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
            prerequisites JSON,
            project TEXT DEFAULT 'TheMaestro'
        )
    """)
    conn.execute("""
        INSERT INTO tasks_new
            (id, title, type, description, owner, tags, content, history,
             position, created_at, updated_at, prerequisites, project)
        SELECT
            id, title, type, description, owner, tags, content, history,
            position, created_at, updated_at, prerequisites, project
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.commit()
