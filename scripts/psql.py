"""
psql.py — Run PostgreSQL queries using Maestro's config system.

Pulls connection credentials from .env (MAESTRO_DATABASE_URL or
MAESTRO_ADMIN_DATABASE_URL) so no credentials need to be typed manually.

Usage:
    python scripts/psql.py "SELECT count(*) FROM tasks"
    python scripts/psql.py --admin "VACUUM FULL budget_entries"
    python scripts/psql.py --admin -              # read SQL from stdin
    python scripts/psql.py --list-tables          # quick table size overview
    python scripts/psql.py --admin --vacuum-full budget_entries

Connection modes:
    (default)  MAESTRO_DATABASE_URL      — app user, normal queries
    --admin    MAESTRO_ADMIN_DATABASE_URL — admin user, DDL / VACUUM
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2
import psycopg2.extras


# ---------------------------------------------------------------------------
# DSN resolution
# ---------------------------------------------------------------------------

def _load_dsn(admin: bool) -> str:
    import dotenv
    dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    if admin:
        dsn = os.environ.get("MAESTRO_ADMIN_DATABASE_URL")
        if not dsn:
            raise RuntimeError("MAESTRO_ADMIN_DATABASE_URL not set in .env")
        return dsn
    dsn = os.environ.get("MAESTRO_DATABASE_URL")
    if not dsn:
        raise RuntimeError("MAESTRO_DATABASE_URL not set in .env")
    return dsn


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _fmt_bytes(n: int) -> str:
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PiB"


def _print_table(rows: list[tuple], columns: list[str]) -> None:
    if not rows:
        print("(no rows)")
        return
    col_widths = [len(c) for c in columns]
    str_rows = []
    for row in rows:
        str_row = [str(v) if v is not None else "NULL" for v in row]
        str_rows.append(str_row)
        for i, cell in enumerate(str_row):
            col_widths[i] = max(col_widths[i], len(cell))

    sep = "+" + "+".join("-" * (w + 2) for w in col_widths) + "+"
    header = "|" + "|".join(f" {c:<{w}} " for c, w in zip(columns, col_widths)) + "|"
    print(sep)
    print(header)
    print(sep)
    for row in str_rows:
        safe = [c.encode(sys.stdout.encoding or "utf-8", errors="backslashreplace").decode(sys.stdout.encoding or "utf-8") for c in row]
        print("|" + "|".join(f" {cell:<{w}} " for cell, w in zip(safe, col_widths)) + "|")
    print(sep)
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


# ---------------------------------------------------------------------------
# Built-in shortcuts
# ---------------------------------------------------------------------------

LIST_TABLES_SQL = """
SELECT
    schemaname,
    relname AS table_name,
    pg_size_pretty(pg_total_relation_size(relid)) AS total_size,
    pg_size_pretty(pg_relation_size(relid))        AS table_size,
    pg_size_pretty(pg_total_relation_size(relid)
                   - pg_relation_size(relid))      AS indexes_and_toast,
    n_live_tup                                     AS live_rows,
    n_dead_tup                                     AS dead_rows
FROM pg_stat_user_tables
ORDER BY pg_total_relation_size(relid) DESC;
"""

BUDGET_ENTRIES_DETAIL_SQL = """
SELECT
    pg_size_pretty(pg_relation_size('budget_entries'))        AS heap,
    pg_size_pretty(pg_total_relation_size('budget_entries')
                   - pg_relation_size('budget_entries'))      AS toast_plus_indexes,
    pg_size_pretty(pg_total_relation_size('budget_entries'))  AS total,
    (SELECT count(*) FROM budget_entries)                     AS live_rows,
    (SELECT n_dead_tup FROM pg_stat_user_tables
     WHERE relname = 'budget_entries')                        AS dead_rows;
"""


# ---------------------------------------------------------------------------
# Core execution
# ---------------------------------------------------------------------------

def run_sql(sql: str, admin: bool, autocommit: bool = False) -> None:
    dsn = _load_dsn(admin)
    conn = psycopg2.connect(dsn)
    try:
        if autocommit:
            conn.set_isolation_level(0)
        with conn.cursor() as cur:
            cur.execute(sql)
            if cur.description:
                cols = [d[0] for d in cur.description]
                rows = cur.fetchall()
                _print_table(rows, cols)
            else:
                status = cur.statusmessage or "OK"
                print(status)
        if not autocommit:
            conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run PostgreSQL queries using Maestro's .env credentials.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "sql",
        nargs="?",
        help="SQL to execute. Use '-' to read from stdin.",
    )
    parser.add_argument(
        "--admin",
        action="store_true",
        help="Connect as admin (MAESTRO_ADMIN_DATABASE_URL). Required for VACUUM FULL, DDL.",
    )
    parser.add_argument(
        "--list-tables",
        action="store_true",
        help="Show all user tables with size, live/dead row counts.",
    )
    parser.add_argument(
        "--budget-entries",
        action="store_true",
        help="Show detailed size breakdown for budget_entries (heap + TOAST).",
    )
    parser.add_argument(
        "--vacuum-full",
        metavar="TABLE",
        help="Run VACUUM FULL <table> as admin (autocommit, may take a while).",
    )
    parser.add_argument(
        "--autocommit",
        action="store_true",
        help="Run in autocommit mode (required for VACUUM, CREATE DATABASE, etc.).",
    )

    args = parser.parse_args()

    if args.vacuum_full:
        table = args.vacuum_full
        print(f"Running VACUUM FULL {table} (admin, autocommit)...")
        run_sql(f"VACUUM FULL {table}", admin=True, autocommit=True)
        print("Done. Checking new size...")
        run_sql(
            f"SELECT pg_size_pretty(pg_total_relation_size('{table}')) AS total_size,"
            f" (SELECT count(*) FROM {table}) AS live_rows",
            admin=True,
        )
        return

    if args.list_tables:
        run_sql(LIST_TABLES_SQL, admin=args.admin)
        return

    if args.budget_entries:
        run_sql(BUDGET_ENTRIES_DETAIL_SQL, admin=args.admin)
        return

    if args.sql == "-":
        sql = sys.stdin.read().strip()
    elif args.sql:
        sql = args.sql
    else:
        parser.print_help()
        sys.exit(0)

    if not sql:
        print("No SQL provided.", file=sys.stderr)
        sys.exit(1)

    needs_autocommit = args.autocommit or sql.strip().upper().startswith("VACUUM")
    run_sql(sql, admin=args.admin, autocommit=needs_autocommit)


if __name__ == "__main__":
    main()
