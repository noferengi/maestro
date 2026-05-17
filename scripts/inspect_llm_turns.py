"""
inspect_llm_turns.py — Browse LLM conversations stored in budget_entries.

Reconstructs full message history from stored deltas (prompt_message_count IS NOT NULL).
Legacy rows (prompt_message_count IS NULL) are shown as-is.

Usage:
    # List all entries for a task (summary table)
    python scripts/inspect_llm_turns.py --task task-1774228642.452532

    # Show full conversation for a single entry (reconstructed from deltas)
    python scripts/inspect_llm_turns.py --entry 2015

    # Include reasoning/chain-of-thought
    python scripts/inspect_llm_turns.py --entry 2015 --reasoning

    # Show all entries for a task in full detail
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --full

    # Show only entries in an ID range
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --from-id 2200 --to-id 2230 --full

    # Show last N entries
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --last 5 --full

    # Show all entries for a session
    python scripts/inspect_llm_turns.py --session <uuid> --full
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database.session import engine

DIVIDER = "=" * 80
SUBDIV  = "-" * 60


def _get_conn():
    return engine.connect()


def reconstruct_messages(conn, session_id, up_to_entry_id):
    """Accumulate delta prompt_data for all entries in session up to entry_id."""
    rows = conn.execute(text(
        "SELECT id, prompt_data, prompt_message_count "
        "FROM budget_entries "
        "WHERE session_id = :sid AND id <= :eid AND prompt_data IS NOT NULL "
        "ORDER BY id ASC"
    ), {"sid": session_id, "eid": up_to_entry_id}).fetchall()
    messages = []
    for row in rows:
        try:
            messages.extend(json.loads(row[1]))
        except (json.JSONDecodeError, TypeError):
            pass
    return messages


def list_entries(task_id, from_id=None, to_id=None, last=None):
    with _get_conn() as conn:
        query = (
            "SELECT id, session_id, agent_name, prompt_cost, generation_cost, "
            "       prompt_message_count, created_at "
            "FROM budget_entries WHERE task_id = :tid"
        )
        params = {"tid": task_id}
        if from_id:
            query += " AND id >= :from_id"
            params["from_id"] = from_id
        if to_id:
            query += " AND id <= :to_id"
            params["to_id"] = to_id
        query += " ORDER BY id"
        if last:
            query = (
                f"SELECT * FROM ({query}) sub ORDER BY id DESC LIMIT {int(last)}"
            )
            query = f"SELECT * FROM ({query}) sub2 ORDER BY id"
        rows = conn.execute(text(query), params).fetchall()

    print(f"\n{'ID':>6}  {'pp':>6}  {'tg':>6}  {'msgs':>5}  "
          f"{'agent':<25}  {'session':<12}  created_at")
    print(SUBDIV)
    for r in rows:
        eid, sid, aname, pp, tg, pmc, cat = r
        sid_short = (str(sid)[:10] + "...") if sid and len(str(sid)) > 10 else (sid or "—")
        delta_flag = "  " if pmc is not None else "L "  # L = legacy full history
        print(f"{eid:>6}{delta_flag}  {pp:>6}  {tg:>6}  {(pmc or 0):>5}  "
              f"{(aname or ''):<25}  {sid_short:<12}  {cat}")
    print(f"\n{len(rows)} entries  (L = legacy full-history row)")


def _fmt_args(args_raw):
    if isinstance(args_raw, dict):
        return json.dumps(args_raw, indent=4)
    try:
        return json.dumps(json.loads(args_raw), indent=4)
    except Exception:
        return str(args_raw)


def show_entry(entry_id, show_reasoning=False):
    with _get_conn() as conn:
        row = conn.execute(
            text("SELECT id, task_id, session_id, agent_name, prompt_cost, "
                 "generation_cost, prompt_message_count, prompt_data, response_data, "
                 "created_at FROM budget_entries WHERE id = :eid"),
            {"eid": entry_id},
        ).fetchone()
        if not row:
            print(f"Entry {entry_id} not found")
            return

        eid, task_id, session_id, agent_name, pp, tg, pmc, pd_raw, rd_raw, cat = row

        print(f"\n{DIVIDER}")
        print(f"ENTRY {eid}  |  task={task_id}  |  agent={agent_name}")
        print(f"session={session_id}  |  pp={pp}  tg={tg}  msgs={pmc}")
        print(f"created: {cat}")
        print(DIVIDER)

        # Reconstruct full message list
        if pmc is not None and session_id:
            messages = reconstruct_messages(conn, session_id, entry_id)
            source = f"reconstructed from deltas ({len(messages)} messages)"
        else:
            try:
                messages = json.loads(pd_raw) if pd_raw else []
            except (json.JSONDecodeError, TypeError):
                messages = []
            source = f"legacy full-history ({len(messages)} messages)"

    print(f"\n{'='*38} PROMPT — {source} {'='*38}\n")
    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content") or ""
        tool_calls_in_msg = msg.get("tool_calls", [])
        tool_call_id = msg.get("tool_call_id")

        print(f"\n[{i}] {role.upper()}", end="")
        if tool_call_id:
            print(f"  (tool_result  call_id={tool_call_id})", end="")
        print()

        if isinstance(content, list):
            content = "\n".join(
                p.get("text", "") for p in content if p.get("type") == "text"
            )

        if tool_calls_in_msg:
            print(f"  -> {len(tool_calls_in_msg)} tool call(s):")
            for tc in tool_calls_in_msg:
                fn = tc.get("function", {})
                print(f"    * {fn.get('name','?')}()  id={tc.get('id','')}")
                print(f"      args: {_fmt_args(fn.get('arguments', '{}'))}")

        if content:
            print(content)

    print(f"\n{'='*40} RESPONSE {'='*40}\n")
    try:
        resp = json.loads(rd_raw) if rd_raw else {}
    except (json.JSONDecodeError, TypeError):
        print(f"[response_data parse error]")
        print(str(rd_raw))
        return

    choices = resp.get("choices", [])
    if not choices:
        print("[no choices in response]")
        return

    choice = choices[0]
    finish = choice.get("finish_reason", "?")
    msg = choice.get("message", {})
    content = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    tool_calls = msg.get("tool_calls", [])

    print(f"finish_reason: {finish}")

    if reasoning:
        if show_reasoning:
            print(f"\n--- REASONING ({len(reasoning)} chars) ---\n{reasoning}")
        else:
            print(f"\n--- REASONING ({len(reasoning)} chars; --reasoning to show) ---")

    if tool_calls:
        print(f"\n--- TOOL CALLS ({len(tool_calls)}) ---")
        for tc in tool_calls:
            fn = tc.get("function", {})
            print(f"\n  * {fn.get('name','?')}()  id={tc.get('id','')}")
            print(f"    args:\n{_fmt_args(fn.get('arguments', '{}'))}")

    if content:
        print(f"\n--- CONTENT ({len(content)} chars) ---\n{content}")
    elif not tool_calls:
        print("[no content, no tool calls]")


def get_entry_ids(conn, *, task_id=None, session_id=None, from_id=None, to_id=None, last=None):
    if session_id:
        query = "SELECT id FROM budget_entries WHERE session_id = :sid"
        params = {"sid": session_id}
    else:
        query = "SELECT id FROM budget_entries WHERE task_id = :tid"
        params = {"tid": task_id}
    if from_id:
        query += " AND id >= :from_id"
        params["from_id"] = from_id
    if to_id:
        query += " AND id <= :to_id"
        params["to_id"] = to_id
    query += " ORDER BY id"
    if last:
        query = (
            f"SELECT * FROM ({query}) sub ORDER BY id DESC LIMIT {int(last)}"
        )
        query = f"SELECT * FROM ({query}) sub2 ORDER BY id"
    rows = conn.execute(text(query), params).fetchall()
    return [r[0] for r in rows]


def show_task_full(task_id=None, session_id=None, show_reasoning=False,
                   from_id=None, to_id=None, last=None):
    with _get_conn() as conn:
        ids = get_entry_ids(
            conn, task_id=task_id, session_id=session_id,
            from_id=from_id, to_id=to_id, last=last,
        )
    for eid in ids:
        show_entry(eid, show_reasoning=show_reasoning)
        print()


def main():
    parser = argparse.ArgumentParser(description="Browse LLM turns in budget_entries")
    parser.add_argument("--task",      help="Task ID to list or inspect")
    parser.add_argument("--session",   help="Session ID (shows all entries for that session)")
    parser.add_argument("--entry",     type=int, help="Single budget_entry ID to show")
    parser.add_argument("--reasoning", action="store_true",
                                       help="Show reasoning_content (chain-of-thought)")
    parser.add_argument("--full",      action="store_true",
                                       help="Show all entries in full detail")
    parser.add_argument("--from-id",   type=int, dest="from_id",
                                       help="Start from this entry ID")
    parser.add_argument("--to-id",     type=int, dest="to_id",
                                       help="End at this entry ID")
    parser.add_argument("--last",      type=int,
                                       help="Show only the last N entries")
    args = parser.parse_args()

    if args.entry:
        show_entry(args.entry, show_reasoning=args.reasoning)
    elif args.session:
        if args.full or args.from_id or args.to_id or args.last:
            show_task_full(session_id=args.session, show_reasoning=args.reasoning,
                           from_id=args.from_id, to_id=args.to_id, last=args.last)
        else:
            with _get_conn() as conn:
                ids = get_entry_ids(conn, session_id=args.session)
            print(f"\nSession {args.session}: {len(ids)} entries")
            for eid in ids:
                show_entry(eid, show_reasoning=args.reasoning)
                print()
    elif args.task:
        if args.full or args.from_id or args.to_id or args.last:
            show_task_full(task_id=args.task, show_reasoning=args.reasoning,
                           from_id=args.from_id, to_id=args.to_id, last=args.last)
        else:
            list_entries(args.task, from_id=args.from_id,
                         to_id=args.to_id, last=args.last)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
