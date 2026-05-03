"""
Migration 0044 — add numeric integer PK to projects and project_id FK to tasks.

SQLite cannot alter primary keys in-place, so we rebuild the projects table with a
proper INTEGER PRIMARY KEY AUTOINCREMENT.  tasks gains a project_id INTEGER column
(populated from the existing tasks.project text column via a join).

Migration 0045 (future) will drop tasks.project once all Python code uses project_id.
"""

description = "Add numeric integer PK to projects; add project_id FK to tasks"


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())


def _is_integer_pk(cur, table: str) -> bool:
    """Return True if the table already has an INTEGER PRIMARY KEY (rowid alias)."""
    cur.execute(f"PRAGMA table_info({table})")
    for row in cur.fetchall():
        if row["pk"] == 1 and "INTEGER" in (row["type"] or "").upper():
            return True
    return False


def up(conn):
    cur = conn.cursor()

    # --- Step 1: rebuild projects with id as the real INTEGER PRIMARY KEY ---
    # Must happen BEFORE adding the FK on tasks, because SQLite requires the
    # referenced column to be an explicit PK or UNIQUE index.
    if not _is_integer_pk(cur, "projects"):
        # Add temporary id column so we can preserve existing rowid values.
        if not _has_column(cur, "projects", "id"):
            cur.execute("ALTER TABLE projects ADD COLUMN id INTEGER")
            cur.execute("UPDATE projects SET id = rowid")

        cur.execute(
            """
            CREATE TABLE projects_new (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL UNIQUE,
                path        TEXT,
                description TEXT,
                llm_id      INTEGER REFERENCES llms(id),
                budget_id   INTEGER REFERENCES budgets(id),
                created_at  TEXT NOT NULL DEFAULT (datetime('now'))
            )
            """
        )
        cur.execute(
            """
            INSERT INTO projects_new (id, name, path, description, llm_id, budget_id, created_at)
            SELECT id, name, path, description, llm_id, budget_id, created_at
            FROM projects
            """
        )
        cur.execute("DROP TABLE projects")
        cur.execute("ALTER TABLE projects_new RENAME TO projects")

    # --- Step 2: add project_id FK to tasks (now references a proper PK) ---
    if not _has_column(cur, "tasks", "project_id"):
        cur.execute(
            "ALTER TABLE tasks ADD COLUMN project_id INTEGER REFERENCES projects(id)"
        )
    # Populate any NULL project_id rows (safe to run repeatedly)
    cur.execute(
        """
        UPDATE tasks SET project_id = (
            SELECT id FROM projects WHERE projects.name = tasks.project
        )
        WHERE project_id IS NULL
        """
    )

    conn.commit()


def down(conn):
    cur = conn.cursor()

    # Restore original projects table (text PK, no id column)
    cur.execute(
        """
        CREATE TABLE projects_old (
            name        TEXT PRIMARY KEY,
            path        TEXT,
            description TEXT,
            llm_id      INTEGER REFERENCES llms(id),
            budget_id   INTEGER REFERENCES budgets(id),
            created_at  TEXT NOT NULL DEFAULT (datetime('now'))
        )
        """
    )
    cur.execute(
        """
        INSERT INTO projects_old (name, path, description, llm_id, budget_id, created_at)
        SELECT name, path, description, llm_id, budget_id, created_at
        FROM projects
        """
    )
    cur.execute("DROP TABLE projects")
    cur.execute("ALTER TABLE projects_old RENAME TO projects")

    # SQLite cannot drop columns pre-3.35 — leave tasks.project_id in place on rollback.
    conn.commit()
