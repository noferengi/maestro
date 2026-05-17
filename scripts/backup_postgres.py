"""
backup_postgres.py — Full logical backup of the Maestro PostgreSQL database.

Uses psycopg2 COPY TO STDOUT (no pg_dump binary required).
Each table is dumped to a gzip-compressed CSV file.
A manifest records row counts and file sizes for verification.

Output directory: backups/YYYY-MM-DD_HHMMSS/
  <table>.csv.gz  — one file per table
  MANIFEST.txt    — row counts, sizes, and restore notes

Restore procedure:
  1. Recreate schema:   python app/migrations/runner.py migrate
  2. For each table:    COPY <table> FROM STDIN (FORMAT csv, HEADER true)
     or run:           python scripts/backup_postgres.py --restore <backup-dir>

Usage:
    python scripts/backup_postgres.py                     # backup to backups/<timestamp>/
    python scripts/backup_postgres.py --out backups/pre-cleanup
    python scripts/backup_postgres.py --dry-run           # list tables + row counts only
    python scripts/backup_postgres.py --restore backups/2026-05-16_120000
"""

import argparse
import csv
import gzip
import io
import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import psycopg2

SEP = "=" * 72


def get_dsn() -> str:
    """Read the admin DSN from environment / .env (needs DDL access for info queries)."""
    import dotenv
    dotenv.load_dotenv(os.path.join(os.path.dirname(__file__), "..", ".env"))
    # Prefer admin URL for broadest read access
    dsn = os.environ.get("MAESTRO_ADMIN_DATABASE_URL") or os.environ.get("MAESTRO_DATABASE_URL")
    if not dsn:
        raise RuntimeError("No DATABASE_URL found in .env")
    return dsn


def get_tables(cur) -> list[str]:
    """Return all user tables in public schema, ordered for FK safety on restore."""
    cur.execute("""
        SELECT table_name
        FROM information_schema.tables
        WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
        ORDER BY table_name
    """)
    return [row[0] for row in cur.fetchall()]


def count_rows(cur, table: str) -> int:
    cur.execute(f"SELECT COUNT(*) FROM {table}")
    return cur.fetchone()[0]


def dump_table(conn, table: str, out_path: str) -> dict:
    """COPY table to gzipped CSV. Returns stats dict."""
    t0 = time.time()
    buf = io.BytesIO()
    with conn.cursor() as cur:
        with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
            cur.copy_expert(
                f"COPY {table} TO STDOUT (FORMAT csv, HEADER true)",
                gz,
            )
    raw_bytes = buf.tell()
    with open(out_path, "wb") as f:
        f.write(buf.getvalue())
    elapsed = time.time() - t0
    return {"size_bytes": raw_bytes, "elapsed_s": elapsed}


def restore_table(conn, table: str, csv_gz_path: str) -> int:
    """COPY gzipped CSV back into table. Returns rows copied."""
    with conn.cursor() as cur:
        with gzip.open(csv_gz_path, "rb") as gz:
            cur.copy_expert(
                f"COPY {table} FROM STDIN (FORMAT csv, HEADER true)",
                gz,
            )
    conn.commit()
    return count_rows(conn.cursor(), table)


def main():
    parser = argparse.ArgumentParser(description="Backup/restore Maestro PostgreSQL data")
    parser.add_argument("--out",      default=None, help="Output directory (default: backups/<timestamp>)")
    parser.add_argument("--dry-run",  action="store_true", help="List tables + row counts without writing files")
    parser.add_argument("--restore",  default=None, metavar="BACKUP_DIR",
                                      help="Restore from a backup directory")
    parser.add_argument("--table",    default=None, help="Backup/restore a single table only")
    args = parser.parse_args()

    dsn = get_dsn()
    conn = psycopg2.connect(dsn)
    conn.autocommit = False

    with conn.cursor() as cur:
        tables = get_tables(cur)

    if args.table:
        tables = [t for t in tables if t == args.table]
        if not tables:
            print(f"Table '{args.table}' not found in public schema.")
            sys.exit(1)

    # ── RESTORE ──────────────────────────────────────────────────────────────
    if args.restore:
        backup_dir = args.restore
        if not os.path.isdir(backup_dir):
            print(f"Backup directory not found: {backup_dir}")
            sys.exit(1)
        print(f"\n{SEP}")
        print(f"  RESTORE from {backup_dir}")
        print(SEP)
        for table in tables:
            csv_gz = os.path.join(backup_dir, f"{table}.csv.gz")
            if not os.path.exists(csv_gz):
                print(f"  SKIP {table:<40} (no file)")
                continue
            print(f"  {table:<40} ...", end="", flush=True)
            n = restore_table(conn, table, csv_gz)
            print(f" {n:,} rows")
        print("\nRestore complete.")
        conn.close()
        return

    # ── DRY RUN ──────────────────────────────────────────────────────────────
    if args.dry_run:
        print(f"\n{SEP}")
        print(f"  DRY RUN — table listing and row counts")
        print(SEP)
        total = 0
        with conn.cursor() as cur:
            for table in tables:
                n = count_rows(cur, table)
                total += n
                print(f"  {table:<45} {n:>12,} rows")
        print(f"\n  Total rows across {len(tables)} tables: {total:,}")
        conn.close()
        return

    # ── BACKUP ───────────────────────────────────────────────────────────────
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    out_dir = args.out or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "backups",
        ts,
    )
    os.makedirs(out_dir, exist_ok=True)

    print(f"\n{SEP}")
    print(f"  BACKUP — {ts}")
    print(f"  Output: {out_dir}")
    print(SEP)

    manifest_lines = [
        f"Backup timestamp: {ts}",
        f"DSN host: {dsn.split('@')[-1]}",
        f"Tables: {len(tables)}",
        "",
        f"{'Table':<45} {'Rows':>10}  {'Compressed':>12}  {'Time':>6}",
        "-" * 80,
    ]

    total_rows = 0
    total_bytes = 0
    with conn.cursor() as cur:
        for table in tables:
            n = count_rows(cur, table)
            total_rows += n
            out_path = os.path.join(out_dir, f"{table}.csv.gz")
            print(f"  {table:<45} {n:>10,} rows ... ", end="", flush=True)
            stats = dump_table(conn, table, out_path)
            total_bytes += stats["size_bytes"]
            kb = stats["size_bytes"] / 1024
            print(f"{kb:>8.0f} kB  ({stats['elapsed_s']:.1f}s)")
            manifest_lines.append(
                f"{table:<45} {n:>10,}  {kb:>10.0f} kB  {stats['elapsed_s']:>5.1f}s"
            )

    manifest_lines += [
        "-" * 80,
        f"{'TOTAL':<45} {total_rows:>10,}  {total_bytes / 1024 / 1024:>9.1f} MB",
        "",
        "Restore notes:",
        "  1. Recreate schema:  python app/migrations/runner.py migrate",
        "  2. Run restore:      python scripts/backup_postgres.py --restore <this-dir>",
        "  Warning: restore truncates existing data — run only on an empty/fresh DB.",
    ]

    manifest_path = os.path.join(out_dir, "MANIFEST.txt")
    with open(manifest_path, "w", encoding="utf-8") as f:
        f.write("\n".join(manifest_lines) + "\n")

    print(f"\n  Total: {total_rows:,} rows  |  {total_bytes / 1024 / 1024:.1f} MB compressed")
    print(f"  Manifest: {manifest_path}")
    print("\nBackup complete.")
    conn.close()


if __name__ == "__main__":
    main()
