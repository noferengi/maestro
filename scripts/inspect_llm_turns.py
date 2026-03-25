#!/usr/bin/env python3
"""
inspect_llm_turns.py — Browse LLM conversations stored in budget_entries.

Usage:
    # List all entries for a task (summary table)
    python scripts/inspect_llm_turns.py --task task-1774228642.452532

    # Show full conversation for a single entry (full by default)
    python scripts/inspect_llm_turns.py --entry 2015

    # Include reasoning/chain-of-thought
    python scripts/inspect_llm_turns.py --entry 2015 --reasoning

    # Show all entries for a task, full detail
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --full

    # Show only entries in an ID range (full detail)
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --from-id 2200 --to-id 2230 --full

    # Show last N entries for a task (summary)
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --last 10

    # Show last N entries in full
    python scripts/inspect_llm_turns.py --task task-1774228642.452532 --last 5 --full
"""

import argparse
import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "kanban.db"
DIVIDER = "=" * 80
SUBDIV  = "-" * 60


def connect():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def list_entries(task_id, from_id=None, to_id=None, last=None):
    conn = connect()
    cur = conn.cursor()
    query = "SELECT id, prompt_cost, generation_cost, created_at FROM budget_entries WHERE task_id=?"
    params = [task_id]
    if from_id:
        query += " AND id >= ?"
        params.append(from_id)
    if to_id:
        query += " AND id <= ?"
        params.append(to_id)
    query += " ORDER BY id"
    if last:
        query = f"SELECT * FROM ({query}) ORDER BY id DESC LIMIT {int(last)}"
        query = f"SELECT * FROM ({query}) ORDER BY id"
    cur.execute(query, params)
    rows = cur.fetchall()
    conn.close()

    print(f"\n{'ID':>6}  {'pp':>6}  {'tg':>6}  {'created_at'}")
    print(SUBDIV)
    for r in rows:
        print(f"{r['id']:>6}  {r['prompt_cost']:>6}  {r['generation_cost']:>6}  {r['created_at']}")
    print(f"\n{len(rows)} entries")


def _fmt_args(args_raw):
    """Return args as a formatted JSON string regardless of input type."""
    if isinstance(args_raw, dict):
        return json.dumps(args_raw, indent=4)
    try:
        return json.dumps(json.loads(args_raw), indent=4)
    except Exception:
        return str(args_raw)


def show_entry(entry_id, show_reasoning=False):
    conn = connect()
    cur = conn.cursor()
    cur.execute("SELECT * FROM budget_entries WHERE id=?", (entry_id,))
    r = cur.fetchone()
    conn.close()
    if not r:
        print(f"Entry {entry_id} not found")
        return

    print(f"\n{DIVIDER}")
    print(f"ENTRY {r['id']}  |  task={r['task_id']}  |  pp={r['prompt_cost']}  tg={r['generation_cost']}")
    print(f"created: {r['created_at']}")
    print(DIVIDER)

    # --- PROMPT ---
    try:
        messages = json.loads(r['prompt_data'])
    except Exception as e:
        print(f"[prompt_data parse error: {e}]")
        print(r['prompt_data'])
        messages = []

    print(f"\n{'='*40} PROMPT ({len(messages)} messages) {'='*40}\n")
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
            content = "\n".join(p.get("text", "") for p in content if p.get("type") == "text")

        if tool_calls_in_msg:
            print(f"  ↳ {len(tool_calls_in_msg)} tool call(s):")
            for tc in tool_calls_in_msg:
                fn = tc.get("function", {})
                print(f"    • {fn.get('name','?')}()  id={tc.get('id','')}")
                print(f"      args: {_fmt_args(fn.get('arguments', '{}'))}")

        if content:
            print(content)

    # --- RESPONSE ---
    print(f"\n{'='*40} RESPONSE {'='*40}\n")
    try:
        resp = json.loads(r['response_data'])
    except Exception as e:
        print(f"[response_data parse error: {e}]")
        print(str(r['response_data']))
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
            print(f"\n--- REASONING ({len(reasoning)} chars, use --reasoning to show) ---")

    if tool_calls:
        print(f"\n--- TOOL CALLS ({len(tool_calls)}) ---")
        for tc in tool_calls:
            fn = tc.get("function", {})
            print(f"\n  • {fn.get('name','?')}()  id={tc.get('id','')}")
            print(f"    args:\n{_fmt_args(fn.get('arguments', '{}'))}")

    if content:
        print(f"\n--- CONTENT ({len(content)} chars) ---\n{content}")
    elif not tool_calls:
        print("[no content, no tool calls]")


def show_task_full(task_id, show_reasoning=False, from_id=None, to_id=None, last=None):
    conn = connect()
    cur = conn.cursor()
    if last:
        cur.execute(
            "SELECT id FROM (SELECT id FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT ?) ORDER BY id",
            (task_id, int(last))
        )
    else:
        query = "SELECT id FROM budget_entries WHERE task_id=?"
        params = [task_id]
        if from_id:
            query += " AND id >= ?"
            params.append(from_id)
        if to_id:
            query += " AND id <= ?"
            params.append(to_id)
        query += " ORDER BY id"
        cur.execute(query, params)
    ids = [row['id'] for row in cur.fetchall()]
    conn.close()

    for eid in ids:
        show_entry(eid, show_reasoning=show_reasoning)
        print()


def main():
    parser = argparse.ArgumentParser(description="Browse LLM turns in budget_entries")
    parser.add_argument("--task", help="Task ID to list or inspect")
    parser.add_argument("--entry", type=int, help="Single budget_entry ID to show")
    parser.add_argument("--reasoning", action="store_true", help="Show reasoning_content (chain-of-thought)")
    parser.add_argument("--full", action="store_true", help="Show all entries in full detail (requires --task)")
    parser.add_argument("--from-id", type=int, dest="from_id", help="Start from this entry ID")
    parser.add_argument("--to-id", type=int, dest="to_id", help="End at this entry ID")
    parser.add_argument("--last", type=int, help="Show only the last N entries")
    args = parser.parse_args()

    if args.entry:
        show_entry(args.entry, show_reasoning=args.reasoning)
    elif args.task:
        if args.full or args.from_id or args.to_id or args.last:
            show_task_full(args.task, show_reasoning=args.reasoning,
                          from_id=args.from_id, to_id=args.to_id, last=args.last)
        else:
            list_entries(args.task)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
