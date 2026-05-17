"""
Backfill prompt_data from full-history to delta format for budget_entries.

Each session's entries currently store the full accumulated message list.
This script converts them to store only the delta (new messages since the
prior turn) and sets prompt_message_count to the cumulative count.

Usage:
    python scripts/backfill_prompt_deltas.py --dry-run
    python scripts/backfill_prompt_deltas.py --batch-size 100
    python scripts/backfill_prompt_deltas.py --session <uuid>
    python scripts/backfill_prompt_deltas.py --nullify-orphans
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from app.database.session import engine


def process_session(conn, session_id: str, dry_run: bool) -> dict:
    rows = conn.execute(
        text(
            "SELECT id, prompt_data, prompt_message_count "
            "FROM budget_entries "
            "WHERE session_id = :sid "
            "ORDER BY id ASC"
        ),
        {"sid": session_id},
    ).fetchall()

    if not rows:
        return {"status": "empty", "session_id": session_id}

    if all(r[2] is not None for r in rows):
        return {"status": "already_done", "session_id": session_id}

    entry_ids = [r[0] for r in rows]
    raw_data = [r[1] for r in rows]

    msg_lists = []
    for i, pd in enumerate(raw_data):
        if pd is None:
            return {
                "status": "skip",
                "session_id": session_id,
                "reason": f"NULL prompt_data at entry id={entry_ids[i]}",
            }
        try:
            decoded = json.loads(pd)
        except json.JSONDecodeError as e:
            return {
                "status": "skip",
                "session_id": session_id,
                "reason": f"JSON decode error at entry id={entry_ids[i]}: {e}",
            }
        msg_lists.append(decoded)

    # Anomaly checks: monotonic growth and prefix consistency
    zero_deltas = []
    for i in range(1, len(msg_lists)):
        cur_len = len(msg_lists[i])
        prev_len = len(msg_lists[i - 1])
        if cur_len < prev_len:
            return {
                "status": "skip",
                "session_id": session_id,
                "reason": f"Context reset at turn {i}: len {cur_len} < prev {prev_len}",
            }
        if prev_len > 0 and msg_lists[i][:prev_len] != msg_lists[i - 1]:
            return {
                "status": "skip",
                "session_id": session_id,
                "reason": f"Corrupted history at turn {i}: prefix mismatch",
            }
        if cur_len == prev_len:
            zero_deltas.append(i)

    # Compute deltas
    deltas = [msg_lists[0]]
    for i in range(1, len(msg_lists)):
        deltas.append(msg_lists[i][len(msg_lists[i - 1]):])

    # Verify round-trip
    accumulated: list = []
    for d in deltas:
        accumulated.extend(d)
    if accumulated != msg_lists[-1]:
        return {
            "status": "skip",
            "session_id": session_id,
            "reason": "Accumulated deltas do not equal final message list",
        }

    full_size = sum(len(json.dumps(m, ensure_ascii=False)) for m in msg_lists)
    delta_size = sum(len(json.dumps(d, ensure_ascii=False)) for d in deltas)

    if not dry_run:
        for i in range(len(rows) - 1, -1, -1):
            conn.execute(
                text(
                    "UPDATE budget_entries "
                    "SET prompt_data = :pd, prompt_message_count = :pmc "
                    "WHERE id = :eid"
                ),
                {
                    "pd": json.dumps(deltas[i], ensure_ascii=False),
                    "pmc": len(msg_lists[i]),
                    "eid": entry_ids[i],
                },
            )

    return {
        "status": "ok",
        "session_id": session_id,
        "turns": len(rows),
        "full_size_bytes": full_size,
        "delta_size_bytes": delta_size,
        "zero_deltas": zero_deltas,
    }


def get_pending_sessions(conn, batch_size: int):
    rows = conn.execute(
        text(
            "SELECT DISTINCT session_id FROM budget_entries "
            "WHERE session_id IS NOT NULL "
            "  AND prompt_message_count IS NULL "
            "LIMIT :lim"
        ),
        {"lim": batch_size},
    ).fetchall()
    return [r[0] for r in rows]


def count_orphans(conn):
    row = conn.execute(
        text(
            "SELECT COUNT(*), COALESCE(SUM(LENGTH(prompt_data)), 0) "
            "FROM budget_entries "
            "WHERE session_id IS NULL AND prompt_message_count IS NULL"
        )
    ).fetchone()
    return row[0], row[1]


def print_result(result: dict):
    sid = result["session_id"][:8] + "..."
    status = result["status"]
    if status == "ok":
        turns = result["turns"]
        full_kb = result["full_size_bytes"] / 1024
        delta_kb = result["delta_size_bytes"] / 1024
        ratio = full_kb / max(0.001, delta_kb)
        zd = len(result["zero_deltas"])
        zd_note = f"  [{zd} zero-delta]" if zd else ""
        print(f"  {sid}  {turns:4d} turns  {full_kb:8.1f} kB -> {delta_kb:7.1f} kB  "
              f"({ratio:5.1f}x){zd_note}")
    elif status == "skip":
        print(f"  {sid}  SKIP: {result['reason']}")
    # "already_done" and "empty" are silent


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--session", type=str, default=None)
    parser.add_argument("--nullify-orphans", action="store_true")
    args = parser.parse_args()

    total_ok = total_skip = total_already = 0
    total_full = total_delta = 0

    def run_session(conn, sid):
        nonlocal total_ok, total_skip, total_already, total_full, total_delta
        result = process_session(conn, sid, args.dry_run)
        print_result(result)
        if result["status"] == "ok":
            total_ok += 1
            total_full += result["full_size_bytes"]
            total_delta += result["delta_size_bytes"]
        elif result["status"] == "skip":
            total_skip += 1
        elif result["status"] == "already_done":
            total_already += 1

    with engine.connect() as conn:
        if args.session:
            run_session(conn, args.session)
            if not args.dry_run:
                conn.commit()
        else:
            while True:
                # Always fetch at OFFSET 0: committed rows disappear from
                # prompt_message_count IS NULL, so the window shifts naturally.
                # Incrementing offset would skip every other batch.
                batch = get_pending_sessions(conn, args.batch_size)
                if not batch:
                    break
                for sid in batch:
                    run_session(conn, sid)
                if not args.dry_run:
                    conn.commit()

        if args.nullify_orphans:
            orphan_count, orphan_bytes = count_orphans(conn)
            if orphan_count == 0:
                print("\nNo NULL-session orphans found.")
            else:
                print(f"\nNullifying {orphan_count} NULL-session orphans "
                      f"({orphan_bytes / 1024 / 1024:.1f} MiB) ...")
                if not args.dry_run:
                    conn.execute(
                        text(
                            "UPDATE budget_entries "
                            "SET prompt_data = NULL, prompt_message_count = NULL "
                            "WHERE session_id IS NULL AND prompt_message_count IS NULL"
                        )
                    )
                    conn.commit()
                    print("  Done.")
                else:
                    print("  (dry-run -- no changes written)")

        orphan_count, orphan_bytes = count_orphans(conn)

    print("\n=== Summary ===")
    if args.dry_run:
        print("  (DRY RUN -- no writes)")
    print(f"  Sessions converted:   {total_ok}")
    print(f"  Sessions skipped:     {total_skip}")
    print(f"  Already backfilled:   {total_already}")
    if total_full > 0:
        ratio = total_full / max(1, total_delta)
        print(f"  Full size:   {total_full / 1024 / 1024:.1f} MiB")
        print(f"  Delta size:  {total_delta / 1024 / 1024:.1f} MiB")
        print(f"  Reduction:   {ratio:.1f}x")
    if orphan_count > 0:
        print(f"  NULL-session orphans: {orphan_count} entries "
              f"({orphan_bytes / 1024 / 1024:.1f} MiB) -- use --nullify-orphans to free")


if __name__ == "__main__":
    main()
