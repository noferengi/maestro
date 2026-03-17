"""
Migration 0011 — Add Big Idea flag and interface contracts.

Adds is_big_idea to tasks table, and interface_contracts (JSON) to both
tasks and subdivision_records tables.
"""

description = "Add is_big_idea flag and interface_contracts to tasks and subdivision_records"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN is_big_idea INTEGER NOT NULL DEFAULT 0")
    conn.execute("ALTER TABLE tasks ADD COLUMN interface_contracts TEXT")
    conn.execute("ALTER TABLE subdivision_records ADD COLUMN interface_contracts TEXT")
    conn.commit()


def down(conn):
    # SQLite < 3.35 can't DROP COLUMN; these columns will simply be ignored by old code
    conn.commit()
