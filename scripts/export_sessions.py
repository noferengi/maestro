"""
Export clean budget_entry sessions to JSONL for LLM training / fine-tuning.

Only exports sessions where all entries have prompt_message_count IS NOT NULL
(properly backfilled delta rows).  Each output line is one session:

  {
    "session_id":   "...",
    "task_id":      "...",
    "task_title":   "...",
    "agent_type":   "...",
    "started_at":   "...",
    "ended_at":     "...",
    "exit_reason":  "...",
    "turns": [
      {
        "entry_id":        N,
        "created_at":      "...",
        "agent_name":      "...",
        "finish_reason":   "stop" | "tool_calls" | "length",
        "prompt_tokens":   N,
        "completion_tokens": N,
        "messages":        [...],   // full reconstructed prompt (accumulated deltas)
        "response":        {...}    // parsed LLM response object
      }
    ]
  }

Usage:
    python scripts/export_sessions.py                        # stdout
    python scripts/export_sessions.py -o sessions.jsonl      # file
    python scripts/export_sessions.py --task task-123        # single task
    python scripts/export_sessions.py --since 2026-05-01     # from date
    python scripts/export_sessions.py --agent-type maestro_loop
    python scripts/export_sessions.py --min-turns 3          # skip tiny sessions
    python scripts/export_sessions.py --summary              # stats only
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database.session import engine

SEP = "=" * 72


def iter_clean_sessions(conn, task_id=None, since=None, agent_type=None):
    """Yield (session_id, task_id, agent_type, started_at, ended_at, exit_reason)
    for every session that has at least one clean delta entry."""
    conditions = [
        "be.prompt_message_count IS NOT NULL",
        "be.session_id IS NOT NULL",
    ]
    params = {}
    if task_id:
        conditions.append("be.task_id = :task_id")
        params["task_id"] = task_id
    if since:
        conditions.append("be.created_at >= :since")
        params["since"] = since
    if agent_type:
        conditions.append("s.agent_type = :agent_type")
        params["agent_type"] = agent_type

    where = " AND ".join(conditions)
    rows = conn.execute(text(f"""
        SELECT
            be.session_id,
            be.task_id,
            s.agent_type,
            s.started_at,
            s.ended_at,
            s.exit_reason
        FROM budget_entries be
        LEFT JOIN agent_sessions s
            ON s.id = be.session_id::integer
               AND be.session_id ~ '^[0-9]+$'
        WHERE {where}
        GROUP BY be.session_id, be.task_id, s.agent_type, s.started_at, s.ended_at, s.exit_reason
        ORDER BY MIN(be.id)
    """), params).fetchall()
    return rows


def fetch_session_entries(conn, session_id):
    """Return all clean delta entries for a session, ordered by id."""
    rows = conn.execute(text("""
        SELECT id, created_at, agent_name, prompt_data, response_data,
               prompt_cost, generation_cost, prompt_message_count
        FROM budget_entries
        WHERE session_id = :sid
          AND prompt_message_count IS NOT NULL
          AND prompt_data IS NOT NULL
        ORDER BY id ASC
    """), {"sid": session_id}).fetchall()
    return rows


def fetch_task_title(conn, task_id):
    if not task_id:
        return None
    row = conn.execute(text(
        "SELECT title FROM tasks WHERE id = :tid"
    ), {"tid": task_id}).fetchone()
    return row[0] if row else None


def build_session_record(conn, session_row, min_turns):
    session_id, task_id, agent_type, started_at, ended_at, exit_reason = session_row

    entries = fetch_session_entries(conn, session_id)
    if len(entries) < min_turns:
        return None

    task_title = fetch_task_title(conn, task_id)

    turns = []
    accumulated = []
    for entry in entries:
        eid, created_at, agent_name, prompt_data_raw, response_data_raw, \
            prompt_cost, gen_cost, msg_count = entry

        try:
            delta = json.loads(prompt_data_raw)
        except (json.JSONDecodeError, TypeError):
            continue
        accumulated.extend(delta)

        finish_reason = None
        response_obj = None
        if response_data_raw:
            try:
                response_obj = json.loads(response_data_raw)
                choices = response_obj.get("choices", [])
                if choices:
                    finish_reason = choices[0].get("finish_reason")
            except (json.JSONDecodeError, TypeError):
                pass

        turns.append({
            "entry_id":          eid,
            "created_at":        str(created_at),
            "agent_name":        agent_name,
            "finish_reason":     finish_reason,
            "prompt_tokens":     prompt_cost,
            "completion_tokens": gen_cost,
            "messages":          list(accumulated),
            "response":          response_obj,
        })

    if not turns:
        return None

    return {
        "session_id":  session_id,
        "task_id":     task_id,
        "task_title":  task_title,
        "agent_type":  agent_type,
        "started_at":  str(started_at) if started_at else None,
        "ended_at":    str(ended_at)   if ended_at   else None,
        "exit_reason": exit_reason,
        "turns":       turns,
    }


def main():
    parser = argparse.ArgumentParser(description="Export clean sessions to JSONL")
    parser.add_argument("-o", "--output",     default=None, help="Output file (default: stdout)")
    parser.add_argument("--task",             default=None, help="Filter by task_id")
    parser.add_argument("--since",            default=None, help="Filter by created_at >= (ISO date)")
    parser.add_argument("--agent-type",       default=None, dest="agent_type",
                                              help="Filter by agent_type (e.g. maestro_loop)")
    parser.add_argument("--min-turns",        type=int, default=1, dest="min_turns",
                                              help="Skip sessions with fewer than N turns (default 1)")
    parser.add_argument("--summary",          action="store_true",
                                              help="Print stats and exit without exporting")
    args = parser.parse_args()

    out_fh = open(args.output, "w", encoding="utf-8") if args.output else sys.stdout

    with engine.connect() as conn:
        sessions = iter_clean_sessions(
            conn,
            task_id=args.task,
            since=args.since,
            agent_type=args.agent_type,
        )

        if args.summary:
            total_sessions = len(sessions)
            total_entries = conn.execute(text(
                "SELECT COUNT(*) FROM budget_entries WHERE prompt_message_count IS NOT NULL"
            )).scalar()
            table_size = conn.execute(text(
                "SELECT pg_size_pretty(pg_total_relation_size('budget_entries'))"
            )).scalar()
            agent_counts = conn.execute(text("""
                SELECT s.agent_type, COUNT(DISTINCT be.session_id)
                FROM budget_entries be
                LEFT JOIN agent_sessions s
                    ON s.id = be.session_id::integer
                       AND be.session_id ~ '^[0-9]+$'
                WHERE be.prompt_message_count IS NOT NULL
                GROUP BY s.agent_type
                ORDER BY 2 DESC
            """)).fetchall()
            print(f"\n{SEP}")
            print(f"  Export summary")
            print(SEP)
            print(f"  Table size:       {table_size}")
            print(f"  Clean sessions:   {total_sessions:,}")
            print(f"  Clean entries:    {total_entries:,}")
            print(f"\n  By agent type:")
            for atype, count in agent_counts:
                print(f"    {(atype or 'unknown'):<30} {count:>6,} sessions")
            return

        exported = 0
        skipped = 0
        errors = 0
        for session_row in sessions:
            try:
                record = build_session_record(conn, session_row, args.min_turns)
                if record is None:
                    skipped += 1
                    continue
                out_fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                exported += 1
                if exported % 100 == 0:
                    print(f"  ... {exported} sessions exported", flush=True, file=sys.stderr)
            except Exception as e:
                errors += 1
                sid = session_row[0][:16] if session_row[0] else "?"
                print(f"  [WARN] session {sid}: {e}", file=sys.stderr)

    if args.output:
        out_fh.close()

    print(f"\nExported {exported:,} sessions, skipped {skipped:,}, errors {errors}", file=sys.stderr)
    if args.output:
        size = os.path.getsize(args.output)
        print(f"Output: {args.output}  ({size / 1024 / 1024:.1f} MiB)", file=sys.stderr)


if __name__ == "__main__":
    main()
