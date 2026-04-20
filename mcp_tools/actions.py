"""Write/admin action tools for TheMaestro."""

import json
import sys
import os

# Ensure project root is on sys.path so app.* imports work
_PROJECT_ROOT = str(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

_ALLOWED_PLAN_FIELDS = {
    "interface_contracts",
    "dependency_graph",
    "file_manifest",
    "test_strategy",
    "implementation_steps",
}

_ALLOWED_TASK_TYPES = {
    "idea", "planning", "indev", "conceptual_review",
    "optimization", "security", "full_review", "completed", "architecture",
}


def append_task_description(task_id: str, text: str) -> str:
    """
    Append text to a task's description field without replacing existing content.
    Use this to add scope notes, constraints, or caveats before the next planning run.

    The appended text is separated from the existing description by two newlines.
    """
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    existing = (task.description or "").rstrip()
    new_desc = existing + f"\n\n{text.strip()}"
    result = update_task(task_id, description=new_desc)
    if result is None:
        return f"ERROR: update_task returned None for '{task_id}'."
    return f"OK: appended {len(text)} chars to task '{task_id}'. New length: {len(new_desc)}."


def replace_task_description(task_id: str, description: str) -> str:
    """
    Fully replace a task's description. Use append_task_description unless
    you need to rewrite from scratch.
    """
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    result = update_task(task_id, description=description)
    if result is None:
        return f"ERROR: update_task returned None for '{task_id}'."
    return f"OK: description replaced on task '{task_id}'. Length: {len(description)}."


def patch_planning_fields(result_id: int, fields: dict) -> str:
    """
    Patch specific fields on a planning_results row identified by result_id
    (get the id from get_planning_result or diagnose_task).

    Allowed fields: interface_contracts, dependency_graph, file_manifest,
    test_strategy, implementation_steps.

    Pass fields as a dict mapping field name to the new value (list or dict).
    Example: {"interface_contracts": [...updated list...]}
    """
    invalid = set(fields.keys()) - _ALLOWED_PLAN_FIELDS
    if invalid:
        return f"ERROR: Invalid fields: {sorted(invalid)}. Allowed: {sorted(_ALLOWED_PLAN_FIELDS)}"
    if not fields:
        return "ERROR: fields dict is empty — nothing to update."

    from app.database import update_planning_result
    from app.database.session import SessionLocal

    serialized = {
        k: (v if isinstance(v, str) else json.dumps(v))
        for k, v in fields.items()
    }
    db = SessionLocal()
    try:
        result = update_planning_result(db, result_id, **serialized)
        if result is None:
            return f"ERROR: planning_result id={result_id} not found."
        return f"OK: updated fields {sorted(serialized.keys())} on planning_result {result_id}."
    finally:
        db.close()


def set_task_type(task_id: str, type: str) -> str:
    """
    Force a task to any pipeline stage. Equivalent to the /set-stage endpoint.

    Allowed types: idea, planning, indev, conceptual_review, optimization,
    security, full_review, completed, architecture.

    Use with care — this bypasses gate checks and does not create demotion records.
    """
    if type not in _ALLOWED_TASK_TYPES:
        return f"ERROR: Invalid type '{type}'. Allowed: {sorted(_ALLOWED_TASK_TYPES)}"
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    prev_type = task.type
    result = update_task(task_id, type=type)
    if result is None:
        return f"ERROR: update_task returned None for '{task_id}'."
    return f"OK: task '{task_id}' ({task.title!r}) changed from '{prev_type}' to '{type}'."


def append_task_history(task_id: str, note: str) -> str:
    """
    Append a diagnostic note to a task's history JSON array.
    Useful for leaving breadcrumbs when manually intervening.
    """
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    try:
        history = json.loads(task.history or "[]")
    except Exception:
        history = []
    history.append({"role": "system", "content": note, "source": "claude-mcp"})
    result = update_task(task_id, history=json.dumps(history))
    if result is None:
        return f"ERROR: update_task returned None for '{task_id}'."
    return f"OK: history note appended to task '{task_id}' ({len(history)} entries total)."


def trigger_planning_run(task_id: str) -> str:
    """
    Trigger a planning pipeline run for a task via the Maestro API.
    Requires the Maestro server to be running on localhost:8000.

    This is equivalent to clicking 'Run Planning' in the UI.
    """
    import urllib.request, urllib.error
    url = f"http://localhost:8000/api/tasks/{task_id}/run-planning"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return f"OK: planning triggered. Response: {body[:300]}"
    except urllib.error.URLError as e:
        return f"ERROR: Could not reach Maestro API — is the server running? {e}"
    except Exception as exc:
        return f"ERROR: {exc}"


def restart_server() -> str:
    """
    Trigger a hot-restart of the Maestro server via POST /api/admin/restart.

    Requires:
      - Maestro server running on localhost:8000
      - [server] allow_remote_restart = true in maestro.ini

    Mechanism: the server writes restart.flag and calls os._exit(0).
    Launcher.ps1 detects the flag, removes it, waits 3 s, and relaunches uvicorn.

    After calling this tool wait ~5 seconds before making further API calls —
    the server will be unavailable while restarting.
    """
    import urllib.request, urllib.error
    url = "http://localhost:8000/api/admin/restart"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=5) as resp:
            body = resp.read().decode()
            return f"OK: restart triggered. Server will be back in ~5 s. Response: {body[:300]}"
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:300]
        return f"ERROR {e.code}: {body}"
    except urllib.error.URLError as e:
        return f"ERROR: Could not reach Maestro API — is the server running? {e}"
    except Exception as exc:
        return f"ERROR: {exc}"


