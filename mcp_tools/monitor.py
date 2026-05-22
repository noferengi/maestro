"""Blocking monitor tool — observes Maestro activity over a time window."""

import configparser
import time
import json
import os
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from .helpers import get_conn, extract_response_fields, parse_gate_checks

_INI_PATH = Path(__file__).parent.parent / "maestro.ini"
_LOG_PATH = Path(__file__).parent.parent / "logs" / "maestro.log"


def _load_monitor_config() -> dict:
    """Read [monitor] section from maestro.ini with built-in fallbacks."""
    cfg = configparser.ConfigParser(delimiters=("=",), interpolation=None)
    cfg.optionxform = str  # type: ignore[assignment]
    cfg.read(str(_INI_PATH), encoding="utf-8")

    def _int(key: str, fallback: int) -> int:
        return int(cfg.get("monitor", key, fallback=str(fallback)))

    def _float(key: str, fallback: float) -> float:
        return float(cfg.get("monitor", key, fallback=str(fallback)))

    return {
        "duration_seconds": _int("duration_seconds", 300),
        "poll_interval_seconds": _int("poll_interval_seconds", 30),
        "rapid_cycling_window_seconds": _int("rapid_cycling_window_seconds", 90),
        "rapid_cycling_min_entries": _int("rapid_cycling_min_entries", 3),
        "rapid_cycling_max_prompt_cost": _int("rapid_cycling_max_prompt_cost", 700),
        "zombie_idle_minutes": _int("zombie_idle_minutes", 10),
        "tool_call_storm_rate": _float("tool_call_storm_rate", 5.0),
        "tool_call_storm_min_entries": _int("tool_call_storm_min_entries", 3),
    }


