"""
check_500s.py — Diagnose LLM 500 errors by correlating log events with DB state.

What this shows:
  - Every 500/error event in the log with its timestamp and error body
  - Budget entries whose calls landed within +/-N seconds of each 500
  - Top sessions by token spend (via expenses table — accurate with delta storage)
  - Seconds where >1 call completed (concurrent request slot contention)
  - Root cause analysis summary

Usage:
    venv/Scripts/python.exe scripts/check_500s.py              # last 2 hours
    venv/Scripts/python.exe scripts/check_500s.py --hours 12   # last 12 hours
    venv/Scripts/python.exe scripts/check_500s.py --all        # all time
    venv/Scripts/python.exe scripts/check_500s.py --task <id>  # token growth for task
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from sqlalchemy import text
from app.database.session import engine

_LOG_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "logs", "maestro.log")

# ---------------------------------------------------------------------------
# Token estimate
# ---------------------------------------------------------------------------

def _tok(chars: int) -> int:
    return chars // 4


def _fmt_size(chars: int) -> str:
    tok = _tok(chars)
    if chars < 1024:
        return f"{chars}B / ~{tok}tok"
    elif chars < 1024 * 1024:
        return f"{chars/1024:.1f}KB / ~{tok:,}tok"
    else:
        return f"{chars/1024/1024:.1f}MB / ~{tok:,}tok"


# ---------------------------------------------------------------------------
# Log parsing  (unchanged from original — no DB dependency here)
# ---------------------------------------------------------------------------

_HTTP_LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'\s+\[\w+\]\s+httpx:.*?"HTTP/1\.1\s+(?P<code>\d{3})\s+(?P<reason>[^"]*)"'
)
_BODY_LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'\s+\[WARNING\].*?returned (?P<code>\d{3}) -- body: (?P<body>.+)$'
)
_RECOVER_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'.*?back online.*?(?P<attempts>\d+) attempt'
)


def _parse_log_ts(ts_str: str) -> datetime | None:
    try:
        import time as _time
        local_dt = datetime.fromisoformat(ts_str)
        utc_offset_s = -_time.timezone if not _time.daylight else -_time.altzone
        return local_dt - timedelta(seconds=utc_offset_s)
    except Exception:
        return None


def _local_to_utc(dt: datetime) -> datetime:
    import time as _time
    utc_offset_s = -_time.timezone if not _time.daylight else -_time.altzone
    return dt - timedelta(seconds=utc_offset_s)


def parse_log(path: str, since: datetime | None) -> dict:
    result = {"errors": [], "bodies": [], "recoveries": []}
    if not os.path.exists(path):
        print(f"[WARN] Log file not found: {path}")
        return result
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip()
            m = _HTTP_LINE_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                code = int(m.group("code"))
                if code >= 500 and (since is None or (ts and ts >= since)):
                    result["errors"].append((ts, code, m.group("reason").strip()))
                continue
            m = _BODY_LINE_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                code = int(m.group("code"))
                if code >= 500 and (since is None or (ts and ts >= since)):
                    result["bodies"].append((ts, code, m.group("body").strip()))
                continue
            m = _RECOVER_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                if since is None or (ts and ts >= since):
                    result["recoveries"].append((ts, int(m.group("attempts"))))
    return result


# ---------------------------------------------------------------------------
# DB queries — PostgreSQL
# ---------------------------------------------------------------------------

def get_entries_near(conn, ts: datetime, window_s: int = 10) -> list:
    """Budget entries within ±window_s seconds of ts."""
    rows = conn.execute(text("""
        SELECT be.id, be.task_id, be.created_at,
               be.prompt_cost, be.generation_cost,
               be.tool_calls, be.session_id, be.agent_name,
               be.prompt_message_count
        FROM budget_entries be
        WHERE be.created_at BETWEEN :lo AND :hi
        ORDER BY be.created_at
    """), {
        "lo": ts - timedelta(seconds=window_s),
        "hi": ts + timedelta(seconds=window_s),
    }).fetchall()
    return rows


def get_top_sessions_by_tokens(conn, limit: int = 15, since: datetime | None = None) -> list:
    """Top sessions by total prompt tokens (from expenses — accurate with delta storage)."""
    params = {}
    since_clause = ""
    if since:
        since_clause = "WHERE be.created_at >= :since"
        params["since"] = since
    rows = conn.execute(text(f"""
        SELECT
            be.session_id,
            be.task_id,
            be.agent_name,
            SUM(e.prompt_tokens)      AS total_prompt_tok,
            SUM(e.completion_tokens)  AS total_completion_tok,
            COUNT(be.id)              AS turns,
            MIN(be.created_at)        AS first_call
        FROM budget_entries be
        JOIN expenses e ON e.budget_entry_id = be.id
        {since_clause}
        GROUP BY be.session_id, be.task_id, be.agent_name
        ORDER BY total_prompt_tok DESC
        LIMIT {limit}
    """), params).fetchall()
    return rows


def get_concurrent_calls(conn, since: datetime | None) -> list:
    """Seconds where >1 call completed — potential slot contention."""
    params = {}
    since_clause = ""
    if since:
        since_clause = "WHERE created_at >= :since"
        params["since"] = since
    rows = conn.execute(text(f"""
        SELECT
            date_trunc('second', created_at) AS second,
            COUNT(*)                          AS call_count,
            STRING_AGG(DISTINCT task_id, ', ') AS tasks
        FROM budget_entries
        {since_clause}
        GROUP BY second
        HAVING COUNT(*) > 1
        ORDER BY call_count DESC
        LIMIT 20
    """), params).fetchall()
    return rows


def get_task_token_growth(conn, task_id: str) -> list:
    """Token growth per entry for a task (uses expenses for accurate counts)."""
    rows = conn.execute(text("""
        SELECT be.id, be.created_at, be.session_id,
               e.prompt_tokens, e.completion_tokens, be.tool_calls,
               be.prompt_message_count
        FROM budget_entries be
        LEFT JOIN expenses e ON e.budget_entry_id = be.id
        WHERE be.task_id = :tid
        ORDER BY be.id
    """), {"tid": task_id}).fetchall()
    return rows


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

SEP = "=" * 72


def section(title: str) -> None:
    print(f"\n{SEP}")
    print(f"  {title}")
    print(SEP)


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose LLM 500 errors")
    parser.add_argument("--hours",  type=float, default=2.0)
    parser.add_argument("--all",    action="store_true")
    parser.add_argument("--task",   default=None)
    parser.add_argument("--window", type=int, default=10)
    args = parser.parse_args()

    since: datetime | None = None
    if not args.all:
        since = _local_to_utc(datetime.now()) - timedelta(hours=args.hours)

    # -- Parse log -----------------------------------------------------------
    log = parse_log(_LOG_PATH, since)

    section(f"500 ERRORS IN LOG  (since {since or 'all time'})")
    if not log["errors"]:
        print("  None found in window.")
    else:
        for ts, code, reason in log["errors"]:
            print(f"  {ts}  HTTP {code}  {reason}")

    if log["bodies"]:
        print()
        print("  Error bodies (what the LLM server said):")
        for ts, code, body in log["bodies"]:
            print(f"  {ts}  {code}  {body[:200]}")
    else:
        print()
        print("  NOTE: No error bodies logged. Restart the server to start capturing them.")

    if log["recoveries"]:
        print()
        print("  Recoveries:")
        for ts, attempts in log["recoveries"]:
            print(f"  {ts}  recovered after {attempts} attempt(s)")

    # -- DB queries ----------------------------------------------------------
    with engine.connect() as conn:

        if log["errors"]:
            section(f"BUDGET ENTRIES NEAR EACH 500  (+/-{args.window}s)")
            seen_ids: set[int] = set()
            for ts, code, reason in log["errors"]:
                if ts is None:
                    continue
                rows = get_entries_near(conn, ts, args.window)
                if not rows:
                    print(f"\n  {ts}  HTTP {code} — no DB entries within +/-{args.window}s")
                    continue
                print(f"\n  {ts}  HTTP {code}")
                for eid, task_id, cat, pp, tg, tool_calls, sid, aname, pmc in rows:
                    if eid in seen_ids:
                        continue
                    seen_ids.add(eid)
                    flag = " <<< HEAVY" if (pp or 0) > 30_000 else ""
                    dtype = "delta" if pmc is not None else "legacy"
                    print(f"    entry {eid:>6}  {cat}  task={task_id}  [{dtype}]")
                    print(f"             prompt_tok={pp:,}  completion_tok={tg:,}"
                          f"  tools={tool_calls}  agent={aname}{flag}")

        section("TOP 15 SESSIONS BY PROMPT TOKENS")
        top_sessions = get_top_sessions_by_tokens(conn, limit=15, since=since)
        if not top_sessions:
            print("  (no expenses records in window)")
        for sid, tid, aname, ptok, ctok, turns, first in top_sessions:
            flag = " <<< LIKELY OVERFLOW" if (ptok or 0) > 40_000 else ""
            flag = " <<< WARN >20k tok"   if (ptok or 0) > 20_000 and not flag else flag
            sid_short = (str(sid)[:12] + "...") if sid and len(str(sid)) > 12 else sid
            print(f"  {sid_short}  {(ptok or 0):>10,} prompt tok  "
                  f"{turns:>4} turns  task={tid}  {flag}")

        section("SECONDS WITH >1 CONCURRENT CALL  (potential slot contention)")
        conc = get_concurrent_calls(conn, since)
        if not conc:
            print("  None.")
        else:
            print(f"  {'Second':<22} {'Count':>5}  Tasks")
            for second, count, tasks in conc:
                print(f"  {str(second):<22} {count:>5}  {str(tasks)[:60]}")

        if args.task:
            section(f"TOKEN GROWTH FOR TASK: {args.task}")
            rows = get_task_token_growth(conn, args.task)
            if not rows:
                print("  No entries found.")
            else:
                prev_tok = 0
                for eid, cat, sid, pp, tg, tool_calls, pmc in rows:
                    pp = pp or 0
                    delta = pp - prev_tok
                    flag = " <<<" if pp > 20_000 else ""
                    dtype = "delta" if pmc is not None else "legacy"
                    delta_str = f"+{delta:,}" if delta >= 0 else f"{delta:,}"
                    print(f"  entry {eid:>6}  {cat}  prompt={pp:>10,} tok  "
                          f"delta={delta_str:>10}  [{dtype}]{flag}")
                    prev_tok = pp

        top_tok = top_sessions[0][3] if top_sessions else 0

    section("ROOT CAUSE ANALYSIS")
    causes: list[str] = []

    if top_tok and top_tok > 40_000:
        causes.append(
            f"CONTEXT OVERFLOW: largest session sent ~{top_tok:,} tokens. "
            "llama.cpp returns 500 when a request exceeds --ctx-size. "
            "Fix: lower context_budget_ratio in maestro.ini, or increase --ctx-size."
        )
    elif top_tok and top_tok > 20_000:
        causes.append(
            f"LARGE CONTEXT: largest session is ~{top_tok:,} tokens. "
            "Getting close to typical llama.cpp limits."
        )

    if conc:
        max_conc = max(c for _, c, _ in conc)
        if max_conc >= 3:
            causes.append(
                f"SLOT CONTENTION: up to {max_conc} calls completed in the same second. "
                "Fix: raise --parallel on llama.cpp, or lower max_parallel_sessions in the UI."
            )

    if log["errors"] and not log["bodies"]:
        causes.append(
            "BLIND SPOT: 500 error bodies not logged (old server). "
            "Restart — the fix is live and will capture bodies going forward."
        )

    if not causes:
        causes.append(
            "No clear pattern in this window. Try --hours 24 or --all."
        )

    for i, c in enumerate(causes, 1):
        print(f"\n  [{i}] {c}")
    print()


if __name__ == "__main__":
    main()
