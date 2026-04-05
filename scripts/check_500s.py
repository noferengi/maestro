#!/usr/bin/env python
"""
scripts/check_500s.py
----------------------
Diagnose LLM 500 errors by correlating log timestamps with budget_entries
payload sizes.

Usage:
    venv/Scripts/python.exe scripts/check_500s.py              # last 2 hours
    venv/Scripts/python.exe scripts/check_500s.py --hours 12   # last 12 hours
    venv/Scripts/python.exe scripts/check_500s.py --all        # all time

What this shows
---------------
- Every 500/error event in the log file with its timestamp and (after today's
  fix) the actual error body from llama.cpp
- For each 500 cluster: the budget_entries whose calls landed within ±10 s,
  showing prompt sizes, estimated token counts, and task/agent context
- A summary table of the 10 largest prompts ever sent so you can spot
  runaway context growth
- A concurrency timeline: calls that overlapped within the same second
  (concurrent requests → slot exhaustion)

All output is ASCII-safe (Windows cp1252 terminal compatible).
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_SCRIPT_DIR)
_LOG_PATH = os.path.join(_ROOT, "logs", "maestro.log")
_DB_PATH  = os.path.join(_ROOT, "data", "kanban.db")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tok(chars: int) -> int:
    """Rough token estimate: 1 token ~ 4 chars."""
    return chars // 4


def _fmt_size(chars: int) -> str:
    tok = _tok(chars)
    if chars < 1024:
        return f"{chars}B / ~{tok}tok"
    elif chars < 1024 * 1024:
        return f"{chars/1024:.1f}KB / ~{tok:,}tok"
    else:
        return f"{chars/1024/1024:.1f}MB / ~{tok:,}tok"


def _parse_log_ts(ts_str: str) -> "datetime | None":
    """Parse '2026-04-04T18:05:27' from log lines.

    Log timestamps are in local time; DB stores UTC.  We convert here so
    all comparisons against DB values work correctly.
    """
    try:
        import time as _time
        local_dt = datetime.fromisoformat(ts_str)
        # utcoffset in seconds (negative west of UTC, positive east)
        utc_offset_s = -_time.timezone if not _time.daylight else -_time.altzone
        import datetime as _dt
        utc_dt = local_dt - _dt.timedelta(seconds=utc_offset_s)
        return utc_dt
    except (ValueError, Exception):
        return None


def _db_ts(ts_str: str) -> "datetime | None":
    """Parse SQLite created_at like '2026-04-04 22:08:36.663454' (UTC)."""
    try:
        return datetime.fromisoformat(ts_str.replace(" ", "T"))
    except ValueError:
        return None


def _local_to_utc(dt: "datetime") -> "datetime":
    """Convert a local naive datetime to UTC."""
    import time as _time
    import datetime as _dt
    utc_offset_s = -_time.timezone if not _time.daylight else -_time.altzone
    return dt - _dt.timedelta(seconds=utc_offset_s)

# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------

# Matches lines like:
#   2026-04-04T18:05:27 [INFO] httpx: HTTP Request: POST http://...  "HTTP/1.1 500 ..."
_HTTP_LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'\s+\[\w+\]\s+httpx:.*?"HTTP/1\.1\s+(?P<code>\d{3})\s+(?P<reason>[^"]*)"'
)

# Matches new-style WARNING lines (after our fix):
#   2026-04-04T18:05:27 [WARNING] app.agent.llm_client: LLM call to ... returned 500 — body: ...
_BODY_LINE_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'\s+\[WARNING\].*?returned (?P<code>\d{3}) -- body: (?P<body>.+)$'
)

# "back online" recovery lines
_RECOVER_RE = re.compile(
    r'^(?P<ts>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})'
    r'.*?back online.*?(?P<attempts>\d+) attempt'
)


def parse_log(path: str, since: "datetime | None") -> dict:
    """
    Returns:
        {
          'errors':    [(ts, code, reason)],
          'bodies':    [(ts, code, body)],       # only after logging fix
          'recoveries':[(ts, attempts)],
          'all_lines': [str],
        }
    """
    result = {"errors": [], "bodies": [], "recoveries": [], "all_lines": []}
    if not os.path.exists(path):
        print(f"[WARN] Log file not found: {path}")
        return result

    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.rstrip()
            result["all_lines"].append(line)

            m = _HTTP_LINE_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                code = int(m.group("code"))
                reason = m.group("reason").strip()
                if code >= 500:
                    if since is None or (ts and ts >= since):
                        result["errors"].append((ts, code, reason))
                continue

            m = _BODY_LINE_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                code = int(m.group("code"))
                body = m.group("body").strip()
                if code >= 500:
                    if since is None or (ts and ts >= since):
                        result["bodies"].append((ts, code, body))
                continue

            m = _RECOVER_RE.match(line)
            if m:
                ts = _parse_log_ts(m.group("ts"))
                attempts = int(m.group("attempts"))
                if since is None or (ts and ts >= since):
                    result["recoveries"].append((ts, attempts))

    return result


# ---------------------------------------------------------------------------
# DB queries
# ---------------------------------------------------------------------------

def get_entries_near(conn: sqlite3.Connection, ts: "datetime", window_s: int = 10) -> list:
    """Return budget_entries within ±window_s seconds of ts."""
    lo = ts.isoformat(sep=" ").replace("T", " ")
    # Cheap: just grab entries within a larger range and filter in Python
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, task_id, created_at,
               length(prompt_data)   AS prompt_len,
               length(response_data) AS resp_len,
               tool_calls
        FROM budget_entries
        WHERE created_at >= datetime(?, '-{w} seconds')
          AND created_at <= datetime(?, '+{w} seconds')
        ORDER BY created_at
    """.format(w=window_s), (lo, lo))
    return cursor.fetchall()


