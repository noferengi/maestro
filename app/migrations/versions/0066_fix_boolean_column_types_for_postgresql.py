"""
Migration 0066 — fix boolean column types for PostgreSQL.

SQLite has no dedicated boolean type; the original migrations used INTEGER
(0/1) for boolean columns.  After the SQLite→PostgreSQL migration those
columns are still stored as int4, but the SQLAlchemy models declare them as
Boolean.  PostgreSQL rejects comparisons like `col = true` when the column is
int4, so every query that filters on a boolean field fails.

Fix: three-step ALTER per column (required by PostgreSQL when a column default
exists and its type is changing):
  1. DROP DEFAULT  — removes the integer default (e.g. DEFAULT 1) that cannot
                     be cast automatically to boolean
  2. ALTER COLUMN TYPE BOOLEAN USING col::boolean  — converts 0→false, 1→true
  3. SET DEFAULT <bool>  — restores the semantically correct boolean default

This migration is a no-op on SQLite (which accepts integer 0/1 as boolean
natively and has no ALTER COLUMN TYPE syntax).

Affected columns and their canonical boolean defaults:
  tasks.is_active             true   (1 = active)
  tasks.is_big_idea           false  (0 = ordinary task)
  tasks.is_starred            false  (0 = not starred)
  inbox_messages.read         false  (0 = unread)
  planning_results.was_gate_passed  false  (0 = gate not yet passed)
  project_decisions.is_binding      true   (1 = binding)
"""

description = "fix boolean column types for postgresql"

# (table, column, boolean_default)
_CONVERSIONS = [
    ("tasks",             "is_active",        True),
    ("tasks",             "is_big_idea",       False),
    ("tasks",             "is_starred",        False),
    ("inbox_messages",    "read",              False),
    ("planning_results",  "was_gate_passed",   False),
    ("project_decisions", "is_binding",        True),
]


def _column_type(conn, table, column):
    """Return the pg_type.typname for a column, or None if it doesn't exist."""
    conn.execute(
        """
        SELECT t.typname
        FROM pg_attribute a
        JOIN pg_class c ON c.oid = a.attrelid
        JOIN pg_type  t ON t.oid = a.atttypid
        WHERE c.relname = :table
          AND a.attname = :column
          AND a.attnum  > 0
          AND NOT a.attisdropped
        """,
        {"table": table, "column": column},
    )
    row = conn.fetchone()
    return row[0] if row else None


def up(conn):
    if not conn.is_postgres:
        print("[0066] SQLite detected — no column type changes needed.")
        return

    for table, column, bool_default in _CONVERSIONS:
        typname = _column_type(conn, table, column)
        if typname is None:
            print(f"[0066] {table}.{column} — column not found, skipping.")
            continue
        if typname == "bool":
            print(f"[0066] {table}.{column} — already BOOLEAN, skipping.")
            continue

        default_sql = "true" if bool_default else "false"

        # Step 1: remove the integer default so PostgreSQL can change the type
        conn.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
        # Step 2: convert stored 0/1 integers to false/true
        conn.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE BOOLEAN'
            f' USING "{column}"::boolean'
        )
        # Step 3: restore a semantically correct boolean default
        conn.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {default_sql}'
        )
        print(f"[0066] {table}.{column}: int4 -> boolean  (default {default_sql})")


def down(conn):
    if not conn.is_postgres:
        return

    for table, column, bool_default in _CONVERSIONS:
        typname = _column_type(conn, table, column)
        if typname is None or typname != "bool":
            continue

        int_default = 1 if bool_default else 0

        conn.execute(f'ALTER TABLE "{table}" ALTER COLUMN "{column}" DROP DEFAULT')
        conn.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" TYPE INTEGER'
            f' USING "{column}"::integer'
        )
        conn.execute(
            f'ALTER TABLE "{table}" ALTER COLUMN "{column}" SET DEFAULT {int_default}'
        )
        print(f"[0066] {table}.{column}: boolean -> int4  (default {int_default}, rollback)")
