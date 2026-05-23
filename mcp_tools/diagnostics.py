"""Read-only diagnostic tools for TheMaestro."""

import json
from datetime import datetime, timezone
from .helpers import (
    get_conn, extract_response_fields, parse_gate_checks,
    parse_json_field, DISPATCHABLE_TYPES, _date_ago,
)


def _infer_phase(task_id: str, conn) -> str:
    """Infer current pipeline sub-phase from the last budget entry's agent_name."""
    last = conn.execute(
        "SELECT agent_name, tool_calls FROM budget_entries "
        "WHERE task_id=? ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not last:
        return "waiting_for_llm_slot"
    name = last["agent_name"] or ""
    has_tools = bool(last["tool_calls"] and last["tool_calls"] not in ("[]", "null", ""))
    if "[" in name:
        return "design_sub_agents"
    if "Planning Pipeline" in name:
        return "surveying" if has_tools else "consolidating"
    if "Component Loop" in name:
        return "implementing"
    if "PlanningCorrection" in name or "planning_correction" in name.lower():
        return "correcting"
    return "running"


def _latest_planning_result(task_id: str, conn) -> dict | None:
    """Return latest non-superseded planning result, falling back to latest overall."""
    pr = conn.execute(
        "SELECT id, status, correction_attempts, gate_checks, created_at "
        "FROM planning_results WHERE task_id=? AND status != 'superseded' ORDER BY id DESC LIMIT 1",
        (task_id,),
    ).fetchone()
    if not pr:
        pr = conn.execute(
            "SELECT id, status, correction_attempts, gate_checks, created_at "
            "FROM planning_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,),
        ).fetchone()
    if not pr:
        return None
    raw_checks = parse_json_field(pr["gate_checks"])
    return {
        "result_id": pr["id"],
        "status": pr["status"],
        "correction_attempts": pr["correction_attempts"],
        "gate_checks": raw_checks if isinstance(raw_checks, list) else [],
        "created_at": pr["created_at"],
    }


def diagnose_task(task_id: str, since_entry_id: int = None) -> dict:
    """
    Complete diagnostic snapshot for a single task.

    Returns current type, active/recent agent sessions (each with a
    current_phase field inferred from the last LLM call), last 15 budget
    entries with finish_reason pre-extracted (prompt_cost_delta and
    prompt_message_count reflect the per-turn delta model), planning gate history,
    correction agent history, cycle_counts rollup, and an activity_status
    summary. Replaces 4+ separate DB queries.

    since_entry_id: when provided, returns a lightweight delta instead of
    the full snapshot. Only budget entries with id > since_entry_id are
    returned (as new_budget_entries). Static sections (gate_history,
    transitions, recent_sessions, correction_sessions) are omitted. Useful
    for polling loops where re-reading unchanged history wastes context.
    The response includes delta=True so the caller can distinguish.
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

        # Active sessions — with inferred current_phase
        active_rows = conn.execute(
            "SELECT agent_type, started_at FROM agent_sessions "
            "WHERE task_id=? AND ended_at IS NULL ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        active_sessions = []
        for r in active_rows:
            session = dict(r)
            session["current_phase"] = _infer_phase(task_id, conn)
            active_sessions.append(session)

        # Budget trace — delta or full
        if since_entry_id is not None:
            budget_rows = conn.execute(
                "SELECT id, agent_name, prompt_cost, prompt_message_count, generation_cost, response_data, created_at "
                "FROM budget_entries WHERE task_id=? AND id > ? ORDER BY id DESC",
                (task_id, since_entry_id),
            ).fetchall()
        else:
            budget_rows = conn.execute(
                "SELECT id, agent_name, prompt_cost, prompt_message_count, generation_cost, response_data, created_at "
                "FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 15",
                (task_id,),
            ).fetchall()
        budget_trace = []
        for b in budget_rows:
            fields = extract_response_fields(b["response_data"])
            budget_trace.append({
                "id": b["id"],
                "agent_name": b["agent_name"],
                "prompt_cost_delta": b["prompt_cost"],
                "prompt_message_count": b["prompt_message_count"],
                "generation_cost": b["generation_cost"],
                "created_at": b["created_at"],
                **fields,
            })

        # Activity status — always reflects all-time last entry (not just delta window)
        if active_sessions:
            last_any = (
                budget_trace[0] if budget_trace else
                conn.execute(
                    "SELECT created_at FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 1",
                    (task_id,),
                ).fetchone()
            )
            if last_any:
                t = last_any["created_at"] if isinstance(last_any, dict) else last_any["created_at"]
                activity_status = f"active — last LLM call at {t}"
            else:
                activity_status = "active — no budget entries yet (survey or waiting for slot)"
        else:
            activity_status = "idle"

        # Delta mode — slim response, skip static sections
        if since_entry_id is not None:
            return {
                "delta": True,
                "since_entry_id": since_entry_id,
                "task": {"id": task["id"], "title": task["title"], "type": task["type"]},
                "active_sessions": active_sessions,
                "planning": _latest_planning_result(task_id, conn),
                "new_budget_entries": budget_trace,
                "activity_status": activity_status,
            }

        # Full mode
        recent_rows = conn.execute(
            "SELECT agent_type, exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 10",
            (task_id,),
        ).fetchall()
        recent_sessions = [dict(r) for r in recent_rows]

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

        tr_rows = conn.execute(
            "SELECT transition, outcome, substr(vote_summary::text, 1, 300) AS summary_preview, created_at "
            "FROM transition_results WHERE task_id=? ORDER BY id DESC LIMIT 8",
            (task_id,),
        ).fetchall()
        transitions = [
            {"transition": r["transition"], "outcome": r["outcome"],
             "summary_preview": r["summary_preview"], "created_at": r["created_at"]}
            for r in tr_rows
        ]

        corr_rows = conn.execute(
            "SELECT exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND agent_type='planning_correction' "
            "ORDER BY id DESC LIMIT 5",
            (task_id,),
        ).fetchall()
        correction_sessions = [dict(r) for r in corr_rows]

        # Cycle counts — completed sessions grouped by agent_type
        cycle_rows = conn.execute(
            "SELECT agent_type, COUNT(*) AS count FROM agent_sessions "
            "WHERE task_id=? AND ended_at IS NOT NULL GROUP BY agent_type ORDER BY count DESC",
            (task_id,),
        ).fetchall()
        cycle_counts = {r["agent_type"]: r["count"] for r in cycle_rows}

        return {
            "task": task,
            "active_sessions": active_sessions,
            "recent_sessions": recent_sessions,
            "transitions": transitions,
            "planning": _latest_planning_result(task_id, conn),
            "gate_history": gate_history,
            "budget_trace": budget_trace,
            "correction_sessions": correction_sessions,
            "cycle_counts": cycle_counts,
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
        # Includes latest activity timestamp via JOIN to avoid N+1 queries.
        active_rows = conn.execute("""
            SELECT s.task_id, s.agent_type, s.started_at, be.last_activity
            FROM agent_sessions s
            LEFT JOIN (
                SELECT task_id, MAX(created_at) AS last_activity
                FROM budget_entries
                GROUP BY task_id
            ) be ON be.task_id = s.task_id
            WHERE s.ended_at IS NULL
            AND s.id = (SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id AND ended_at IS NULL)
            ORDER BY id DESC
        """).fetchall()
        active_sessions = [
            {"task_id": r["task_id"], "agent_type": r["agent_type"], "started_at": r["started_at"]}
            for r in active_rows
        ]
        active_task_ids = {r["task_id"] for r in active_rows}

        # All pipeline tasks by type
        task_rows = conn.execute(
            "SELECT t.id, t.title, t.type, p.name AS project "
            "FROM tasks t LEFT JOIN projects p ON t.project_id=p.id "
            "WHERE t.is_active AND t.type NOT IN ('idea','completed','architecture') "
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
        now = datetime.now(timezone.utc)
        for row in active_rows:
            tid = row["task_id"]
            last_activity = row["last_activity"]

            if last_activity is None:
                stuck.append({
                    "task_id": tid,
                    "agent_type": row["agent_type"],
                    "session_started": row["started_at"],
                    "last_budget_entry": None,
                    "note": "no budget entries ever — in survey phase or waiting for LLM slot",
                })
            else:
                try:
                    last_dt = datetime.fromisoformat(last_activity.replace(" ", "T"))
                    if last_dt.tzinfo is None:
                        last_dt = last_dt.replace(tzinfo=timezone.utc)
                    age_min = (now - last_dt).total_seconds() / 60
                    if age_min > 10:
                        stuck.append({
                            "task_id": tid,
                            "agent_type": row["agent_type"],
                            "session_started": row["started_at"],
                            "last_budget_entry": last_activity,
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

    Since migration 0076 budget entries store per-turn deltas, not cumulative
    totals. Fields reflect this:
      prompt_cost_delta     — tokens added to the prompt in THIS turn only
      prompt_message_count  — total messages in context at this turn (absolute)
      generation_cost       — tokens generated in this turn (unchanged)

    Key signal: finish_reason='length' + empty content_preview + non-empty
    reasoning_preview means the LLM hit max_tokens during chain-of-thought.
    Watch prompt_message_count climbing toward the model's context limit.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, agent_name, prompt_cost, prompt_message_count, generation_cost, response_data, created_at "
            "FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT ?",
            (task_id, n),
        ).fetchall()
        result = []
        for b in rows:
            fields = extract_response_fields(b["response_data"])
            result.append({
                "id": b["id"],
                "agent_name": b["agent_name"],
                "prompt_cost_delta": b["prompt_cost"],
                "prompt_message_count": b["prompt_message_count"],
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
            "WHERE t.is_active"
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
        # Batch JOIN: active sessions + title/type/project + latest activity timestamp.
        # GROUP BY is handled by the subquery to ensure one row per unique task_id.
        rows = conn.execute("""
            SELECT s.task_id, s.agent_type, s.started_at, t.title, t.type, p.name AS project,
                   be.last_activity
            FROM agent_sessions s
            JOIN tasks t ON s.task_id = t.id
            LEFT JOIN projects p ON t.project_id = p.id
            LEFT JOIN (
                SELECT task_id, MAX(created_at) AS last_activity
                FROM budget_entries
                GROUP BY task_id
            ) be ON be.task_id = s.task_id
            WHERE s.ended_at IS NULL
            AND s.id = (SELECT MAX(id) FROM agent_sessions WHERE task_id=s.task_id AND ended_at IS NULL)
            ORDER BY s.id DESC
        """).fetchall()

        result = []
        now = datetime.now(timezone.utc)
        for row in rows:
            tid = row["task_id"]
            last_activity = row["last_activity"]

            if last_activity is None:
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
                last_dt = datetime.fromisoformat(last_activity.replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_min = (now - last_dt).total_seconds() / 60
                if age_min >= idle_minutes:
                    result.append({
                        "task_id": tid,
                        "title": row["title"],
                        "type": row["type"],
                        "project": row["project"],
                        "agent_type": row["agent_type"],
                        "session_started": row["started_at"],
                        "last_budget_entry": last_activity,
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


def get_capacity_status() -> dict:
    """
    Current LLM slot utilisation across all compute nodes.

    Returns a per-node, per-LLM breakdown of used/free/total sessions so
    you can answer "how many slots are free right now?" in one call.
    Active session counts are read from open agent_sessions rows; the
    connection is read-only so it never blocks the running server.
    """
    conn = get_conn()
    try:
        nodes = conn.execute(
            "SELECT id, name, max_parallel_sessions, max_loaded_models "
            "FROM compute_nodes ORDER BY name"
        ).fetchall()

        llms = conn.execute(
            "SELECT id, address, port, model, max_context, parallel_sessions, compute_node_id "
            "FROM llms ORDER BY compute_node_id, id"
        ).fetchall()

        # Count open sessions per llm_id
        session_counts = {}
        for row in conn.execute(
            "SELECT llm_id, COUNT(*) AS cnt FROM agent_sessions "
            "WHERE ended_at IS NULL AND llm_id IS NOT NULL GROUP BY llm_id"
        ).fetchall():
            session_counts[row["llm_id"]] = row["cnt"]

        # Group LLMs by node
        llms_by_node: dict[int | None, list] = {}
        for llm in llms:
            nid = llm["compute_node_id"]
            llms_by_node.setdefault(nid, []).append(llm)

        result_nodes = []
        total_free = 0
        total_capacity = 0

        # Nodes with LLMs attached
        for node in nodes:
            nid = node["id"]
            node_llms = llms_by_node.pop(nid, [])
            node_used = sum(session_counts.get(l["id"], 0) for l in node_llms)
            node_cap = node["max_parallel_sessions"]
            node_free = max(0, node_cap - node_used)
            models_active = sum(1 for l in node_llms if session_counts.get(l["id"], 0) > 0)

            endpoints = []
            for llm in node_llms:
                used = session_counts.get(llm["id"], 0)
                cap = llm["parallel_sessions"]
                free = max(0, cap - used)
                total_free += free
                total_capacity += cap
                endpoints.append({
                    "llm_id": llm["id"],
                    "endpoint": f"{llm['address']}:{llm['port']}",
                    "model": llm["model"],
                    "max_context": llm["max_context"],
                    "sessions_used": used,
                    "sessions_free": free,
                    "sessions_total": cap,
                    "status": (
                        f"OVERCOUNTED ({used}/{cap}) — zombie session likely; restart server to clear"
                        if used > cap else
                        "FULL" if free == 0 else f"{free} free"
                    ),
                })

            result_nodes.append({
                "node_id": nid,
                "node_name": node["name"],
                "node_sessions_used": node_used,
                "node_sessions_total": node_cap,
                "node_models_active": models_active,
                "node_models_max": node["max_loaded_models"],
                "llm_endpoints": endpoints,
            })

        # Unassigned LLMs (no compute_node_id)
        orphan_llms = llms_by_node.get(None, [])
        if orphan_llms:
            endpoints = []
            for llm in orphan_llms:
                used = session_counts.get(llm["id"], 0)
                cap = llm["parallel_sessions"]
                free = max(0, cap - used)
                total_free += free
                total_capacity += cap
                endpoints.append({
                    "llm_id": llm["id"],
                    "endpoint": f"{llm['address']}:{llm['port']}",
                    "model": llm["model"],
                    "max_context": llm["max_context"],
                    "sessions_used": used,
                    "sessions_free": free,
                    "sessions_total": cap,
                    "status": (
                        f"OVERCOUNTED ({used}/{cap}) — zombie session likely; restart server to clear"
                        if used > cap else
                        "FULL" if free == 0 else f"{free} free"
                    ),
                })
            result_nodes.append({
                "node_id": None,
                "node_name": "(no compute node)",
                "node_sessions_used": sum(session_counts.get(l["id"], 0) for l in orphan_llms),
                "node_sessions_total": sum(l["parallel_sessions"] for l in orphan_llms),
                "node_models_active": None,
                "node_models_max": None,
                "llm_endpoints": endpoints,
            })

        return {
            "nodes": result_nodes,
            "summary": {
                "total_slots_free": total_free,
                "total_slots_used": total_capacity - total_free,
                "total_slots": total_capacity,
                "status": "IDLE" if total_capacity - total_free == 0
                          else ("FULL" if total_free == 0 else "ACTIVE"),
            },
        }
    finally:
        conn.close()


def list_pending_merges(project: str = None) -> list:
    """
    All COMPLETED tasks whose work has not yet been merged to main.

    A task is "pending merge" when it has type='completed' and either has no
    merge_record row or its merge_record.merge_commit_sha is NULL.
    Returns task_id, title, project, branch_name, and accepted_at.
    Optionally filter by project name.
    """
    conn = get_conn()
    try:
        query = """
            SELECT t.id, t.title, p.name AS project, t.updated_at AS accepted_at,
                   mr.branch_name, mr.merge_commit_sha, mr.status AS merge_status
            FROM tasks t
            LEFT JOIN projects p ON t.project_id = p.id
            LEFT JOIN merge_records mr ON mr.task_id = t.id
            WHERE t.is_active
              AND t.type = 'completed'
              AND (mr.id IS NULL OR mr.merge_commit_sha IS NULL)
        """
        params: list = []
        if project:
            query += " AND p.name = ?"
            params.append(project)
        query += " ORDER BY t.updated_at DESC"

        rows = conn.execute(query, params).fetchall()
        result = []
        for r in rows:
            branch = r["branch_name"] or f"maestro/task-{r['id']}"
            result.append({
                "task_id": r["id"],
                "title": r["title"],
                "project": r["project"],
                "branch": branch,
                "accepted_at": r["accepted_at"],
                "merge_status": r["merge_status"] or "no_record",
            })
        return result
    finally:
        conn.close()


def get_project_health(project: str = None) -> dict:
    """
    High-level Kanban and Budget overview for a project (or all projects).
    Use this for an 'Executive Summary' of what is on the board.

    Returns:
    - Stage distribution: card counts per pipeline stage
    - Active sessions: summary list of tasks currently running
    - Recent demotions: tasks that failed a gate in the last 24 hours
    - Pending merges: count of completed tasks waiting for git merge
    - Budget spend: token cost in dollars/microcents (last 7 days)
    """
    conn = get_conn()
    try:
        proj_filter = ""
        params: list = []
        if project:
            proj_filter = " AND p.name = ?"
            params.append(project)

        # Stage distribution
        stage_rows = conn.execute(
            "SELECT t.type, COUNT(*) AS cnt "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            f"WHERE t.is_active{proj_filter} "
            "GROUP BY t.type ORDER BY t.type",
            params,
        ).fetchall()
        stage_dist = {r["type"]: r["cnt"] for r in stage_rows}

        # Active sessions with latest activity JOIN
        active_rows = conn.execute(f"""
            SELECT s.task_id, t.title, t.type, p.name AS project, s.agent_type, s.started_at,
                   be.last_activity
            FROM agent_sessions s
            JOIN tasks t ON s.task_id = t.id
            LEFT JOIN projects p ON t.project_id = p.id
            LEFT JOIN (
                SELECT task_id, MAX(created_at) AS last_activity
                FROM budget_entries
                GROUP BY task_id
            ) be ON be.task_id = s.task_id
            WHERE s.ended_at IS NULL{proj_filter}
            AND s.id = (SELECT MAX(id) FROM agent_sessions WHERE task_id = s.task_id AND ended_at IS NULL)
            ORDER BY s.started_at DESC
        """, params).fetchall()
        active_sessions = [
            {
                "task_id": r["task_id"], "title": r["title"], "type": r["type"],
                "project": r["project"], "agent_type": r["agent_type"], "started_at": r["started_at"]
            }
            for r in active_rows
        ]

        # Recent demotions (last 24 h) — tasks whose demotion_count > 0 and updated recently
        demotion_rows = conn.execute(
            "SELECT t.id, t.title, t.type, p.name AS project, t.demotion_count, t.updated_at "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            f"WHERE t.is_active AND t.demotion_count > 0{proj_filter} "
            f"AND t.updated_at >= {_date_ago(1, 'day')} "
            "ORDER BY t.updated_at DESC",
            params,
        ).fetchall()
        recent_demotions = [dict(r) for r in demotion_rows]

        # Pending merges count
        merge_query = (
            "SELECT COUNT(*) AS cnt FROM tasks t "
            "LEFT JOIN projects p ON t.project_id = p.id "
            "LEFT JOIN merge_records mr ON mr.task_id = t.id "
            f"WHERE t.is_active AND t.type = 'completed'"
            f"{proj_filter} AND (mr.id IS NULL OR mr.merge_commit_sha IS NULL)"
        )
        pending_merges = conn.execute(merge_query, params).fetchone()["cnt"]

        # Budget spend last 7 days (microcents)
        spend_row = conn.execute(
            "SELECT COALESCE(SUM(e.total_cost_microcents), 0) AS total "
            "FROM expenses e "
            "JOIN tasks t ON e.task_id = t.id "
            "LEFT JOIN projects p ON t.project_id = p.id "
            f"WHERE e.created_at >= {_date_ago(7, 'days')}{proj_filter}",
            params,
        ).fetchone()
        spend_microcents = spend_row["total"] if spend_row else 0
        spend_dollars = round(spend_microcents / 100_000_000, 4)

        # Stuck candidates: open session, no budget entry in last 10 min
        stuck = []
        now = datetime.now(timezone.utc)
        for row in active_rows:
            tid = row["task_id"]
            last_activity = row["last_activity"]
            if last_activity is None:
                stuck.append({"task_id": tid, "title": row["title"],
                               "idle_minutes": None, "note": "no LLM calls yet"})
                continue
            try:
                last_dt = datetime.fromisoformat(last_activity.replace(" ", "T"))
                if last_dt.tzinfo is None:
                    last_dt = last_dt.replace(tzinfo=timezone.utc)
                age_min = (now - last_dt).total_seconds() / 60
                if age_min > 10:
                    stuck.append({"task_id": tid, "title": row["title"],
                                  "idle_minutes": round(age_min, 1),
                                  "note": "open session, no LLM call in >10 min"})
            except Exception:
                pass

        scope = project or "all projects"
        return {
            "scope": scope,
            "stage_distribution": stage_dist,
            "active_sessions": active_sessions,
            "active_count": len(active_sessions),
            "recent_demotions_24h": recent_demotions,
            "pending_merges": pending_merges,
            "budget_spend_7d_microcents": spend_microcents,
            "budget_spend_7d_dollars": spend_dollars,
            "stuck_candidates": stuck,
        }
    finally:
        conn.close()


def get_project_diagnostic(project: str, limit: int = 15) -> dict:
    """
    Deep-dive 'Engine Diagnostic' for debugging execution issues in a project.
    Optimized for maximum speed - avoids large blobs and slow joins.
    """
    import os, subprocess
    conn = get_conn()
    try:
        # 1. Project Info
        p_row = conn.execute(
            "SELECT id, path FROM projects WHERE name = ?", (project,)
        ).fetchone()
        if not p_row:
            return {"error": f"Project '{project}' not found."}
        project_id = p_row["id"]
        project_path = p_row["path"]

        # 2. Stage Distribution (Active Only)
        stage_rows = conn.execute(
            "SELECT type, COUNT(*) as cnt FROM tasks "
            "WHERE project_id = ? AND is_active GROUP BY type",
            (project_id,)
        ).fetchall()
        stage_dist = {r["type"]: r["cnt"] for r in stage_rows}

        # 3. Active Sessions with Duration and Inferred Phase
        # Uses fast indexed subqueries for phase inference metadata
        active_rows = conn.execute(
            """
            SELECT s.task_id, t.title, t.type, s.agent_type, s.started_at,
                   (SELECT agent_name FROM budget_entries WHERE task_id = s.task_id ORDER BY id DESC LIMIT 1) as last_agent
            FROM agent_sessions s
            JOIN tasks t ON s.task_id = t.id
            WHERE t.project_id = ? AND s.ended_at IS NULL
            """,
            (project_id,)
        ).fetchall()

        active_sessions = []
        now = datetime.now(timezone.utc)
        for a in active_rows:
            started = datetime.fromisoformat(a["started_at"].replace(" ", "T"))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            duration_min = (now - started).total_seconds() / 60
            
            active_sessions.append({
                "task_id": a["task_id"],
                "title": a["title"],
                "type": a["type"],
                "agent_type": a["agent_type"],
                "started_at": a["started_at"],
                "duration_minutes": round(duration_min, 1),
                "last_agent": a["last_agent"]
            })

        # 4. Recent Activity (Metadata Only - No large blobs)
        recent_rows = conn.execute(
            "SELECT b.id, b.task_id, t.title, b.agent_name, b.created_at "
            "FROM budget_entries b "
            "JOIN tasks t ON b.task_id = t.id "
            "WHERE t.project_id = ? "
            "ORDER BY b.id DESC LIMIT ?",
            (project_id, limit)
        ).fetchall()
        
        recent_activity = [dict(r) for r in recent_rows]

        return {
            "project": project,
            "stage_distribution": stage_dist,
            "active_sessions": active_sessions,
            "recent_activity": recent_activity,
            "summary": {
                "active_count": len(active_sessions),
                "total_active_tasks": sum(stage_dist.values())
            }
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


def preview_dispatch(project: str = None) -> dict:
    """
    Dry-run the scheduler tick without dispatching anything.

    Returns what *would* be dispatched: task ID, title, target LLM, estimated
    token cost, and why each other ready task was skipped (capacity, cooldown,
    PIP gate, budget, etc.).

    No DB writes. Replicates the _tick() dispatch logic in read-only mode
    using the same DAG resolution, capacity checks, and cooldown filters.

    project: optional project name filter. If None, scans all projects.
    """
    import time
    import sys
    import os
    from pathlib import Path

    # Ensure project root is on sys.path
    _project_root = str(Path(__file__).parent.parent)
    if _project_root not in sys.path:
        sys.path.insert(0, _project_root)

    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_task, get_llm, get_project

    conn = get_conn()
    try:
        # --- Gather tasks ---
        all_tasks_rows = conn.execute(
            "SELECT t.id, t.title, t.type, t.prerequisites, t.llm_id, t.budget_id, "
            "       t.position, t.clarification_status, t.intake_exhausted_at, "
            "       p.name AS project, p.id AS project_id "
            "FROM tasks t LEFT JOIN projects p ON t.project_id = p.id "
            "WHERE t.is_active",
        ).fetchall()

        if project:
            all_tasks_rows = [r for r in all_tasks_rows if r["project"] == project]

        task_dicts = []
        for r in all_tasks_rows:
            prereqs = r["prerequisites"]
            try:
                prereqs = json.loads(prereqs) if isinstance(prereqs, str) else (prereqs or [])
            except Exception:
                prereqs = []
            task_dicts.append({
                "id": r["id"],
                "title": r["title"],
                "type": r["type"],
                "prerequisites": prereqs,
                "position": r["position"] or 0,
                "_llm_id": r["llm_id"],
                "_budget_id": r["budget_id"],
                "_project": r["project"],
                "_clarification_status": r["clarification_status"] or "none",
                "_intake_exhausted": bool(r["intake_exhausted_at"]),
            })

        # --- DAG resolution ---
        resolver = DAGResolver(task_dicts)
        ready_tasks = resolver.get_ready_tasks()
        by_id = {t["id"]: t for t in task_dicts}

        # Compute DAG depth for priority sorting
        def _dag_depth(tid):
            task = by_id.get(tid)
            if not task or not task.get("prerequisites"):
                return 0
            return max(
                (_dag_depth(pid) for pid in task["prerequisites"] if pid in by_id),
                default=0,
            ) + 1

        ready_tasks.sort(key=lambda t: _dag_depth(t["id"]))

        # --- Simulate dispatch state ---
        # Track which LLMs are "active" (we read from the real scheduler state
        # so the simulation reflects what's actually pinned right now).
        # We'll use the real _active_sessions from the scheduler module if it's
        # running, otherwise assume nothing is active.
        simulated_active_llm_ids = set()
        try:
            from app.agent.scheduler import _active_sessions, _session_llm_ids, _active_sessions_lock
            from app.agent.scheduler import _external_sessions, _external_sessions_lock
            with _active_sessions_lock:
                simulated_active_llm_ids = {
                    lid for key, lid in _session_llm_ids.items()
                    if key in _active_sessions and _active_sessions[key].is_alive()
                }
            with _external_sessions_lock:
                simulated_active_llm_ids.update(_external_sessions.values())
        except Exception:
            pass

        allowed_llm_id = next(iter(simulated_active_llm_ids)) if simulated_active_llm_ids else None

        # Track simulated capacity
        simulated_llm_sessions: dict[int, int] = {}
        for lid in simulated_active_llm_ids:
            simulated_llm_sessions[lid] = simulated_llm_sessions.get(lid, 0)
            # Count sessions already pinned to this LLM
        try:
            from app.agent.scheduler import _llm_session_counts, _llm_counts_lock
            with _llm_counts_lock:
                snap = dict(_llm_session_counts)
            simulated_llm_sessions.update(snap)
        except Exception:
            pass

        # Cooldown simulation — read from DB for rejection cooldowns
        # We can't easily read the in-memory cooldown dicts, so we approximate
        # using the rejection_cooldowns table or just note the limitation.
        # For a read-only preview, we'll check if the task was recently rejected.
        now = time.time()

        dispatched = []
        skipped = []

        for task in ready_tasks:
            task_id = task["id"]
            task_type = task["type"]
            llm_id = task["_llm_id"]
            budget_id = task["_budget_id"]

            # Check 1: skip types that the real scheduler never auto-dispatches
            # (matches _SCHEDULER_SKIP_STAGE_TYPES in scheduler.py — human_review only)
            if task_type in ("human_review",):
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "human_gate — requires manual action",
                })
                continue

            # Check 2: intake exhausted (idea tasks)
            if task_type == "idea" and task["_intake_exhausted"]:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "intake exhausted — human reset required",
                })
                continue

            # Check 3: clarification approved or skipped (idea tasks)
            if task_type == "idea" and task["_clarification_status"] not in ("approved", "skipped"):
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": f"clarification_status='{task['_clarification_status']}' (not approved)",
                })
                continue

            # Check 4: already running — read from real scheduler state
            is_running = False
            try:
                from app.agent.scheduler import _active_sessions, _active_sessions_lock
                with _active_sessions_lock:
                    is_running = task_id in _active_sessions and _active_sessions[task_id].is_alive()
            except Exception:
                pass
            if is_running:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "already running (active session)",
                })
                continue

            # Check 5: PIP resolution guard (review stages)
            pip_guarded = False
            if task_type in {"conceptual_review", "optimization", "security", "human_review"}:
                try:
                    from app.database import get_active_pip_resolution_jobs_for_task
                    if get_active_pip_resolution_jobs_for_task(task_id):
                        pip_guarded = True
                except Exception:
                    pass
            if pip_guarded:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "PIP resolution jobs active — re-dispatch blocked",
                })
                continue

            # Check 6: planning-gate rejection cooldown / stopped
            if task_type == "planning":
                try:
                    from app.agent.scheduler import _planning_stopped
                    if task_id in _planning_stopped:
                        skipped.append({
                            "task_id": task_id,
                            "title": task["title"],
                            "type": task_type,
                            "reason": "planning stopped — requires manual re-trigger via /run-planning",
                        })
                        continue
                except Exception:
                    pass
                # Check rejection cooldown via DB transition results
                try:
                    last_planning_gate = conn.execute(
                        "SELECT created_at FROM transition_results "
                        "WHERE task_id=? AND transition='planning_gate' AND outcome='fail' "
                        "ORDER BY id DESC LIMIT 1",
                        (task_id,),
                    ).fetchone()
                    if last_planning_gate and last_planning_gate["created_at"]:
                        gate_time = last_planning_gate["created_at"].replace("T", " ").replace("Z", "")
                        from datetime import datetime as _dt
                        try:
                            gate_dt = _dt.strptime(gate_time, "%Y-%m-%d %H:%M:%S")
                            gate_dt = gate_dt.replace(tzinfo=_dt.now(_dt.timezone.utc).tzinfo)
                            age_min = (now - gate_dt.timestamp()) / 60
                            if age_min < 5:
                                skipped.append({
                                    "task_id": task_id,
                                    "title": task["title"],
                                    "type": task_type,
                                    "reason": f"planning-gate rejection cooldown ({age_min:.1f} min ago, 5 min cooldown)",
                                })
                                continue
                        except Exception:
                            pass
                except Exception:
                    pass

            # Check 7: failure cooldown (60s)
            failure_cooldown = False
            try:
                from app.agent.scheduler import _failed_cooldowns
                if task_id in _failed_cooldowns:
                    elapsed = now - _failed_cooldowns[task_id]
                    if elapsed < 60:
                        failure_cooldown = True
                        skipped.append({
                            "task_id": task_id,
                            "title": task["title"],
                            "type": task_type,
                            "reason": f"failure cooldown ({60 - elapsed:.0f}s remaining)",
                        })
                        continue
            except Exception:
                pass

            # Check 8: LLM configured
            if not llm_id:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "no LLM configured",
                })
                continue

            # Check 9: LLM exists
            db_task = get_task(task_id)
            if not db_task or not db_task.llm_id:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": "task has no LLM reference",
                })
                continue

            llm = get_llm(db_task.llm_id)
            if not llm:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": f"LLM {db_task.llm_id} not found",
                })
                continue

            # Check 10: one-LLM-at-a-time policy
            if allowed_llm_id is not None and llm.id != allowed_llm_id:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": f"one-LLM policy pinned to LLM {allowed_llm_id} ({llm.model}); this task uses LLM {llm.id} ({llm.model})",
                })
                continue

            # Check 11: budget capacity
            worst = 0
            if llm_id and budget_id:
                pp_rate = getattr(llm, 'cost_per_million_prompt_tokens', 0.0) or 0.0
                tg_rate = getattr(llm, 'cost_per_million_completion_tokens', 0.0) or 0.0
                if pp_rate > 0 or tg_rate > 0:
                    from app.agent.config import MAX_TOKENS_PER_TURN
                    worst = int(llm.max_context * pp_rate * 100) + int(MAX_TOKENS_PER_TURN * tg_rate * 100)

            if worst > 0:
                try:
                    from app.database import budget_has_capacity
                    if not budget_has_capacity(budget_id, worst):
                        skipped.append({
                            "task_id": task_id,
                            "title": task["title"],
                            "type": task_type,
                            "reason": f"budget {budget_id} insufficient (worst-case {worst} µ¢ needed)",
                        })
                        continue
                except Exception:
                    pass

            # Check 12: capacity slot
            current_count = simulated_llm_sessions.get(llm.id, 0)
            cap = llm.parallel_sessions
            if current_count >= cap:
                skipped.append({
                    "task_id": task_id,
                    "title": task["title"],
                    "type": task_type,
                    "reason": f"LLM {llm.id} capacity full ({current_count}/{cap} sessions)",
                })
                continue

            # All checks passed — would dispatch
            simulated_llm_sessions[llm.id] = current_count + 1
            if allowed_llm_id is None:
                allowed_llm_id = llm.id

            estimated_cost = ""
            if worst > 0:
                estimated_cost = f"{worst} µ¢"

            dispatched.append({
                "task_id": task_id,
                "title": task["title"],
                "type": task_type,
                "project": task["_project"],
                "llm_id": llm.id,
                "llm_model": llm.model,
                "estimated_cost": estimated_cost,
            })

        return {
            "dispatched": dispatched,
            "skipped": skipped,
            "summary": {
                "total_ready": len(ready_tasks),
                "would_dispatch": len(dispatched),
                "would_skip": len(skipped),
                "active_llm_ids": list(simulated_active_llm_ids),
                "pinned_llm_id": allowed_llm_id,
            },
        }
    finally:
        conn.close()


def get_task_events(task_id: str, event_type: str | None = None) -> list[dict]:
    """
    Task event log from the tasks.history JSON column.

    Returns the array written by append_task_history().
    Each entry: {status, timestamp, message}.

    event_type: optional filter, e.g. 'merge_test_failed', 'ready_for_review',
                'correction_attempts'. Pass None to return all events.

    Useful for diagnosing merge failures, correction cycles, and other
    lifecycle events that aren't captured in agent_sessions or transitions.
    """
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT history FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        if not row:
            return []
        events = parse_json_field(row["history"]) or []
        if not isinstance(events, list):
            return []
        if event_type:
            events = [e for e in events if isinstance(e, dict) and e.get("status") == event_type]
        return events
    finally:
        conn.close()


def get_merge_records(task_id: str) -> list[dict]:
    """
    All merge and virtual-merge attempts for a task from the merge_records table.

    Each record includes: id, branch_name, merge_commit_sha, status,
    error_detail, test_output (first 500 chars), created_at.

    Status values: merged | conflict | test_failure | error | virtual_passed | push_failure

    Useful for understanding why virtual merge kept failing before a task
    could advance from final_review to human_review.
    """
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, task_id, branch_name, merge_commit_sha, status, "
            "       error_detail, test_output, created_at "
            "FROM merge_records WHERE task_id=? ORDER BY id DESC",
            (task_id,),
        ).fetchall()
        result = []
        for r in rows:
            result.append({
                "id": r["id"],
                "task_id": r["task_id"],
                "branch_name": r["branch_name"],
                "merge_commit_sha": r["merge_commit_sha"],
                "status": r["status"],
                "error_detail": r["error_detail"],
                "test_output": (r["test_output"] or "")[:500] if r["test_output"] else None,
                "created_at": r["created_at"],
            })
        return result
    finally:
        conn.close()


def get_git_branch_state(project_name: str) -> dict:
    """
    Git state for a project: HEAD branch, all maestro/task-* branches,
    and active worktrees.

    Runs read-only git commands against the project's real filesystem path
    (looked up from the projects table). Useful for diagnosing merge failures:
    - Reveals main vs master branch name mismatches
    - Shows which task branches exist locally
    - Identifies stale or orphaned worktrees

    Returns: project_name, project_path, main_branch, current_commit,
             task_branches (list), active_worktrees (list).
    """
    import subprocess
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT path FROM projects WHERE name=?", (project_name,)
        ).fetchone()
        if not row:
            return {"error": f"Project '{project_name}' not found."}
        project_path = row["path"]
    finally:
        conn.close()

    def _git(args: list[str]) -> tuple[int, str]:
        try:
            r = subprocess.run(
                ["git"] + args, capture_output=True, text=True,
                timeout=15, cwd=project_path,
            )
            return r.returncode, (r.stdout + r.stderr).strip()
        except Exception as e:
            return 1, str(e)

    # Current HEAD
    rc, head_branch = _git(["symbolic-ref", "--short", "HEAD"])
    if rc != 0:
        rc, head_branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    head_branch = head_branch if rc == 0 else "unknown"

    rc, current_commit = _git(["rev-parse", "--short", "HEAD"])
    current_commit = current_commit if rc == 0 else "unknown"

    # Detect main branch (main or master)
    main_branch = None
    for candidate in ("main", "master"):
        rc2, out2 = _git(["branch", "--list", candidate])
        if out2.strip():
            main_branch = candidate
            break

    # All maestro/task-* branches
    rc, branches_out = _git(["branch", "--list", "maestro/task-*"])
    task_branches = [b.strip().lstrip("* ") for b in branches_out.splitlines() if b.strip()] if rc == 0 else []

    # Active worktrees
    rc, worktree_out = _git(["worktree", "list", "--porcelain"])
    worktrees = []
    if rc == 0:
        current_wt: dict = {}
        for line in worktree_out.splitlines():
            if line.startswith("worktree "):
                if current_wt:
                    worktrees.append(current_wt)
                current_wt = {"path": line[len("worktree "):]}
            elif line.startswith("HEAD "):
                current_wt["commit"] = line[5:]
            elif line.startswith("branch "):
                current_wt["branch"] = line[len("branch "):]
            elif line == "detached":
                current_wt["branch"] = "(detached)"
        if current_wt:
            worktrees.append(current_wt)

    return {
        "project_name": project_name,
        "project_path": project_path,
        "head_branch": head_branch,
        "main_branch": main_branch,
        "current_commit": current_commit,
        "task_branches": task_branches,
        "task_branch_count": len(task_branches),
        "active_worktrees": worktrees,
    }


def get_tool_bug_reports(
    task_id: str = None,
    tool_name: str = None,
    unread_only: bool = True,
    limit: int = 50,
) -> list:
    """
    Fetch agent-filed tool bug reports.

    Agents call report_tool_bug() when a tool misbehaves — wrong output,
    stale content, unexpected error, missing capability, etc. Each report
    captures the session_id, tool name, what the agent was trying to do,
    what it expected, and what actually happened.

    unread_only: when True (default) only returns reports not yet viewed.
    Filter by task_id or tool_name to drill into a specific session or tool.
    Returns newest-first up to `limit` rows.
    """
    conn = get_conn()
    try:
        params: list = []
        where_clauses: list[str] = []
        if task_id:
            where_clauses.append("task_id = ?")
            params.append(task_id)
        if tool_name:
            where_clauses.append("tool_name = ?")
            params.append(tool_name)
        if unread_only:
            where_clauses.append("viewed_at IS NULL")
        where = ("WHERE " + " AND ".join(where_clauses)) if where_clauses else ""
        params.append(limit)
        rows = conn.execute(
            f"SELECT id, task_id, session_id, tool_name, trying_to, expected, actual, created_at, viewed_at "
            f"FROM tool_bug_reports {where} ORDER BY created_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [
            {
                "id": r[0],
                "task_id": r[1],
                "session_id": r[2],
                "tool_name": r[3],
                "trying_to": r[4],
                "expected": r[5],
                "actual": r[6],
                "created_at": r[7],
                "viewed_at": r[8],
            }
            for r in rows
        ]
    finally:
        conn.close()


def mark_tool_bug_reports_viewed(report_ids: list = None) -> dict:
    """
    Mark tool bug reports as viewed so they no longer appear in the unread feed.

    Pass report_ids=[1, 2, 3] to mark specific reports, or omit (None) to mark
    all unread reports viewed at once.

    Returns {"marked": N} count of rows updated.
    """
    from .helpers import get_rw_conn
    conn = get_rw_conn()
    try:
        now = __import__("datetime").datetime.utcnow().isoformat()
        if report_ids:
            placeholders = ",".join("?" for _ in report_ids)
            result = conn.execute(
                f"UPDATE tool_bug_reports SET viewed_at=? WHERE viewed_at IS NULL AND id IN ({placeholders})",
                [now, *report_ids],
            )
        else:
            result = conn.execute(
                "UPDATE tool_bug_reports SET viewed_at=? WHERE viewed_at IS NULL",
                [now],
            )
        conn.commit()
        return {"marked": result.rowcount}
    finally:
        conn.close()


def tail_task(task_id: str, since_entry_id: int = 0, n: int = 20) -> dict:
    """
    Lightweight live-tail for a running task session.

    Returns only budget entries with id > since_entry_id (max n), each
    enriched with extracted tool call names so you can see exactly what the
    agent called. Designed for rapid polling — call every 5-10 seconds,
    passing last_entry_id from the previous response as since_entry_id.

    Returns:
      last_entry_id   — pass as since_entry_id on next call (0 if no entries yet)
      active          — True if an open agent session exists
      stage           — current task.type
      new_entries     — list of LLM calls since since_entry_id, oldest first:
          id, created_at, agent_name, finish_reason,
          tool_names (list[str]), reasoning_snippet (100 chars), content_snippet (200 chars),
          gen_tokens, ctx_messages
    """
    conn = get_conn()
    try:
        task_row = conn.execute(
            "SELECT t.type FROM tasks t WHERE t.id = ?",
            (task_id,),
        ).fetchone()
        if not task_row:
            return {"error": f"Task '{task_id}' not found."}

        active_row = conn.execute(
            "SELECT id FROM agent_sessions WHERE task_id = ? AND ended_at IS NULL LIMIT 1",
            (task_id,),
        ).fetchone()
        active = active_row is not None

        rows = conn.execute(
            "SELECT id, agent_name, generation_cost, prompt_message_count, response_data, created_at "
            "FROM budget_entries "
            "WHERE task_id = ? AND id > ? "
            "ORDER BY id ASC LIMIT ?",
            (task_id, since_entry_id, n),
        ).fetchall()

        entries = []
        for r in rows:
            try:
                data = json.loads(r["response_data"] or "{}")
            except Exception:
                data = {}
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message") or {}
            finish_reason = choice.get("finish_reason") or ""
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            raw_tool_calls = msg.get("tool_calls") or []
            tool_names = [
                tc.get("function", {}).get("name", "?")
                for tc in raw_tool_calls
                if isinstance(tc, dict)
            ]
            entries.append({
                "id": r["id"],
                "created_at": str(r["created_at"]),
                "agent_name": r["agent_name"] or "",
                "finish_reason": finish_reason,
                "tool_names": tool_names,
                "reasoning_snippet": reasoning[:100] if reasoning else "",
                "content_snippet": content[:200] if content else "",
                "gen_tokens": r["generation_cost"],
                "ctx_messages": r["prompt_message_count"],
            })

        last_entry_id = entries[-1]["id"] if entries else since_entry_id
        return {
            "last_entry_id": last_entry_id,
            "active": active,
            "stage": task_row["type"],
            "new_entries": entries,
        }
    finally:
        conn.close()
