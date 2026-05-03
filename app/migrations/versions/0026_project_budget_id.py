description = "Add budget_id column to projects table"


def up(conn):
    conn.execute(
        "ALTER TABLE projects ADD COLUMN budget_id INTEGER REFERENCES budgets(id)"
    )
    conn.commit()


def down(conn):
    # SQLite ALTER TABLE does not support DROP COLUMN before 3.35.
    # Recreate the table without the column.
    conn.execute("""
        CREATE TABLE projects_new (
            name        TEXT PRIMARY KEY,
            path        TEXT,
            description TEXT,
            llm_id      INTEGER,
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("""
        INSERT INTO projects_new (name, path, description, llm_id, created_at)
        SELECT name, path, description, llm_id, created_at FROM projects
    """)
    conn.execute("DROP TABLE projects")
    conn.execute("ALTER TABLE projects_new RENAME TO projects")
    conn.commit()
