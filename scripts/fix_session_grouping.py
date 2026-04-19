# -*- coding: utf-8 -*-
"""
fix_session_grouping.py -- Diagnostics session-grouping repair tool

Multi-stage pipelines (Planning, Intake, etc.) call set_llm_session_context()
once per pipeline run, so ALL sub-stage LLM calls share a single session_id.
Each sub-stage has its own system prompt and fresh context, making them
logically distinct conversations — but the diagnostics view merges them into
one session, causing wrong turn display, negative delta-prompt values, and
broken click-to-scroll navigation.

This script detects sub-conversation boundaries within a shared session_id
group by comparing system prompt content between adjacent entries.  When the
system prompt changes, a new sub-conversation starts.  Fallback: a >40%
prompt_cost drop from the previous entry also signals a fresh start.

Usage:
    venv/Scripts/python.exe scripts/fix_session_grouping.py
    venv/Scripts/python.exe scripts/fix_session_grouping.py --task TASK_ID
    venv/Scripts/python.exe scripts/fix_session_grouping.py --fix
    venv/Scripts/python.exe scripts/fix_session_grouping.py --task TASK_ID --fix
"""

import argparse
import json
import os
import sqlite3
import sys
import uuid
from collections import defaultdict
from datetime import datetime

DB_PATH = "data/kanban.db"
CTX_DROP_FLOOR = 0.60   # prompt_cost ratio below this => new sub-conversation


def open_db():
    if not os.path.exists(DB_PATH):
        print("DB not found at " + DB_PATH)
        sys.exit(1)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn, conn.cursor()


def hdr(text, width=72):
    safe_print("")
    safe_print("=" * width)
    safe_print("  " + text)
    safe_print("=" * width)


def fmt_tokens(n):
    if n is None:
        return "?"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}M"
    if n >= 1024:
        return f"{n / 1024:.1f}K"
    return str(n)


