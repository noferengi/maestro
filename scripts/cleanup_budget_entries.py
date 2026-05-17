"""
Delete incoherent and orphaned budget_entries rows.

Targeted rows:
  "skipped"  — session_id NOT NULL, prompt_message_count IS NULL
               (sessions where backfill detected context resets or prefix mismatches;
                prompt_data is still the full accumulated history, 1.7 GB total)
  "orphan"   — session_id IS NULL
               (pre-session-tracking entries; prompt_data already nullified by
                backfill_prompt_deltas.py --nullify-orphans; no recoverable content)

Rows preserved:
  All rows where prompt_message_count IS NOT NULL  (properly backfilled delta rows)

Cascades:
  Deleting budget_entries cascades to expenses (ON DELETE CASCADE on budget_entry_id).

Usage:
    python scripts/cleanup_budget_entries.py              # dry-run (default)
    python scripts/cleanup_budget_entries.py --execute    # actually delete
    python scripts/cleanup_budget_entries.py --batch-size 2000 --execute
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database.session import engine


def count_targets(conn):
    skipped = conn.execute(text(
        "SELECT COUNT(*), COALESCE(SUM(LENGTH(prompt_data::text)), 0) "
        "FROM budget_entries "
        "WHERE session_id IS NOT NULL AND prompt_message_count IS NULL"
    )).fetchone()
    orphans = conn.execute(text(
        "SELECT COUNT(*) FROM budget_entries WHERE session_id IS NULL"
    )).fetchone()
    good = conn.execute(text(
        "SELECT COUNT(*) FROM budget_entries WHERE prompt_message_count IS NOT NULL"
    )).fetchone()
    table_size = conn.execute(text(
        "SELECT pg_size_pretty(pg_total_relation_size('budget_entries'))"
    )).scalar()
    return {
        "skipped_count":  skipped[0],
        "skipped_bytes":  skipped[1],
        "orphan_count":   orphans[0],
        "good_count":     good[0],
        "table_size":     table_size,
    }


def delete_in_batches(conn, where: str, batch_size: int) -> int:
    deleted_total = 0
    while True:
        # Collect the batch of IDs first
        ids = conn.execute(text(
            f"SELECT id FROM budget_entries WHERE {where} LIMIT {batch_size}"
        )).fetchall()
        if not ids:
            break
        id_list = [r[0] for r in ids]
        id_csv = ",".join(str(i) for i in id_list)
        # Delete child expenses rows first (FK: expenses.budget_entry_id → budget_entries.id)
        conn.execute(text(f"DELETE FROM expenses WHERE budget_entry_id IN ({id_csv})"))
        conn.execute(text(f"DELETE FROM budget_entries WHERE id IN ({id_csv})"))
        n = len(id_list)
        deleted_total += n
        conn.commit()
        print(f"    ... {deleted_total:,} rows deleted so far", flush=True)
        if n < batch_size:
            break
    return deleted_total


def vacuum(engine):
    raw = engine.raw_connection()
    try:
        raw.set_isolation_level(0)  # AUTOCOMMIT required for VACUUM
        cur = raw.cursor()
        print("  Running VACUUM ANALYZE budget_entries ...")
        cur.execute("VACUUM ANALYZE budget_entries")
        cur.close()
    finally:
        raw.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--execute", action="store_true",
                        help="Actually perform the deletion (default is dry-run)")
    parser.add_argument("--batch-size", type=int, default=5000,
                        help="Rows per DELETE batch (default 5000)")
    args = parser.parse_args()
    dry_run = not args.execute

    with engine.connect() as conn:
        s = count_targets(conn)

    print("=== Budget Entry Cleanup ===\n")
    print(f"  Table size now:    {s['table_size']}")
    print(f"  Clean delta rows:  {s['good_count']:>10,}")
    print(f"  Skipped sessions:  {s['skipped_count']:>10,}  "
          f"({s['skipped_bytes'] / 1024 / 1024:.0f} MiB of prompt_data)")
    print(f"  Orphan rows:       {s['orphan_count']:>10,}  (prompt_data already NULL)")
    print(f"\n  Targeting:         {s['skipped_count'] + s['orphan_count']:,} rows for deletion")

    if dry_run:
        print("\n[DRY RUN] No changes written. Use --execute to apply.")
        return

    print("\nProceeding...\n")
    with engine.connect() as conn:
        print("  Phase 1: deleting skipped sessions (incoherent history)...")
        n_skip = delete_in_batches(
            conn,
            "session_id IS NOT NULL AND prompt_message_count IS NULL",
            args.batch_size,
        )
        print(f"  Phase 1 complete: {n_skip:,} rows deleted\n")

        print("  Phase 2: deleting orphan rows (NULL session_id)...")
        n_orph = delete_in_batches(conn, "session_id IS NULL", args.batch_size)
        print(f"  Phase 2 complete: {n_orph:,} rows deleted\n")

    vacuum(engine)

    with engine.connect() as conn:
        after = count_targets(conn)

    print(f"\n=== Done ===")
    print(f"  Total deleted:    {n_skip + n_orph:,} rows")
    print(f"  Remaining rows:   {after['good_count']:,}")
    print(f"  Table size now:   {after['table_size']}")


if __name__ == "__main__":
    main()
