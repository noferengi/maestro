"""Blocking monitor tool — observes Maestro activity over a time window."""

import configparser
import time
from datetime import datetime, timezone
from collections import defaultdict
from pathlib import Path
from .helpers import get_conn, extract_response_fields

_INI_PATH = Path(__file__).parent.parent / "maestro.ini"


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
        "tool_call_storm_rate": _float("tool_call_storm_rate", 2.0),
        "tool_call_storm_min_entries": _int("tool_call_storm_min_entries", 3),
    }


def monitor(
    duration_seconds: int = None,
    poll_interval_seconds: int = None,
    project: str = None,
    watch_task_ids: list = None,
) -> dict:
    """
    Block for duration_seconds, polling the DB every poll_interval_seconds.
    Returns a diff-style report of everything that happened: new LLM calls,
    session starts/completions, stage changes, and detected bad patterns.

    Defaults are read from the [monitor] section of maestro.ini; explicit
    parameters override them. Designed for /loop use: call once per iteration,
    review the report, take corrective actions, then call again.

    pattern detection:
      rapid_cycling    — task with >= rapid_cycling_min_entries cheap calls in a sliding window
      token_limited    — finish_reason='length' with empty content_preview
      zombie_session   — open session at window-end with no budget entry in last zombie_idle_minutes
      stage_thrash     — task whose type changed >=2 times in the window
      tool_call_storm  — task averaging >tool_call_storm_rate tool-call entries per minute
    """
    cfg = _load_monitor_config()
    if duration_seconds is None:
        duration_seconds = cfg["duration_seconds"]
    if poll_interval_seconds is None:
        poll_interval_seconds = cfg["poll_interval_seconds"]

    start_mono = time.monotonic()
    deadline = start_mono + duration_seconds
    start_wall = datetime.now(timezone.utc).isoformat()

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
            "WHERE t.is_active=1 AND t.type NOT IN ('idea','architecture')"
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
                if project:
                    # filter: only entries belonging to the requested project's tasks
                    pass  # project filter applied via task_types membership check below
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

            # session completions: sessions that were open and closed DURING this window
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

        finally:
            conn.close()

    end_wall = datetime.now(timezone.utc).isoformat()

    # --- end-state stuck detection ---
    stuck_candidates: list[dict] = []
    conn = get_conn()
    try:
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
    finally:
        conn.close()

    # --- pattern analysis ---
    patterns = _analyze_patterns(new_budget_entries, open_sessions, type_changes, cfg)

    # summary line
    parts = []
    if session_completions:
        parts.append(f"{len(session_completions)} completion(s)")
    if session_starts:
        parts.append(f"{len(session_starts)} session start(s)")
    if stuck_candidates:
        parts.append(f"{len(stuck_candidates)} stuck")
    if type_changes:
        parts.append(f"{len(type_changes)} stage change(s)")
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
        "end_state": {
            "open_sessions": [
                {"task_id": tid, **info} for tid, info in open_sessions.items()
            ],
            "stuck_candidates": stuck_candidates,
        },
        "patterns": patterns,
        "summary": summary,
    }


def _analyze_patterns(
    entries: list[dict],
    open_sessions: dict,
    type_changes: list[dict],
    cfg: dict,
) -> dict:
    by_task: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        if e.get("task_id"):
            by_task[e["task_id"]].append(e)

    rapid_cycling: list[dict] = []
    token_limited: list[dict] = []
    tool_call_storms: list[dict] = []

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

        # rapid_cycling: >= rc_min cheap entries in any rc_window seconds
        cheap = [e for e in task_entries if (e.get("prompt_cost") or 0) < rc_max_cost]
        if len(cheap) >= rc_min:
            try:
                cheap_sorted = sorted(
                    cheap,
                    key=lambda e: datetime.fromisoformat(e["created_at"].replace(" ", "T")),
                )
                for i in range(len(cheap_sorted) - (rc_min - 1)):
                    t0 = datetime.fromisoformat(cheap_sorted[i]["created_at"].replace(" ", "T"))
                    tn = datetime.fromisoformat(cheap_sorted[i + rc_min - 1]["created_at"].replace(" ", "T"))
                    if t0.tzinfo is None:
                        t0 = t0.replace(tzinfo=timezone.utc)
                    if tn.tzinfo is None:
                        tn = tn.replace(tzinfo=timezone.utc)
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
                times = sorted(
                    datetime.fromisoformat(e["created_at"].replace(" ", "T"))
                    for e in tc_entries
                )
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
    }
