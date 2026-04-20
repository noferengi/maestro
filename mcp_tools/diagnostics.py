"""Read-only diagnostic tools for TheMaestro."""

import json
from datetime import datetime, timezone
from .helpers import (
    get_conn, extract_response_fields, parse_gate_checks,
    parse_json_field, DISPATCHABLE_TYPES,
)


def diagnose_task(task_id: str) -> dict:
    """
    Complete diagnostic snapshot for a single task.

    Returns current type, active/recent agent sessions, last 15 budget entries
    with finish_reason pre-extracted, planning gate history, correction agent
    history, and an activity_status summary. Replaces 4+ separate DB queries.
    """
    conn = get_conn()
    try:
        task_row = conn.execute(
            "SELECT t.id, t.title, t.type, p.name AS project, t.description, t.prerequisites "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id WHERE t.id=?",
            (task_id,),
        ).fetchone()
        if not task_row:
            return {"error": f"Task '{task_id}' not found."}

        task = {
            "id": task_row["id"],
            "title": task_row["title"],
            "type": task_row["type"],
            "project": task_row["project"],
            "description": (task_row["description"] or "")[:600],
            "prerequisites": parse_json_field(task_row["prerequisites"]),
        }

        # Active sessions (no ended_at)
        active_rows = conn.execute(
            "SELECT agent_type, started_at FROM agent_sessions "
            "WHERE task_id=? AND ended_at IS NULL ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        active_sessions = [dict(r) for r in active_rows]

        # Recent completed sessions (last 10)
        recent_rows = conn.execute(
            "SELECT agent_type, exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 10",
            (task_id,),
        ).fetchall()
        recent_sessions = [dict(r) for r in recent_rows]

        # Latest planning_result
        pr = conn.execute(
            "SELECT id, status, correction_attempts, gate_checks, created_at "
            "FROM planning_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        planning = None
        if pr:
            raw_checks = parse_json_field(pr["gate_checks"])
            planning = {
                "result_id": pr["id"],
                "status": pr["status"],
                "correction_attempts": pr["correction_attempts"],
                "gate_checks": raw_checks if isinstance(raw_checks, list) else [],
                "created_at": pr["created_at"],
            }

        # Gate transition history (last 5)
        gate_rows = conn.execute(
            "SELECT outcome, vote_summary, created_at FROM transition_results "
            "WHERE task_id=? AND transition='planning_gate' ORDER BY id DESC LIMIT 5",
            (task_id,),
        ).fetchall()
        gate_history = []
        for g in gate_rows:
            checks = parse_gate_checks(g["vote_summary"])
            gate_history.append({
                "outcome": g["outcome"],
                "created_at": g["created_at"],
                "checks": checks,
            })

        # All transition history (last 8, for full pipeline picture)
        tr_rows = conn.execute(
            "SELECT transition, outcome, substr(vote_summary, 1, 300), created_at "
            "FROM transition_results WHERE task_id=? ORDER BY id DESC LIMIT 8",
            (task_id,),
        ).fetchall()
        transitions = [
            {"transition": r[0], "outcome": r[1], "summary_preview": r[2], "created_at": r[3]}
            for r in tr_rows
        ]

        # Budget trace (last 15, finish_reason extracted)
        budget_rows = conn.execute(
            "SELECT id, agent_name, prompt_cost, generation_cost, response_data, created_at "
            "FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 15",
            (task_id,),
        ).fetchall()
        budget_trace = []
        for b in budget_rows:
            fields = extract_response_fields(b["response_data"])
            budget_trace.append({
                "id": b["id"],
                "agent_name": b["agent_name"],
                "prompt_cost": b["prompt_cost"],
                "generation_cost": b["generation_cost"],
                "created_at": b["created_at"],
                **fields,
            })

        # Correction agent sessions
        corr_rows = conn.execute(
            "SELECT exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND agent_type='planning_correction' "
            "ORDER BY id DESC LIMIT 5",
            (task_id,),
        ).fetchall()
        correction_sessions = [dict(r) for r in corr_rows]

        # Activity status
        if active_sessions:
            if budget_trace:
                last_entry_time = budget_trace[0]["created_at"]
                activity_status = f"active — last LLM call at {last_entry_time}"
            else:
                activity_status = "active — no budget entries yet (survey or waiting for slot)"
        else:
            activity_status = "idle"

        return {
            "task": task,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "transitions": transitions,
            "planning": planning,
            "gate_history": gate_history,
            "budget_trace": budget_trace,
            "correction_sessions": correction_sessions,
            "activity_status": activity_status,
        }
    finally:
        conn.close()


def get_scheduler_state() -> dict:
    """
    Overview of the scheduler: what's running, what's in each pipeline stage,
    recent completions, and stuck candidates (active session but no recent LLM call).
    """
    conn = get_conn()
    try:
        # Active agent sessions — one per task, latest session only (avoids zombie duplicates)
        active_rows = conn.execute(
            "SELECT task_id, agent_type, started_at FROM agent_sessions s "
            "WHERE ended_at IS NULL "
            "AND id = (SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id AND ended_at IS NULL) "
            "ORDER BY id DESC",
        ).fetchall()
        active_sessions = [dict(r) for r in active_rows]
        active_task_ids = {r["task_id"] for r in active_rows}

        # All pipeline tasks by type
        task_rows = conn.execute(
            "SELECT t.id, t.title, t.type, p.name AS project "
            "FROM tasks t LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE t.is_active=1 AND t.type NOT IN ('idea','completed','architecture') "
            "ORDER BY t.type, t.title",
        ).fetchall()
        tasks_by_type: dict[str, list] = {}
        for t in task_rows:
            tasks_by_type.setdefault(t["type"], []).append({
                "id": t["id"], "title": t["title"], "project": t["project"],
                "has_active_session": t["id"] in active_task_ids,
            })

        # Recent completions (last 10 non-survey)
        recent_rows = conn.execute(
            "SELECT task_id, agent_type, exit_reason, exit_summary, ended_at "
            "FROM agent_sessions WHERE ended_at IS NOT NULL AND agent_type != 'survey' "
            "ORDER BY id DESC LIMIT 10",
        ).fetchall()
        recent_completions = [dict(r) for r in recent_rows]

        # Stuck candidates: open session + no budget entry in last 10 min
        stuck = []
        now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        for row in active_rows:
            tid = row["task_id"]
            last_entry = conn.execute(
                "SELECT created_at FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()
            if last_entry is None:
                stuck.append({
                    "task_id": tid,
                    "agent_type": row["agent_type"],
                    "session_started": row["started_at"],
                    "last_budget_entry": None,
                    "note": "no budget entries ever — in survey phase or waiting for LLM slot",
                })
            else:
                try:
                    last_dt = datetime.fromisoformat(last_entry["created_at"].replace(" ", "T"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                    if age_min > 10:
                        stuck.append({
                            "task_id": tid,
                            "agent_type": row["agent_type"],
                            "session_started": row["started_at"],
                            "last_budget_entry": last_entry["created_at"],
                            "idle_minutes": round(age_min, 1),
                            "note": "active session but no LLM call in >10 min",
                        })
                except Exception:
                    pass

        return {
            "active_sessions": active_sessions,
            "tasks_by_type": tasks_by_type,
            "recent_completions": recent_completions,
            "stuck_candidates": stuck,
            "summary": {
                "active_count": len(active_sessions),
                "pipeline_task_count": sum(len(v) for v in tasks_by_type.values()),
                "stuck_count": len(stuck),
            },
        }
    finally:
        conn.close()


def get_budget_trace(task_id: str, n: int = 20) -> list:
    """
    Last N budget entries for a task, with finish_reason and content preview
    pre-extracted from the response_data blob.

    Key signal: finish_reason='length' + empty content_preview + non-empty
    reasoning_preview means the LLM hit max_tokens during chain-of-thought.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, agent_name, prompt_cost, generation_cost, response_data, created_at "
            "FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, n),
        ).fetchall()
        result = []
        for b in rows:
            fields = extract_response_fields(b["response_data"])
            result.append({
                "id": b["id"],
                "agent_name": b["agent_name"],
                "prompt_cost": b["prompt_cost"],
                "generation_cost": b["generation_cost"],
                "created_at": b["created_at"],
                **fields,
            })
        return result
    finally:
        conn.close()


def list_tasks(project: str = None, type: str = None) -> list:
    """
    List active tasks, optionally filtered by project name and/or type.
    Returns id, title, type, project for each task.
    """
    conn = get_conn()
    try:
        query = (
            "SELECT t.id, t.title, t.type, p.name AS project "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            "WHERE t.is_active=1"
        )
        params: list = []
        if project:
            query += " AND p.name=?"
            params.append(project)
        if type:
            query += " AND t.type=?"
            params.append(type)
        query += " ORDER BY t.type, p.name, t.title"
        rows = conn.execute(query, params).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_gate_history(task_id: str, n: int = 5) -> list:
    """
    Last N planning_gate transition results for a task, with gate_checks
    extracted from the vote_summary JSON. Shows the sequence of gate
    failures and what specifically failed each run.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, outcome, vote_summary, created_at FROM transition_results "
            "WHERE task_id=? AND transition='planning_gate' ORDER BY id DESC LIMIT ?",
            (task_id, n),
        ).fetchall()
        result = []
        for r in rows:
            checks = parse_gate_checks(r["vote_summary"])
            failing = [c for c in checks if not c.get("passed")]
            result.append({
                "id": r["id"],
                "outcome": r["outcome"],
                "created_at": r["created_at"],
                "checks": checks,
                "failing_checks": failing,
            })
        return result
    finally:
        conn.close()


def get_agent_sessions(task_id: str, n: int = 12) -> list:
    """
    Session history for a task: agent_type, exit_reason, exit_summary,
    timestamps. Includes open sessions (ended_at IS NULL). Newest first.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT agent_type, exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, n),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def find_stuck_tasks(idle_minutes: int = 10) -> list:
    """
    Find tasks with an open agent session but no LLM activity in the last
    idle_minutes minutes. These are candidates for investigation.
    """
    conn = get_conn()
    try:
        # One row per task: latest open session only (avoids zombie duplicates)
        active_rows = conn.execute(
            "SELECT s.task_id, s.agent_type, s.started_at, t.title, t.type, p.name AS project "
            "FROM agent_sessions s "
            "JOIN tasks t ON s.task_id=t.id "
            "LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE s.ended_at IS NULL "
            "AND s.id = (SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id AND ended_at IS NULL) "
            "ORDER BY s.id DESC",
        ).fetchall()

        result = []
        for row in active_rows:
            tid = row["task_id"]
            last_entry = conn.execute(
                "SELECT created_at FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 1",
                (tid,),
            ).fetchone()

            if last_entry is None:
                result.append({
                    "task_id": tid,
                    "title": row["title"],
                    "type": row["type"],
                    "project": row["project"],
                    "agent_type": row["agent_type"],
                    "session_started": row["started_at"],
                    "last_budget_entry": None,
                    "idle_minutes": None,
                    "status": "no_budget_entries",
                })
                continue

            try:
                last_dt = datetime.fromisoformat(last_entry["created_at"].replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_min = (datetime.now(timezone.utc) - last_dt).total_seconds() / 60
                if age_min >= idle_minutes:
                    result.append({
                        "task_id": tid,
                        "title": row["title"],
                        "type": row["type"],
                        "project": row["project"],
                        "agent_type": row["agent_type"],
                        "session_started": row["started_at"],
                        "last_budget_entry": last_entry["created_at"],
                        "idle_minutes": round(age_min, 1),
                        "status": "idle",
                    })
            except Exception:
                pass

        return result
    finally:
        conn.close()


def get_planning_result(task_id: str) -> dict | None:
    """
    Return the latest planning_result for a task, including the full
    interface_contracts, file_manifest, and implementation_steps fields.
    Use this to inspect the actual plan content before patching it.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id, status, correction_attempts, gate_checks, "
            "interface_contracts, dependency_graph, file_manifest, "
            "test_strategy, implementation_steps, created_at "
            "FROM planning_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
        if not row:
            return None
        return {
            "id": row["id"],
            "status": row["status"],
            "correction_attempts": row["correction_attempts"],
            "gate_checks": parse_json_field(row["gate_checks"]),
            "interface_contracts": parse_json_field(row["interface_contracts"]),
            "dependency_graph": parse_json_field(row["dependency_graph"]),
            "file_manifest": parse_json_field(row["file_manifest"]),
            "test_strategy": parse_json_field(row["test_strategy"]),
            "implementation_steps": parse_json_field(row["implementation_steps"]),
            "created_at": row["created_at"],
        }
    finally:
        conn.close()


def run_inspect_cards(section: str = "", extra_args: str = "") -> str:
    """
    Run scripts/inspect_cards.py and return its stdout output.

    section: one of '', 'prereqs', 'scheduler', 'activity', 'votes',
             'budget', 'children', 'all'
    extra_args: additional flags e.g. '--hours 4' or '--task task-123'

    Use this as an escape hatch for diagnostic views not covered by
    the structured tools.
    """
    import subprocess, sys
    from pathlib import Path
    project_root = Path(__file__).parent.parent
    python = project_root / "venv" / "Scripts" / "python.exe"
    script = project_root / "scripts" / "inspect_cards.py"
    cmd = [str(python), str(script)]
    if section:
        cmd.append(section)
    if extra_args:
        cmd.extend(extra_args.split())
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=30,
            cwd=str(project_root),
        )
        output = result.stdout or ""
        if result.stderr:
            output += f"\n--- stderr ---\n{result.stderr}"
        return output[:8000]  # cap at 8 KiB
    except subprocess.TimeoutExpired:
        return "ERROR: inspect_cards.py timed out after 30s"
    except Exception as exc:
        return f"ERROR: {exc}"
