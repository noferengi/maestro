"""
Migration 0045 — drop text-based project FK columns.

After migration 0044 populated tasks.project_id (INTEGER → projects.id), we now:
  1. Add project_id INTEGER FK to arch_gen_jobs and populate it.
  2. Drop the legacy 'project' TEXT column from both tables using
     ALTER TABLE ... DROP COLUMN (requires SQLite >= 3.35; we have 3.50).

No table is rebuilt — only the redundant text columns are removed.
"""

description = "Drop legacy TEXT project FK from tasks and arch_gen_jobs"


def _has_column(cur, table: str, column: str) -> bool:
    cur.execute(f"PRAGMA table_info({table})")
    return any(row["name"] == column for row in cur.fetchall())


def up(conn):
    cur = conn.cursor()

    # -------------------------------------------------------------------------
    # Step 1 — arch_gen_jobs: add project_id FK, populate from project name
    # -------------------------------------------------------------------------
    if not _has_column(cur, "arch_gen_jobs", "project_id"):
        cur.execute(
            "ALTER TABLE arch_gen_jobs ADD COLUMN project_id INTEGER REFERENCES projects(id)"
        )
    cur.execute(
        """
        UPDATE arch_gen_jobs
           SET project_id = (SELECT id FROM projects WHERE projects.name = arch_gen_jobs.project)
         WHERE project_id IS NULL
        """
    )

    # -------------------------------------------------------------------------
    # Step 2 — drop the legacy TEXT columns
    # -------------------------------------------------------------------------
    if _has_column(cur, "tasks", "project"):
        cur.execute("ALTER TABLE tasks DROP COLUMN project")

    if _has_column(cur, "arch_gen_jobs", "project"):
        cur.execute("ALTER TABLE arch_gen_jobs DROP COLUMN project")

    conn.commit()


def down(conn):
    cur = conn.cursor()

    # Restore 'project' TEXT columns from the numeric FK via name lookup
    if not _has_column(cur, "tasks", "project"):
        cur.execute("ALTER TABLE tasks ADD COLUMN project TEXT DEFAULT 'TheMaestro'")
        cur.execute(
            """
            UPDATE tasks
               SET project = (SELECT name FROM projects WHERE projects.id = tasks.project_id)
             WHERE project IS NULL
            """
        )

    if not _has_column(cur, "arch_gen_jobs", "project"):
        cur.execute("ALTER TABLE arch_gen_jobs ADD COLUMN project TEXT")
        cur.execute(
            """
            UPDATE arch_gen_jobs
               SET project = (SELECT name FROM projects WHERE projects.id = arch_gen_jobs.project_id)
             WHERE project IS NULL
            """
        )

    conn.commit()