def demote_task(task_id: str, target_stage: str = None) -> str:
    """
    Demote a task one pipeline stage backward (or to a specific stage).

    Unlike set_task_type, this creates a demotion record and goes through
    the proper /demote endpoint. Pass target_stage to jump to a specific
    stage (e.g. "indev", "planning") rather than just one step back.
    """
    import urllib.request, urllib.error, json as _json
    url = f"http://localhost:8000/api/tasks/{task_id}/demote"
    payload = {}
    if target_stage:
        payload["target"] = target_stage
    data = _json.dumps(payload).encode()
    try:
        req = urllib.request.Request(
            url, method="POST", data=data,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return f"OK: demoted task '{task_id}'. Response: {body[:300]}"
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode()[:300]}"
    except urllib.error.URLError as e:
        return f"ERROR: Could not reach Maestro API — is the server running? {e}"
    except Exception as exc:
        return f"ERROR: {exc}"


def stop_agent(task_id: str) -> str:
    """
    Request a graceful stop of the MaestroLoop running for task_id.

    Only works for MaestroLoop agents (indev stage). Pipeline agents
    (planning, review, security, full_review) cannot be stopped mid-run.
    After calling this, the agent will finish its current turn and halt.
    """
    import urllib.request, urllib.error
    url = f"http://localhost:8000/api/agent/stop/{task_id}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return f"OK: stop requested for task '{task_id}'. Response: {body[:300]}"
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode()[:300]}"
    except urllib.error.URLError as e:
        return f"ERROR: Could not reach Maestro API — is the server running? {e}"
    except Exception as exc:
        return f"ERROR: {exc}"


_RUN_STAGE_ENDPOINTS = {
    "review": "run-review",
    "security": "run-security",
    "full_review": "run-full-review",
}


def run_pipeline_stage(task_id: str, stage: str) -> str:
    """
    Trigger a pipeline stage run for a task.

    stage must be one of: review, security, full_review.
    For planning use trigger_planning_run instead.

    Requires the Maestro server to be running on localhost:8000.
    """
    endpoint = _RUN_STAGE_ENDPOINTS.get(stage)
    if not endpoint:
        return (
            f"ERROR: Unknown stage '{stage}'. "
            f"Allowed: {sorted(_RUN_STAGE_ENDPOINTS.keys())} "
            f"(use trigger_planning_run for planning)"
        )
    import urllib.request, urllib.error
    url = f"http://localhost:8000/api/tasks/{task_id}/{endpoint}"
    try:
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode()
            return f"OK: {stage} triggered for task '{task_id}'. Response: {body[:300]}"
    except urllib.error.HTTPError as e:
        return f"ERROR {e.code}: {e.read().decode()[:300]}"
    except urllib.error.URLError as e:
        return f"ERROR: Could not reach Maestro API — is the server running? {e}"
    except Exception as exc:
        return f"ERROR: {exc}"


def get_budget_entry_full(entry_id: int) -> dict:
    """
    Fetch the full prompt and response for a single budget entry.

    Returns prompt_data and response_data in full — useful for deep-dive
    debugging when a pattern (e.g. token_limited, rapid_cycling) is detected
    and you need to see exactly what the LLM was asked and how it responded.

    Requires the Maestro server to be running on localhost:8000.
    """
    import urllib.request, urllib.error, json as _json
    url = f"http://localhost:8000/api/budget-entries/{entry_id}/full"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            return _json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return {"error": f"HTTP {e.code}: {e.read().decode()[:300]}"}
    except urllib.error.URLError as e:
        return {"error": f"Could not reach Maestro API — is the server running? {e}"}
    except Exception as exc:
        return {"error": str(exc)}


def get_scheduler_api_status() -> dict:
    """
    Fetch live scheduler status from the running Maestro server.
    Returns the full /api/scheduler/status response, which includes
    real-time LLM slot counts and active session details not visible
    from DB alone.

    Falls back to an error dict if the server is not reachable.
    """
    import urllib.request, urllib.error, json as _json
    url = "http://localhost:8000/api/scheduler/status"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return _json.loads(resp.read().decode())
    except urllib.error.URLError as e:
        return {"error": f"Maestro server not reachable: {e}"}
    except Exception as exc:
        return {"error": str(exc)}
