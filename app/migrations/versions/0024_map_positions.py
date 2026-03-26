"""
Migration 0011 — Add map_x and map_y to tasks for Column Map View position persistence.

Positions are NULL by default. The frontend computes them on first open using a radial
layout algorithm and then saves them here. On subsequent opens the saved coordinates are
used directly. Only newly-subdivided children (which have NULL coordinates) trigger
re-computation and a fresh save.
"""

description = "Add map_x and map_y columns to tasks for Column Map View position persistence"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN map_x REAL")
    conn.execute("ALTER TABLE tasks ADD COLUMN map_y REAL")
    conn.commit()


def down(conn):
    # SQLite supports DROP COLUMN from 3.35.0 onward.
    # Emit it defensively; older SQLite will error here but the migration runner
    # should catch it and leave the columns in place (harmless for reads/writes).
    try:
        conn.execute("ALTER TABLE tasks DROP COLUMN map_x")
        conn.execute("ALTER TABLE tasks DROP COLUMN map_y")
        conn.commit()
    except Exception:
        conn.rollback()
