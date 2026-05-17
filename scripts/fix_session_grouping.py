# -*- coding: utf-8 -*-
"""
fix_session_grouping.py -- Diagnostics session-grouping repair tool

Multi-stage pipelines (Planning, Intake, etc.) call set_llm_session_context()
once per pipeline run, so ALL sub-stage LLM calls share a single session_id.
Each sub-stage has its own system prompt and fresh context, making them
logically distinct conversations -- but the diagnostics view merges them into
one session, causing wrong turn display, negative delta-prompt values, and
broken click-to-scroll navigation.

This script detects sub-conversation boundaries within a shared session_id
group by comparing system prompt content between adjacent entries.  When the
system prompt changes, a new sub-conversation starts.  Fallback: a >40%
prompt_cost drop from the previous entry also signals a fresh start.

Delta storage: if prompt_message_count IS NOT NULL, prompt_data contains only
the delta for that turn.  The script reconstructs the full message list by
accumulating deltas before extracting system prompts.

Usage:
    venv/Scripts/python.exe scripts/fix_session_grouping.py
    venv/Scripts/python.exe scripts/fix_session_grouping.py --task TASK_ID
    venv/Scripts/python.exe scripts/fix_session_grouping.py --fix
    venv/Scripts/python.exe scripts/fix_session_grouping.py --task TASK_ID --fix
"""

import argparse
import json
import os
import sys
import uuid
from collections import defaultdict

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text

from app.database.session import engine

CTX_DROP_FLOOR = 0.60   # prompt_cost ratio below this => new sub-conversation


def safe_print(text):
    """Print with ASCII-safe fallback for Windows cp1252 terminals."""
    try:
        print(text)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"))


def hdr(label, width=72):
    safe_print("")
    safe_print("=" * width)
    safe_print("  " + label)
    safe_print("=" * width)


def fmt_tokens(n):
    if n is None:
        return "?"
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}M"
    if n >= 1024:
        return f"{n / 1024:.1f}K"
    return str(n)


def reconstruct_prompt_data(entries: list[dict]) -> None:
    """For delta-storage rows (prompt_message_count IS NOT NULL), accumulate
    deltas in-place so that each entry's reconstructed_prompt_data field
    holds the full message list up to that turn."""
    accumulated: list = []
    for entry in entries:
        pd = entry.get("prompt_data")
        if entry.get("prompt_message_count") is not None:
            # Delta row: accumulate
            delta = json.loads(pd) if pd else []
            accumulated.extend(delta)
            entry["reconstructed_prompt_data"] = list(accumulated)
        else:
            # Legacy row: prompt_data is the full list
            entry["reconstructed_prompt_data"] = json.loads(pd) if pd else []
            # Re-sync accumulated from legacy row so subsequent delta rows are correct
            accumulated = list(entry["reconstructed_prompt_data"])


def extract_system_prompt(entry: dict) -> str | None:
    """Return the content of the first system message in the reconstructed
    message list, or None."""
    msgs = entry.get("reconstructed_prompt_data")
    if not msgs:
        return None
    for msg in msgs:
        if isinstance(msg, dict) and msg.get("role") == "system":
            content = msg.get("content", "")
            if isinstance(content, list):
                content = " ".join(c.get("text", "") for c in content if isinstance(c, dict))
            return str(content)
    return None


def fingerprint(system_prompt: str | None, length: int = 80) -> str:
    if system_prompt is None:
        return "(no system message)"
    cleaned = " ".join(system_prompt.split())
    return cleaned[:length] + ("..." if len(cleaned) > length else "")


def detect_sub_conversations(entries: list[dict]) -> list[list[dict]]:
    """Split a list of entries (sorted by id ASC) into sub-conversations.

    Rules (in priority order):
    1. System prompt content changes between adjacent entries -> new sub-conv
    2. prompt_cost drops to < CTX_DROP_FLOOR * previous entry's prompt_cost -> new sub-conv
    3. Otherwise: continue the same sub-conversation
    """
    if not entries:
        return []

    reconstruct_prompt_data(entries)

    sub_convs: list[list[dict]] = []
    current = [entries[0]]
    prev_sys = extract_system_prompt(entries[0])
    prev_pp = entries[0]["prompt_cost"] or 0

    for entry in entries[1:]:
        curr_sys = extract_system_prompt(entry)
        curr_pp = entry["prompt_cost"] or 0

        sys_changed = curr_sys != prev_sys
        ctx_dropped = prev_pp > 0 and curr_pp < prev_pp * CTX_DROP_FLOOR

        if sys_changed or ctx_dropped:
            sub_convs.append(current)
            current = [entry]
        else:
            current.append(entry)

        prev_sys = curr_sys
        prev_pp = curr_pp

    sub_convs.append(current)
    return sub_convs