def get_top_prompts(conn: sqlite3.Connection, limit: int = 15) -> list:
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, task_id, created_at,
               length(prompt_data)   AS prompt_len,
               length(response_data) AS resp_len
        FROM budget_entries
        ORDER BY prompt_len DESC
        LIMIT ?
    """, (limit,))
    return cursor.fetchall()


def get_concurrent_calls(conn: sqlite3.Connection, since: "datetime | None") -> list:
    """Find seconds where >1 call completed (potential slot contention)."""
    since_str = since.isoformat(sep=" ") if since else "1970-01-01 00:00:00"
    cursor = conn.cursor()
    cursor.execute("""
        SELECT strftime('%Y-%m-%d %H:%M:%S', created_at) AS second,
               count(*) AS call_count,
               group_concat(DISTINCT task_id) AS tasks
        FROM budget_entries
        WHERE created_at >= ?
        GROUP BY second
        HAVING call_count > 1
        ORDER BY call_count DESC
        LIMIT 20
    """, (since_str,))
    return cursor.fetchall()


def get_prompt_growth(conn: sqlite3.Connection, task_id: str) -> list:
    """Return all entries for a task ordered by time, showing prompt growth."""
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, created_at,
               length(prompt_data)   AS prompt_len,
               length(response_data) AS resp_len,
               tool_calls
        FROM budget_entries
        WHERE task_id = ?
        ORDER BY created_at
    """, (task_id,))
    return cursor.fetchall()


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
    parser.add_argument("--hours", type=float, default=2.0,
                        help="Look back N hours (default: 2)")
    parser.add_argument("--all", action="store_true",
                        help="Analyse all time (ignores --hours)")
    parser.add_argument("--task", default=None,
                        help="Show prompt growth for a specific task ID")
    parser.add_argument("--window", type=int, default=10,
                        help="Seconds either side of a 500 to search DB (default: 10)")
    args = parser.parse_args()

    since: "datetime | None" = None
    if not args.all:
        import datetime as _dt
        # Use UTC throughout so log timestamps (converted) match DB timestamps
        since = _local_to_utc(datetime.now()) - _dt.timedelta(hours=args.hours)

    # ── Parse log ──────────────────────────────────────────────────────────
    log = parse_log(_LOG_PATH, since)

    section(f"500 ERRORS IN LOG  (since {since or 'all time'})")
    if not log["errors"]:
        print("  None found in window.")
    else:
        for ts, code, reason in log["errors"]:
            print(f"  {ts}  HTTP {code}  {reason}")

    if log["bodies"]:
        print()
        print("  Error bodies (from WARNING log — what the LLM server said):")
        for ts, code, body in log["bodies"]:
            print(f"  {ts}  {code}  {body[:200]}")
    else:
        print()
        print("  NOTE: No error bodies found. This is expected if the server was")
        print("  started BEFORE today's fix (which raised 500 logging to WARNING).")
        print("  Restart the server to start capturing error bodies going forward.")

    if log["recoveries"]:
        print()
        print("  Recoveries (each = one 500 -> retry -> success cycle):")
        for ts, attempts in log["recoveries"]:
            print(f"  {ts}  recovered after {attempts} attempt(s)")

    # ── DB correlation ─────────────────────────────────────────────────────
    if not os.path.exists(_DB_PATH):
        print(f"\n[WARN] DB not found: {_DB_PATH}")
        return

    conn = sqlite3.connect(_DB_PATH)

    if log["errors"]:
        section("BUDGET ENTRIES NEAR EACH 500  (+/- %ds)" % args.window)
        seen_ids: set[int] = set()
        for ts, code, reason in log["errors"]:
            if ts is None:
                continue
            rows = get_entries_near(conn, ts, args.window)
            if not rows:
                print(f"\n  {ts}  HTTP {code} — no DB entries within +/-{args.window}s")
                continue
            print(f"\n  {ts}  HTTP {code}")
            for eid, task_id, cat, plen, rlen, tcalls in rows:
                if eid in seen_ids:
                    continue
                seen_ids.add(eid)
                flag = " <<< OVERSIZED" if _tok(plen or 0) > 30_000 else ""
                flag = " <<< VERY LARGE" if _tok(plen or 0) > 20_000 else flag
                print(f"    entry {eid:>6}  {cat}  task={task_id}")
                print(f"             prompt={_fmt_size(plen or 0)}  "
                      f"resp={_fmt_size(rlen or 0)}  tools={tcalls}{flag}")

    # ── Top prompts ever ───────────────────────────────────────────────────
    section("TOP 15 LARGEST PROMPTS EVER SENT")
    top = get_top_prompts(conn, 15)
    if not top:
        print("  (no budget entries)")
    for eid, task_id, cat, plen, rlen in top:
        flag = " <<< LIKELY OVERFLOW" if _tok(plen or 0) > 40_000 else ""
        flag = " <<< WARN >20k tok"   if _tok(plen or 0) > 20_000 and not flag else flag
        print(f"  entry {eid:>6}  {_fmt_size(plen or 0):>30}  task={task_id}  {cat}{flag}")

    # ── Concurrent calls ───────────────────────────────────────────────────
    section("SECONDS WITH >1 CONCURRENT CALL COMPLETING  (potential slot contention)")
    conc = get_concurrent_calls(conn, since)
    if not conc:
        print("  None.")
    else:
        print(f"  {'Second':<22} {'Count':>5}  Tasks")
        for second, count, tasks in conc:
            print(f"  {second:<22} {count:>5}  {tasks}")

    # ── Per-task growth ────────────────────────────────────────────────────
    if args.task:
        section(f"PROMPT GROWTH FOR TASK: {args.task}")
        rows = get_prompt_growth(conn, args.task)
        if not rows:
            print("  No entries found.")
        else:
            prev_len = 0
            for eid, cat, plen, rlen, tcalls in rows:
                delta = (plen or 0) - prev_len
                flag = " <<<" if _tok(plen or 0) > 20_000 else ""
                delta_str = f"+{_tok(delta):,}tok" if delta > 0 else f"{_tok(delta):,}tok"
                print(f"  entry {eid:>6}  {cat}  "
                      f"prompt={_fmt_size(plen or 0):>30}  "
                      f"delta={delta_str:>12}  tools={tcalls}{flag}")
                prev_len = plen or 0

    # ── Infer root cause ───────────────────────────────────────────────────
    section("ROOT CAUSE ANALYSIS")
    top_tok = _tok(top[0][3] or 0) if top else 0

    causes: list[str] = []

    if top_tok > 40_000:
        causes.append(
            f"CONTEXT OVERFLOW: largest prompt is ~{top_tok:,} tokens. "
            "llama.cpp returns 500 when a request exceeds the server's --ctx-size. "
            "Fix: lower RESEARCH_CONTEXT_BUDGET_RATIO in maestro.ini, or increase "
            "--ctx-size on the llama.cpp server."
        )
    elif top_tok > 20_000:
        causes.append(
            f"LARGE CONTEXT: largest prompt is ~{top_tok:,} tokens. "
            "Getting close to typical llama.cpp limits. Monitor for overflow."
        )

    if conc:
        max_conc = max(c for _, c, _ in conc)
        if max_conc >= 3:
            causes.append(
                f"SLOT CONTENTION: up to {max_conc} calls completed in the same second. "
                "If llama.cpp --parallel < this count, extra requests get a 500. "
                "Fix: raise --parallel on the server, or lower max_parallel_sessions "
                "for this LLM endpoint in the board UI."
            )

    if len(log["errors"]) > 0 and not log["bodies"]:
        causes.append(
            "BLIND SPOT: 500 error bodies were not logged (old server version). "
            "Restart the server — the logging fix is now live and will show "
            "exactly what llama.cpp said for every future 500."
        )

    if not causes:
        causes.append(
            "No clear pattern detected in this time window. "
            "Try --hours 24 or --all to see more history."
        )

    for i, c in enumerate(causes, 1):
        print(f"\n  [{i}] {c}")

    conn.close()
    print()


if __name__ == "__main__":
    main()
