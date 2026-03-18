"""
Migration 0016 — Add demotion tracking columns to tasks table.

Adds review_notes, demotion_count, and demotion_history columns so that
tasks demoted from REVIEW back to DEVELOPMENT retain an audit trail of
why and how many times they were sent back.
"""

description = "Add demotion tracking columns to tasks table"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN review_notes TEXT")
    conn.execute("ALTER TABLE tasks ADD COLUMN demotion_count INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE tasks ADD COLUMN demotion_history JSON")
    conn.commit()


def down(conn):
    # SQLite doesn't support DROP COLUMN in older versions; these columns
    # are nullable / have defaults so they are safe to leave in place.
    pass