def monitor(
    duration_seconds: int = None,
    poll_interval_seconds: int = None,
    project: str = None,
    watch_task_ids: list = None,
) -> dict:
    """
    Block for duration_seconds, polling the DB and Log every poll_interval_seconds.
    Returns a diff-style report of everything that happened: new LLM calls,
    session starts/completions, stage changes, log errors, and detected bad patterns.

    pattern detection:
      rapid_cycling    — task with >= rapid_cycling_min_entries cheap calls in a sliding window
      token_limited    — finish_reason='length' with empty content_preview
      zombie_session   — open session at window-end with no budget entry in last zombie_idle_minutes
      stage_thrash     — task whose type changed >=2 times in the window
      tool_call_storm  — task averaging >tool_call_storm_rate tool-call entries per minute
      repetitive_tools — task calling the same tool with identical args multiple times
      gate_looping     — task with multiple gate rejections in the window
    """
    cfg = _load_monitor_config()
    if duration_seconds is None:
        duration_seconds = cfg["duration_seconds"]
    if poll_interval_seconds is None:
        poll_interval_seconds = cfg["poll_interval_seconds"]

    start_mono = time.monotonic()
    deadline = start_mono + duration_seconds
    start_wall = datetime.now(timezone.utc).isoformat()

    # --- log watermark ---
    log_offset = 0
    if _LOG_PATH.exists():
        log_offset = _LOG_PATH.stat().st_size

    # --- initial watermark snapshot ---
    conn = get_conn()
    try:
        row = conn.execute("SELECT MAX(id) FROM budget_entries").fetchone()
        max_budget_id: int = row[0] or 0

        row = conn.execute("SELECT MAX(id) FROM agent_sessions").fetchone()
        max_session_id: int = row[0] or 0

        open_sessions: dict[str, dict] = {}
        for r in conn.execute(
            "SELECT s.task_id, s.agent_type, s.started_at, t.title "
            "FROM agent_sessions s JOIN tasks t ON s.task_id=t.id "
            "WHERE s.ended_at IS NULL "
            "AND s.id=(SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id AND ended_at IS NULL)"
        ).fetchall():
            open_sessions[r["task_id"]] = {
                "agent_type": r["agent_type"],
                "started_at": r["started_at"],
                "title": r["title"],
            }

        task_types: dict[str, tuple] = {}
        q = (
            "SELECT t.id, t.title, t.type FROM tasks t "
            "LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE t.is_active AND t.type NOT IN ('idea','architecture')"
        )
        params: list = []
        if project:
            q += " AND p.name=?"
            params.append(project)
        for r in conn.execute(q, params).fetchall():
            task_types[r["id"]] = (r["title"], r["type"])
    finally:
        conn.close()

    # --- accumulators ---
    new_budget_entries: list[dict] = []
    session_starts: list[dict] = []
    session_completions: list[dict] = []
    type_changes: list[dict] = []
    log_events: list[dict] = []
    gate_rejections: list[dict] = []
    poll_count = 0

    # --- poll loop ---
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval_seconds, remaining))
        poll_count += 1

        conn = get_conn()
        try:
            # log tailing
            if _LOG_PATH.exists():
                with open(_LOG_PATH, "r", encoding="utf-8", errors="replace") as f:
                    f.seek(log_offset)
                    new_lines = f.readlines()
                    log_offset = f.tell()
                    for line in new_lines:
                        # Catch capacity limits, thrashing, and errors
                        if "at capacity" in line or "thrashing" in line or "ERROR" in line or "HTTP 500" in line:
                            log_events.append({"line": line.strip(), "time": datetime.now(timezone.utc).isoformat()})

            # new budget entries
            rows = conn.execute(
                "SELECT be.id, be.task_id, be.agent_name, be.prompt_cost, "
                "be.generation_cost, be.tool_calls, be.response_data, be.created_at, "
                "t.title "
                "FROM budget_entries be LEFT JOIN tasks t ON be.task_id=t.id "
                "WHERE be.id > ? ORDER BY be.id",
                (max_budget_id,),
            ).fetchall()
            for r in rows:
                fields = extract_response_fields(r["response_data"])
                entry = {
                    "id": r["id"],
                    "task_id": r["task_id"],
                    "title": r["title"],
                    "agent_name": r["agent_name"],
                    "prompt_cost": r["prompt_cost"],
                    "generation_cost": r["generation_cost"],
                    "tool_calls": r["tool_calls"],
                    "created_at": r["created_at"],
                    **fields,
                }
                if project and r["task_id"] not in task_types:
                    continue
                new_budget_entries.append(entry)
            if rows:
                max_budget_id = rows[-1]["id"]

            # session completions
            closed_ids = list(open_sessions.keys())
            if closed_ids:
                placeholders = ",".join("?" * len(closed_ids))
                for r in conn.execute(
                    f"SELECT s.task_id, s.agent_type, s.exit_reason, s.exit_summary, "
                    f"s.ended_at, t.title "
                    f"FROM agent_sessions s JOIN tasks t ON s.task_id=t.id "
                    f"WHERE s.task_id IN ({placeholders}) AND s.ended_at IS NOT NULL "
                    f"AND s.ended_at >= ? "
                    f"AND s.id=(SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id)",
                    closed_ids + [start_wall],
                ).fetchall():
                    tid = r["task_id"]
                    if tid in open_sessions:
                        session_completions.append({
                            "task_id": tid,
                            "title": r["title"],
                            "agent_type": r["agent_type"],
                            "exit_reason": r["exit_reason"],
                            "exit_summary": (r["exit_summary"] or "")[:300],
                            "ended_at": r["ended_at"],
                        })
                        del open_sessions[tid]

            # new session starts
            for r in conn.execute(
                "SELECT s.task_id, s.agent_type, s.started_at, t.title "
                "FROM agent_sessions s JOIN tasks t ON s.task_id=t.id "
                "WHERE s.id > ? AND s.ended_at IS NULL",
                (max_session_id,),
            ).fetchall():
                tid = r["task_id"]
                if project and tid not in task_types:
                    continue
                open_sessions[tid] = {
                    "agent_type": r["agent_type"],
                    "started_at": r["started_at"],
                    "title": r["title"],
                }
                session_starts.append({
                    "task_id": tid,
                    "title": r["title"],
                    "agent_type": r["agent_type"],
                    "started_at": r["started_at"],
                })

            row = conn.execute("SELECT MAX(id) FROM agent_sessions").fetchone()
            max_session_id = row[0] or max_session_id

            # task type changes
            for r in conn.execute(q, params).fetchall():
                tid = r["id"]
                new_type = r["type"]
                if tid in task_types and task_types[tid][1] != new_type:
                    type_changes.append({
                        "task_id": tid,
                        "title": r["title"],
                        "from": task_types[tid][1],
                        "to": new_type,
                        "at": datetime.now(timezone.utc).isoformat(),
                    })
                task_types[tid] = (r["title"], new_type)

            # gate rejections
            for r in conn.execute(
                "SELECT task_id, transition, outcome, created_at FROM transition_results "
                "WHERE created_at >= ? AND outcome='rejected'",
                (start_wall,),
            ).fetchall():
                if project and r["task_id"] not in task_types:
                    continue
                gate_rejections.append(dict(r))

        finally:
            conn.close()

    end_wall = datetime.now(timezone.utc).isoformat()

    # --- background job check ---
    stuck_jobs: list[dict] = []
    conn = get_conn()
    try:
        # Check research, file_summary, and arch_gen jobs
        for table in ["research_jobs", "file_summary_jobs", "arch_gen_jobs"]:
            try:
                for r in conn.execute(
                    f"SELECT id, status, created_at FROM {table} WHERE status = 'running'"
                ).fetchall():
                    created_at = r["created_at"]
                    # If older than zombie threshold
                    try:
                        last_dt = datetime.fromisoformat(created_at.replace(" ", "T"))
                        if last_dt.tzinfo is None:
                            last_dt = last_dt.replace(tzinfo=timezone.utc)
                        idle_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                        if idle_min > cfg["zombie_idle_minutes"]:
                            stuck_jobs.append({
                                "table": table,
                                "id": r["id"],
                                "idle_minutes": round(idle_min, 1),
                                "created_at": created_at
                            })
                    except Exception:
                        pass
            except Exception:
                pass # Table might not exist or schema differs
    finally:
        conn.close()

    # --- end-state stuck detection ---
    stuck_candidates: list[dict] = []
    stagnant_tasks: list[dict] = []
    conn = get_conn()
    try:
        # 1. Zombie Sessions (no activity in open session)
        for tid, info in open_sessions.items():
            row = conn.execute(
                "SELECT created_at FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if row is None:
                stuck_candidates.append({**info, "task_id": tid, "idle_minutes": None,
                                          "note": "no budget entries"})
            else:
                try:
                    last_dt = datetime.fromisoformat(row["created_at"].replace(" ", "T"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    idle = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if idle > cfg["zombie_idle_minutes"]:
                        stuck_candidates.append({
                            **info, "task_id": tid,
                            "idle_minutes": round(idle, 1),
                            "note": f"no LLM call in {round(idle, 1)} min",
                        })
                except Exception:
                    pass

        # 2. Stagnant Tasks (in same stage for > 24h)
        for tid, (title, ttype) in task_types.items():
            if ttype in ("completed", "accepted", "cancelled"):
                continue
            # Check last transition_result or history entry for this task
            row = conn.execute(
                "SELECT created_at FROM transition_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (tid,)
            ).fetchone()
            if row:
                try:
                    ts = datetime.fromisoformat(row["created_at"].replace(" ", "T"))
                    if ts.tzinfo is None: ts = ts.replace(tzinfo=timezone.utc)
                    age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
                    if age_h > 24:
                        stagnant_tasks.append({
                            "task_id": tid, "title": title, "type": ttype,
                            "age_hours": round(age_h, 1), "note": "stagnant stage"
                        })
                except Exception: pass

        # 3. Merge & Subdivision Failures
        merge_fails = conn.execute(
            "SELECT task_id, status, error_detail, created_at FROM merge_records "
            "WHERE created_at >= ? AND status NOT IN ('merged', 'virtual_passed')",
            (start_wall,)
        ).fetchall()
        
        subdiv_fails = conn.execute(
            "SELECT parent_task_id, status, created_at FROM subdivision_records "
            "WHERE created_at >= ? AND status = 'failed'",
            (start_wall,)
        ).fetchall()

    finally:
        conn.close()

    # --- pattern analysis ---
    patterns = _analyze_patterns(new_budget_entries, open_sessions, type_changes, gate_rejections, cfg)

    # --- inline diagnostics for all flagged tasks ---
    flagged_ids: set[str] = set()
    for flag_list in patterns.values():
        for item in flag_list:
            if isinstance(item, dict) and "task_id" in item:
                flagged_ids.add(item["task_id"])
    for f in merge_fails: flagged_ids.add(f["task_id"])
    for f in subdiv_fails: flagged_ids.add(f["parent_task_id"])

    inline_diagnostics: dict[str, dict] = {}
    if flagged_ids:
        conn = get_conn()
        try:
            for tid in flagged_ids:
                inline_diagnostics[tid] = _compact_task_diagnosis(tid, conn)
        finally:
            conn.close()

    # summary line
    parts = []
    if session_completions:
        parts.append(f"{len(session_completions)} completion(s)")
    if session_starts:
        parts.append(f"{len(session_starts)} session start(s)")
    if stuck_candidates:
        parts.append(f"{len(stuck_candidates)} stuck tasks")
    if stagnant_tasks:
        parts.append(f"{len(stagnant_tasks)} stagnant tasks")
    if stuck_jobs:
        parts.append(f"{len(stuck_jobs)} stuck background jobs")
    if merge_fails:
        parts.append(f"{len(merge_fails)} merge fail(s)")
    if subdiv_fails:
        parts.append(f"{len(subdiv_fails)} subdivision fail(s)")
    if type_changes:
        parts.append(f"{len(type_changes)} stage change(s)")
    if log_events:
        parts.append(f"{len(log_events)} log alert(s)")
    flagged = sum(len(v) for v in patterns.values())
    if flagged:
        parts.append(f"{flagged} pattern flag(s)")
    summary = ", ".join(parts) if parts else "quiet window — no notable events"

    return {
        "window": {
            "start": start_wall,
            "end": end_wall,
            "duration_s": round(time.monotonic() - start_mono),
            "polls": poll_count,
        },
        "new_budget_entries": list(reversed(new_budget_entries)),
        "session_starts": session_starts,
        "session_completions": session_completions,
        "type_changes": type_changes,
        "log_alerts": log_events,
        "stuck_jobs": stuck_jobs,
        "merge_failures": [dict(f) for f in merge_fails],
        "subdivision_failures": [dict(f) for f in subdiv_fails],
        "end_state": {
            "open_sessions": [
                {"task_id": tid, **info} for tid, info in open_sessions.items()
            ],
            "stuck_candidates": stuck_candidates,
            "stagnant_tasks": stagnant_tasks,
        },
        "patterns": patterns,
        "inline_diagnostics": inline_diagnostics,
        "summary": summary,
    }


def _compact_task_diagnosis(task_id: str, conn) -> dict:
    """Minimal snapshot for a flagged task. Included inline in monitor reports."""
    last_session = conn.execute(
        "SELECT agent_type, exit_reason, exit_summary FROM agent_sessions "
        "WHERE task_id=? AND ended_at IS NOT NULL ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    last_entry = conn.execute(
        "SELECT agent_name, response_data, created_at FROM budget_entries "
        "WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    gate_row = conn.execute(
        "SELECT outcome, vote_summary FROM transition_results "
        "WHERE task_id=? AND transition='planning_gate' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()

    result: dict = {}
    if last_session:
        summary = (last_session["exit_summary"] or "")[:150]
        result["last_exit"] = f"{last_session['exit_reason']}: {summary}".rstrip(": ")
    if last_entry:
        fields = extract_response_fields(last_entry["response_data"])
        result["last_llm_agent"] = last_entry["agent_name"]
        result["last_llm_at"] = last_entry["created_at"]
        result["last_finish_reason"] = fields.get("finish_reason", "")
        preview = fields.get("content_preview") or fields.get("reasoning_preview", "")
        if preview:
            result["last_content"] = preview[:150]
    if gate_row and gate_row["outcome"] == "rejected":
        checks = parse_gate_checks(gate_row["vote_summary"])
        failing = [c["name"] for c in checks if not c.get("passed")]
        if failing:
            result["gate_failing_checks"] = failing
    return result


def _analyze_patterns(
    entries: list[dict],
    open_sessions: dict,
    type_changes: list[dict],
    gate_rejections: list[dict],
    cfg: dict,
) -> dict:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("task_id"):
            by_task[e["task_id"]].append(e)

    rapid_cycling: list[dict] = []
    token_limited: list[dict] = []
    tool_call_storms: list[dict] = []
    repetitive_tools: list[dict] = []
    gate_looping: list[dict] = []

    rc_window = cfg["rapid_cycling_window_seconds"]
    rc_min = cfg["rapid_cycling_min_entries"]
    rc_max_cost = cfg["rapid_cycling_max_prompt_cost"]
    storm_rate = cfg["tool_call_storm_rate"]
    storm_min = cfg["tool_call_storm_min_entries"]
    zombie_thresh = cfg["zombie_idle_minutes"]

    for tid, task_entries in by_task.items():
        title = task_entries[0].get("title", tid)

        # token_limited: any entry with finish_reason='length' + empty content
        for e in task_entries:
            if e.get("finish_reason") == "length" and not e.get("content_preview"):
                token_limited.append({
                    "task_id": tid, "title": title,
                    "entry_id": e["id"], "agent_name": e.get("agent_name"),
                    "created_at": e["created_at"],
                })

        # repetitive_tools: calling same tool + same args multiple times
        tool_counts: dict[str, int] = defaultdict(int)
        for e in task_entries:
            tc_str = e.get("tool_calls")
            if tc_str:
                tool_counts[tc_str] += 1
        
        for tc_str, count in tool_counts.items():
            if count >= 3:
                try:
                    # Try to get a friendly label for the tool
                    calls = json.loads(tc_str)
                    if calls and isinstance(calls, list):
                        call = calls[0]
                        tool_name = call.get("function", {}).get("name", "unknown")
                        repetitive_tools.append({
                            "task_id": tid, "title": title,
                            "tool": tool_name, "count": count,
                            "args_preview": str(call.get("function", {}).get("arguments", ""))[:100]
                        })
                except Exception:
                    pass

        # rapid_cycling: >= rc_min cheap entries in any rc_window seconds
        cheap = [e for e in task_entries if (e.get("prompt_cost") or 0) < rc_max_cost]
        if len(cheap) >= rc_min:
            try:
                cheap_sorted = sorted(
                    cheap,
                    key=lambda e: datetime.fromisoformat(e["created_at"].replace("T", " ")) # Fixed ISO format
                )
                # Helper to parse created_at consistently
                def parse_ts(ts_str):
                    try:
                        dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                    except:
                        return datetime.now(timezone.utc)

                for i in range(len(cheap_sorted) - (rc_min - 1)):
                    t0 = parse_ts(cheap_sorted[i]["created_at"])
                    tn = parse_ts(cheap_sorted[i + rc_min - 1]["created_at"])
                    span = (tn - t0).total_seconds()
                    if span <= rc_window:
                        rapid_cycling.append({
                            "task_id": tid, "title": title,
                            "cheap_entries": len(cheap),
                            "window_seconds": round(span),
                        })
                        break
            except Exception:
                pass

        # tool_call_storm: avg > storm_rate tool-call entries per minute
        tc_entries = [e for e in task_entries if e.get("has_tool_calls")]
        if len(tc_entries) >= storm_min:
            try:
                def parse_ts(ts_str):
                    try:
                        dt = datetime.fromisoformat(ts_str.replace(" ", "T"))
                        if dt.tzinfo is None:
                            dt = dt.replace(tzinfo=timezone.utc)
                        return dt
                    except:
                        return datetime.now(timezone.utc)

                times = sorted(parse_ts(e["created_at"]) for e in tc_entries)
                span_min = max((times[-1] - times[0]).total_seconds() / 60, 0.5)
                rate = len(tc_entries) / span_min
                if rate > storm_rate:
                    tool_call_storms.append({
                        "task_id": tid, "title": title,
                        "tool_call_entries": len(tc_entries),
                        "rate_per_min": round(rate, 1),
                    })
            except Exception:
                pass

    # gate_looping: multiple rejections in this window
    rejection_counts = defaultdict(int)
    for gr in gate_rejections:
        rejection_counts[gr["task_id"]] += 1
    for tid, count in rejection_counts.items():
        if count >= 2:
            gate_looping.append({"task_id": tid, "rejections": count})

    # stage_thrash: type changed >=2 times
    thrash_counts: dict[str, list] = defaultdict(list)
    for tc in type_changes:
        thrash_counts[tc["task_id"]].append(tc)
    stage_thrash = [
        {"task_id": tid, "title": changes[0]["title"], "changes": changes}
        for tid, changes in thrash_counts.items()
        if len(changes) >= 2
    ]

    # zombie_sessions: open sessions with no LLM call in zombie_thresh minutes
    zombie_sessions = [
        {"task_id": tid, **info}
        for tid, info in open_sessions.items()
        if info.get("idle_minutes") is not None and info["idle_minutes"] > zombie_thresh
    ]

    return {
        "rapid_cycling": rapid_cycling,
        "token_limited": token_limited,
        "zombie_sessions": zombie_sessions,
        "stage_thrash": stage_thrash,
        "tool_call_storms": tool_call_storms,
        "repetitive_tools": repetitive_tools,
        "gate_looping": gate_looping,
    }
