"""
0047_intake_exhausted.py
------------------------
Add intake_exhausted_at column to tasks table.

When the intake pipeline rejects a task three or more times the scheduler
marks it as intake-exhausted (sets this column to the ISO timestamp of
exhaustion) and stops auto-retrying.  The human must clear the column via
POST /api/tasks/{id}/reset-intake to allow retries to resume.
"""

description = "Add intake_exhausted_at to tasks"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN intake_exhausted_at TEXT")
    conn.commit()


def down(conn):
    pass  # SQLite: no DROP COLUMN; column presence is non-destructive