def safe_print(text):
    """Print with ASCII-safe fallback for Windows cp1252 terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def extract_system_prompt(prompt_data_json):
    """Return the content of the first system message, or None."""
    if not prompt_data_json:
        return None
    try:
        msgs = json.loads(prompt_data_json)
        if not isinstance(msgs, list):
            return None
        for msg in msgs:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content", "")
                if isinstance(content, list):
                    # content blocks format
                    parts = [c.get("text", "") for c in content if isinstance(c, dict)]
                    content = " ".join(parts)
                return str(content)
    except (json.JSONDecodeError, TypeError):
        pass
    return None


def fingerprint(system_prompt, length=80):
    """Short display string for a system prompt (first N non-whitespace chars)."""
    if system_prompt is None:
        return "(no system message)"
    cleaned = " ".join(system_prompt.split())
    return cleaned[:length] + ("..." if len(cleaned) > length else "")


def detect_sub_conversations(entries):
    """
    Split a list of entries (sorted by created_at ASC) into sub-conversations.

    Returns a list of lists: each inner list is one sub-conversation.

    Rules (in priority order):
    1. System prompt content changes between adjacent entries -> new sub-conv
    2. prompt_cost drops to < CTX_DROP_FLOOR * previous entry's prompt_cost -> new sub-conv
    3. Otherwise: continue the same sub-conversation
    """
    if not entries:
        return []

    sub_convs = []
    current = [entries[0]]
    prev_sys = extract_system_prompt(entries[0]["prompt_data"])
    prev_pp  = entries[0]["prompt_cost"] or 0

    for entry in entries[1:]:
        curr_sys = extract_system_prompt(entry["prompt_data"])
        curr_pp  = entry["prompt_cost"] or 0

        sys_changed  = (curr_sys != prev_sys)
        ctx_dropped  = (prev_pp > 0 and curr_pp < prev_pp * CTX_DROP_FLOOR)

        if sys_changed or ctx_dropped:
            sub_convs.append(current)
            current = [entry]
        else:
            current.append(entry)

        prev_sys = curr_sys
        prev_pp  = curr_pp

    sub_convs.append(current)
    return sub_convs


def load_sessions(cur, task_id=None):
    """
    Return a dict: (task_id, session_id) -> list of entry rows (sorted by created_at ASC).
    Entries with NULL session_id are keyed as (task_id, '__null__') so that
    null-session entries from different tasks are never conflated.
    """
    if task_id:
        cur.execute(
            """
            SELECT id, task_id, session_id, agent_name,
                   prompt_cost, generation_cost, tool_calls,
                   prompt_data, created_at
            FROM budget_entries
            WHERE task_id = ?
            ORDER BY created_at ASC
            """,
            (task_id,),
        )
    else:
        cur.execute(
            """
            SELECT id, task_id, session_id, agent_name,
                   prompt_cost, generation_cost, tool_calls,
                   prompt_data, created_at
            FROM budget_entries
            ORDER BY created_at ASC
            """
        )

    groups = defaultdict(list)
    for row in cur.fetchall():
        tid = row["task_id"] or "__null_task__"
        sid = row["session_id"] or "__null__"
        groups[(tid, sid)].append(row)
    return dict(groups)


def analyze(sessions, verbose=False):
    """
    For each (task_id, session_id) group with > 1 entry, detect sub-conversations.
    Returns a list of (orig_session_id, task_id, agent_name, sub_convs) tuples
    where sub_convs has > 1 element (i.e., needs splitting).
    """
    to_split = []
    total_sessions = 0
    total_already_clean = 0

    for (tid, sid), entries in sorted(sessions.items(), key=lambda kv: kv[1][0]["created_at"]):
        total_sessions += 1

        # Skip legacy null-session entries — they have no session_id to repair.
        if sid == "__null__":
            total_already_clean += len(entries)
            continue

        if len(entries) == 1:
            total_already_clean += 1
            continue

        sub_convs = detect_sub_conversations(entries)

        task_id    = entries[0]["task_id"] or "(null)"
        agent_name = entries[0]["agent_name"] or "?"
        sid_short  = sid[:8] if len(sid) >= 8 else sid

        if len(sub_convs) > 1:
            to_split.append((sid, task_id, agent_name, sub_convs))
            safe_print(f"\nSESSION {sid_short}... ({agent_name} · {len(entries)} entries)"
                       f"  task={task_id}")
            for i, sub in enumerate(sub_convs, 1):
                first_id = sub[0]["id"]
                last_id  = sub[-1]["id"]
                id_range = f"#{first_id}" if len(sub) == 1 else f"#{first_id}..#{last_id}"
                pp_min   = min(e["prompt_cost"] or 0 for e in sub)
                pp_max   = max(e["prompt_cost"] or 0 for e in sub)
                pp_str   = (fmt_tokens(pp_min) if pp_min == pp_max
                            else f"{fmt_tokens(pp_min)}->{fmt_tokens(pp_max)}")
                sys_fp   = fingerprint(extract_system_prompt(sub[0]["prompt_data"]))
                safe_print(f"  Sub-conv {i} -- \"{sys_fp}\"")
                safe_print(f"             [{id_range}, {len(sub)} entr{'y' if len(sub)==1 else 'ies'}, ctx {pp_str}]")
            safe_print(f"  -> would split into {len(sub_convs)} sessions ({len(sub_convs)-1} new UUIDs)")
        elif verbose:
            safe_print(f"  OK  {sid_short}... ({agent_name} · {len(entries)} entries, single sub-conv)")
        else:
            total_already_clean += 1

    return to_split, total_sessions, total_already_clean


def apply_fix(conn, cur, to_split):
    """
    Re-assign session_ids for sub-conversations 2, 3, ... within each session.
    Sub-conversation 1 keeps the original session_id.
    Runs inside a single transaction; rolls back on any error.
    """
    total_updated = 0
    new_sessions   = 0

    try:
        for orig_sid, task_id, agent_name, sub_convs in to_split:
            # Sub-conv 0 (index 0) keeps the original session_id.
            for sub in sub_convs[1:]:
                new_sid = str(uuid.uuid4())
                ids     = [e["id"] for e in sub]
                placeholders = ",".join("?" * len(ids))
                cur.execute(
                    f"UPDATE budget_entries SET session_id = ? WHERE id IN ({placeholders})",
                    [new_sid] + ids,
                )
                updated = cur.rowcount
                total_updated += updated
                new_sessions  += 1
                agent_name_str = sub[0]["agent_name"] or "?"
                sys_fp = fingerprint(extract_system_prompt(sub[0]["prompt_data"]), 50)
                safe_print(f"  + new session {new_sid[:8]}... for {updated} entr{'y' if updated==1 else 'ies'}"
                           f" ({agent_name_str}) \"{sys_fp}\"")

        conn.commit()
    except Exception as exc:
        conn.rollback()
        safe_print(f"\nERROR: {exc}")
        safe_print("Transaction rolled back -- no changes applied.")
        sys.exit(1)

    return total_updated, new_sessions


def main():
    parser = argparse.ArgumentParser(
        description="Detect and repair merged multi-sub-conversation session_ids in budget_entries."
    )
    parser.add_argument("--task",    metavar="TASK_ID", help="Limit to a single task")
    parser.add_argument("--fix",     action="store_true", help="Apply the re-assignment (default: dry run)")
    parser.add_argument("--verbose", action="store_true", help="Also show clean single-sub-conv sessions")
    args = parser.parse_args()

    conn, cur = open_db()

    hdr("Session Grouping Analysis" + (" - DRY RUN" if not args.fix else " - APPLYING FIXES"))

    if args.task:
        safe_print(f"  Filtering to task: {args.task}")

    sessions = load_sessions(cur, task_id=args.task)
    if not sessions:
        safe_print("  No budget_entries found.")
        return

    safe_print(f"  {len(sessions)} distinct (task, session) group(s) across"
               f" {sum(len(v) for v in sessions.values())} entries\n")

    to_split, total_sessions, already_clean = analyze(sessions, verbose=args.verbose)

    hdr("Summary")
    safe_print(f"  Total session groups : {total_sessions}")
    safe_print(f"  Already clean        : {already_clean}")
    safe_print(f"  Need splitting       : {len(to_split)}")
    sub_conv_total = sum(len(sc) for _, _, _, sc in to_split)
    safe_print(f"  Sub-conversations    : {sub_conv_total} total across {len(to_split)} merged sessions")

    if not to_split:
        safe_print("\n  Nothing to do -- all sessions are correctly grouped.")
        return

    if not args.fix:
        safe_print("\n  Run with --fix to apply re-assignment.")
        return

    hdr("Applying Fixes")
    updated, new_sid_count = apply_fix(conn, cur, to_split)
    safe_print(f"\n  Done. {updated} entries updated, {new_sid_count} new session_id(s) assigned.")
    safe_print("  Reload /diagnostics to see the corrected groupings.")

    conn.close()


if __name__ == "__main__":
    main()