def load_sessions(conn, task_id: str | None = None) -> dict:
    """Return dict: (task_id, session_id) -> list[dict] sorted by id ASC."""
    if task_id:
        rows = conn.execute(
            text(
                "SELECT id, task_id, session_id, agent_name, "
                "       prompt_cost, generation_cost, tool_calls, "
                "       prompt_data, prompt_message_count, created_at "
                "FROM budget_entries "
                "WHERE task_id = :tid "
                "ORDER BY id ASC"
            ),
            {"tid": task_id},
        ).fetchall()
    else:
        rows = conn.execute(
            text(
                "SELECT id, task_id, session_id, agent_name, "
                "       prompt_cost, generation_cost, tool_calls, "
                "       prompt_data, prompt_message_count, created_at "
                "FROM budget_entries "
                "ORDER BY id ASC"
            )
        ).fetchall()

    groups: dict = defaultdict(list)
    for row in rows:
        d = dict(row._mapping)
        tid = d["task_id"] or "__null_task__"
        sid = d["session_id"] or "__null__"
        groups[(tid, sid)].append(d)
    return dict(groups)


def analyze(sessions: dict, verbose: bool = False):
    to_split = []
    total_sessions = 0
    total_already_clean = 0

    for (tid, sid), entries in sorted(sessions.items(), key=lambda kv: kv[1][0]["created_at"]):
        total_sessions += 1

        if sid == "__null__":
            total_already_clean += len(entries)
            continue

        if len(entries) == 1:
            total_already_clean += 1
            continue

        sub_convs = detect_sub_conversations(entries)

        agent_name = entries[0]["agent_name"] or "?"
        sid_short = sid[:8] if len(sid) >= 8 else sid

        if len(sub_convs) > 1:
            to_split.append((sid, tid, agent_name, sub_convs))
            safe_print(f"\nSESSION {sid_short}... ({agent_name} - {len(entries)} entries)"
                       f"  task={tid}")
            for i, sub in enumerate(sub_convs, 1):
                first_id = sub[0]["id"]
                last_id = sub[-1]["id"]
                id_range = f"#{first_id}" if len(sub) == 1 else f"#{first_id}..#{last_id}"
                pp_min = min(e["prompt_cost"] or 0 for e in sub)
                pp_max = max(e["prompt_cost"] or 0 for e in sub)
                pp_str = (fmt_tokens(pp_min) if pp_min == pp_max
                          else f"{fmt_tokens(pp_min)}->{fmt_tokens(pp_max)}")
                sys_fp = fingerprint(extract_system_prompt(sub[0]), 50)
                safe_print(f"  Sub-conv {i} -- \"{sys_fp}\"")
                safe_print(f"             [{id_range}, {len(sub)} entr{'y' if len(sub) == 1 else 'ies'}, ctx {pp_str}]")
            safe_print(f"  -> would split into {len(sub_convs)} sessions ({len(sub_convs) - 1} new UUIDs)")
        elif verbose:
            safe_print(f"  OK  {sid_short}... ({agent_name} - {len(entries)} entries, single sub-conv)")
        else:
            total_already_clean += 1

    return to_split, total_sessions, total_already_clean


def apply_fix(conn, to_split: list) -> tuple[int, int]:
    total_updated = 0
    new_sessions = 0

    for orig_sid, task_id, agent_name, sub_convs in to_split:
        for sub in sub_convs[1:]:
            new_sid = str(uuid.uuid4())
            ids = [e["id"] for e in sub]
            for eid in ids:
                conn.execute(
                    text("UPDATE budget_entries SET session_id = :sid WHERE id = :eid"),
                    {"sid": new_sid, "eid": eid},
                )
            total_updated += len(ids)
            new_sessions += 1
            agent_name_str = sub[0]["agent_name"] or "?"
            sys_fp = fingerprint(extract_system_prompt(sub[0]), 50)
            safe_print(f"  + new session {new_sid[:8]}... for {len(ids)} entr"
                       f"{'y' if len(ids) == 1 else 'ies'}"
                       f" ({agent_name_str}) \"{sys_fp}\"")

    conn.commit()
    return total_updated, new_sessions


def main():
    parser = argparse.ArgumentParser(
        description="Detect and repair merged multi-sub-conversation session_ids."
    )
    parser.add_argument("--task", metavar="TASK_ID", help="Limit to a single task")
    parser.add_argument("--fix", action="store_true", help="Apply re-assignment (default: dry run)")
    parser.add_argument("--verbose", action="store_true", help="Show clean single-sub-conv sessions too")
    args = parser.parse_args()

    hdr("Session Grouping Analysis" + (" - DRY RUN" if not args.fix else " - APPLYING FIXES"))

    if args.task:
        safe_print(f"  Filtering to task: {args.task}")

    with engine.connect() as conn:
        sessions = load_sessions(conn, task_id=args.task)
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
        updated, new_sid_count = apply_fix(conn, to_split)
        safe_print(f"\n  Done. {updated} entries updated, {new_sid_count} new session_id(s) assigned.")
        safe_print("  Reload /diagnostics to see the corrected groupings.")


if __name__ == "__main__":
    main()
