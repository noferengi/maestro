"""
Migration 0017 — Add projects table with filesystem path.

Each project managed by Maestro needs its own filesystem root so the agent
can run git operations in the correct repository rather than Maestro's own
source tree.  The projects table maps a project name (which tasks already
reference via the tasks.project string column) to a filesystem path.

Existing task data is left unchanged; the path column is nullable so that
legacy project names that pre-date this migration continue to work until
a path is explicitly configured via the UI or API.
"""

description = "Add projects table with filesystem path"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS projects (
            name TEXT PRIMARY KEY,
            path TEXT,
            description TEXT,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Backfill a row for every distinct project name already in tasks so that
    # the foreign relationship is consistent (path starts as NULL).
    conn.execute("""
        INSERT OR IGNORE INTO projects (name)
        SELECT DISTINCT project FROM tasks WHERE project IS NOT NULL
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS projects")
    conn.commit()
