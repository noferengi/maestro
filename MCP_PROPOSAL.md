# Maestro MCP Server — Implementation Proposal

## What this is

An MCP (Model Context Protocol) server that exposes TheMaestro's internals as
native tools callable by Claude Code. Instead of shelling out to `venv/Scripts/python.exe -c`,
constructing SQL queries, and parsing human-readable ASCII, Claude calls
`maestro.diagnose_task(task_id)` and gets structured JSON back — one tool call, complete picture.

MCP is Anthropic's open protocol for connecting AI models to tools and data sources.
Claude Code supports it natively: registered servers appear as tools in Claude's
tool loop, results arrive as tool results (not subprocess stdout), and the server
persists between calls (no cold-start overhead per query).

---

## Architecture decision: stdio transport, separate process

Two options:
- **stdio** — server runs as a child process, communicates via stdin/stdout JSON-RPC. Simple, no port conflicts, standard for local servers.
- **HTTP/SSE** — server runs persistently, clients connect over HTTP. Required for remote access or multiple simultaneous clients.

**Choice: stdio.** The MCP server imports directly from `app/`, reads `data/kanban.db`
directly (no HTTP to the running Maestro server), and runs as a child process spawned
by Claude Code on demand. Benefits: simpler implementation, no port to manage, no
dependency on whether the Maestro server is currently running.

The server **does not write to the DB during normal Maestro operation** — it reads
`data/kanban.db` directly via SQLite (read-only for diagnostic tools). Action tools
use SQLAlchemy via the existing `app/database` layer (same connection semantics as
the Maestro server; SQLite WAL mode handles concurrent reads safely).

---

## File structure

```
mcp_server.py              ← server entry point (project root)
mcp_tools/
  __init__.py
  diagnostics.py           ← read-only diagnostic tools
  actions.py               ← write/admin tools
  helpers.py               ← shared data extraction helpers
```

All files sit at the project root so `sys.path.insert(0, project_root)` gives
access to `app/` without installing the package.

---

## Dependencies

```bash
venv/Scripts/python.exe -m pip install "mcp[cli]"
```

This installs `mcp` 1.27.0+ and its dependencies (anyio, httpx-sse, pydantic).
Add `mcp[cli]` to `requirements.txt`.

---

## Registration

### Claude Code (project-level, checked into git)

Create `.claude/settings.json` at the project root (or add to existing):

```json
{
  "mcpServers": {
    "maestro": {
      "command": "D:/workspace/TheMaestro/venv/Scripts/python.exe",
      "args": ["D:/workspace/TheMaestro/mcp_server.py"]
    }
  }
}
```

### Claude Desktop (optional, for use outside Claude Code)

`C:/Users/<your-username>/AppData/Roaming/Claude/claude_desktop_config.json` — add alongside existing `ddg-search`:

```json
{
  "mcpServers": {
    "ddg-search": { "command": "uvx", "args": ["duckduckgo-mcp-server"] },
    "maestro": {
      "command": "D:/workspace/TheMaestro/venv/Scripts/python.exe",
      "args": ["D:/workspace/TheMaestro/mcp_server.py"]
    }
  }
}
```

After adding the project `.claude/settings.json`, restart Claude Code and run
`/mcp` to verify the server appears. Tools become available immediately.

---

## Tool inventory

### Diagnostic tools (read-only)

#### `maestro__diagnose_task(task_id: str) -> dict`

The primary workhorse. Returns a complete snapshot for one task in a single call.

```json
{
  "task": {
    "id": "task-1776548777.749239",
    "title": "Create Supporting Types",
    "type": "planning",
    "project": "AndroidStreetPass",
    "description": "..."
  },
  "active_sessions": [
    {"agent_type": "planning", "started_at": "2026-04-19T09:02:24Z"}
  ],
  "recent_sessions": [
    {
      "agent_type": "planning",
      "exit_reason": "subdivide",
      "exit_summary": "Planning voted subdivide — demoted to IDEA.",
      "started_at": "...",
      "ended_at": "..."
    }
  ],
  "planning": {
    "latest_result": {
      "id": 42,
      "status": "active",
      "correction_attempts": 0,
      "gate_checks": [
        {"name": "interface_completeness", "passed": false, "hard_fail": true, "detail": "Unresolved consumes: BlePacketType"}
      ]
    },
    "gate_transitions": [
      {"outcome": "rejected", "created_at": "...", "detail_preview": "..."}
    ]
  },
  "budget_trace": [
    {
      "agent_name": "Planning Pipeline",
      "finish_reason": "stop",
      "content_preview": "{\"design_rationale\": \"Create a sealed class...\"}",
      "prompt_cost": 657,
      "generation_cost": 1537,
      "created_at": "2026-04-19 09:06:21"
    }
  ],
  "correction_sessions": [],
  "activity_status": "active"
}
```

Replaces: 4+ separate DB queries to piece together why a task is stuck.

---

#### `maestro__get_scheduler_state() -> dict`

Returns what's running, queued, and blocked across all tasks.

```json
{
  "active_sessions": [
    {"task_id": "...", "agent_type": "planning", "llm_id": 1, "started_at": "..."}
  ],
  "tasks_by_type": {
    "planning": ["task-...", "task-..."],
    "indev": [],
    "review": ["task-..."]
  },
  "recent_completions": [
    {"task_id": "...", "agent_type": "planning", "exit_reason": "passed", "ended_at": "..."}
  ],
  "stuck_candidates": [
    {
      "task_id": "...",
      "type": "planning",
      "session_age_minutes": 47,
      "last_budget_entry_minutes_ago": 23
    }
  ]
}
```

`stuck_candidates`: tasks with an open session (`ended_at IS NULL`) but no budget
entry in the last 10 minutes — likely waiting for an LLM slot or hung.

---

#### `maestro__get_budget_trace(task_id: str, n: int = 15) -> list`

Last N budget entries for a task, with `finish_reason` and content preview extracted
from the raw `response_data` JSON blob. Avoids having to parse 200KB JSON responses.

```json
[
  {
    "id": 8821,
    "agent_name": "Planning Pipeline",
    "finish_reason": "length",
    "content": "",
    "reasoning_content_preview": "Let me consider the design options...",
    "prompt_cost": 9712,
    "generation_cost": 2048,
    "created_at": "2026-04-19 07:15:42"
  }
]
```

`finish_reason: "length"` with empty `content` = reasoning model hit max_tokens.
This is the key signal for `PLANNING_JUDGE_MAX_TOKENS` being too low.

---

#### `maestro__list_tasks(project: str = None, type: str = None) -> list`

List active tasks, optionally filtered. Returns id, title, type, project.
Useful for getting task IDs before calling `diagnose_task`.

---

#### `maestro__get_gate_history(task_id: str, n: int = 5) -> list`

Last N `planning_gate` transition_results for a task, with the full `gate_checks`
array extracted from `vote_summary` JSON. Shows the sequence of gate failures and
what specifically failed each time.

---

#### `maestro__get_agent_sessions(task_id: str, n: int = 10) -> list`

Session history for a task: agent_type, exit_reason, exit_summary, timestamps.
Includes open sessions (ended_at IS NULL). Ordered newest-first.

---

#### `maestro__find_stuck_tasks(idle_minutes: int = 10) -> list`

Tasks with an open planning/dev_orchestrator session but no budget entry in the
last `idle_minutes` minutes. Returns task id, title, type, session age, last
activity timestamp.

---

### Action tools (write)

These modify the database. All are gated behind explicit confirmation in the
tool description so Claude will describe what it's about to do before executing.

#### `maestro__append_task_description(task_id: str, text: str) -> str`

Appends `\n\n{text}` to the task's `description` field. Does not replace existing
content. Used to add scope notes, caveats, or constraints without wiping the
original description.

Returns: `"OK: appended {len} chars to task '{task_id}'"`

---

#### `maestro__patch_planning_fields(result_id: int, fields: dict) -> str`

Wraps `update_planning_result` with the same field whitelist as `update_plan_fields`
in `tools.py`:
`interface_contracts`, `dependency_graph`, `file_manifest`, `test_strategy`,
`implementation_steps`.

Allows direct correction of gate-failing plan fields without going through the
correction agent.

Returns: `"Updated fields: ['interface_contracts']"`

---

#### `maestro__set_task_type(task_id: str, type: str) -> str`

Force a task to any pipeline stage. Wraps `update_task(type=...)`. Equivalent to
the `/api/tasks/{id}/set-stage` endpoint but callable from Claude's tool loop
without needing `curl`.

Allowed types: `idea`, `planning`, `indev`, `conceptual_review`, `optimization`,
`security`, `full_review`, `completed`.

Returns: `"OK: task '{task_id}' set to type '{type}'"`

---

#### `maestro__append_task_history(task_id: str, note: str) -> str`

Appends a `{role: "system", content: note}` entry to the task's history JSON.
Useful for leaving a diagnostic breadcrumb when manually intervening.

---

## Implementation

### `mcp_server.py` (entry point)

```python
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP
from mcp_tools.diagnostics import (
    diagnose_task, get_scheduler_state, get_budget_trace,
    list_tasks, get_gate_history, get_agent_sessions, find_stuck_tasks,
)
from mcp_tools.actions import (
    append_task_description, patch_planning_fields,
    set_task_type, append_task_history,
)

mcp = FastMCP("maestro", description="TheMaestro Kanban + LLM orchestration diagnostics")

mcp.tool()(diagnose_task)
mcp.tool()(get_scheduler_state)
mcp.tool()(get_budget_trace)
mcp.tool()(list_tasks)
mcp.tool()(get_gate_history)
mcp.tool()(get_agent_sessions)
mcp.tool()(find_stuck_tasks)
mcp.tool()(append_task_description)
mcp.tool()(patch_planning_fields)
mcp.tool()(set_task_type)
mcp.tool()(append_task_history)

if __name__ == "__main__":
    mcp.run()
```

---

### `mcp_tools/helpers.py`

```python
import json, sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "kanban.db"

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn

def extract_finish_reason(response_data: str) -> tuple[str, str, str]:
    """Returns (finish_reason, content_preview, reasoning_preview)."""
    try:
        data = json.loads(response_data or "{}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        return (
            choice.get("finish_reason", ""),
            (msg.get("content") or "")[:300],
            (msg.get("reasoning_content") or "")[:200],
        )
    except Exception:
        return ("", "", "")

def parse_gate_checks(vote_summary: str) -> list:
    """Extract gate_checks array from a transition_result vote_summary blob."""
    try:
        data = json.loads(vote_summary or "{}")
        return data.get("checks", [])
    except Exception:
        return []
```

---

### `mcp_tools/diagnostics.py` (key tool — diagnose_task)

```python
import json
from .helpers import get_conn, extract_finish_reason, parse_gate_checks

def diagnose_task(task_id: str) -> dict:
    """
    Complete diagnostic snapshot for a task: current type, active sessions,
    recent budget entries with finish_reason extracted, planning gate history,
    and correction agent session history. Replaces 4+ separate DB queries.
    """
    conn = get_conn()
    try:
        # Task row
        task_row = conn.execute(
            "SELECT id, title, type, project, description FROM tasks WHERE id=?",
            (task_id,)
        ).fetchone()
        if not task_row:
            return {"error": f"Task '{task_id}' not found."}

        task = dict(task_row)
        task["description"] = (task["description"] or "")[:500]

        # Active sessions
        active = conn.execute(
            "SELECT agent_type, started_at FROM agent_sessions "
            "WHERE task_id=? AND ended_at IS NULL ORDER BY id DESC",
            (task_id,)
        ).fetchall()

        # Recent completed sessions
        recent = conn.execute(
            "SELECT agent_type, exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND ended_at IS NOT NULL "
            "ORDER BY id DESC LIMIT 8",
            (task_id,)
        ).fetchall()

        # Planning result
        pr = conn.execute(
            "SELECT id, status, correction_attempts, gate_checks, created_at "
            "FROM planning_results WHERE task_id=? ORDER BY id DESC LIMIT 1",
            (task_id,)
        ).fetchone()

        planning = None
        if pr:
            try:
                gate_checks = json.loads(pr["gate_checks"] or "[]")
            except Exception:
                gate_checks = []
            planning = {
                "result_id": pr["id"],
                "status": pr["status"],
                "correction_attempts": pr["correction_attempts"],
                "gate_checks": gate_checks,
                "created_at": pr["created_at"],
            }

        # Gate transition history
        gate_rows = conn.execute(
            "SELECT outcome, vote_summary, created_at FROM transition_results "
            "WHERE task_id=? AND transition='planning_gate' ORDER BY id DESC LIMIT 5",
            (task_id,)
        ).fetchall()
        gate_transitions = []
        for g in gate_rows:
            checks = parse_gate_checks(g["vote_summary"])
            gate_transitions.append({
                "outcome": g["outcome"],
                "created_at": g["created_at"],
                "checks": checks,
            })

        # Budget trace
        budget_rows = conn.execute(
            "SELECT id, agent_name, prompt_cost, generation_cost, response_data, created_at "
            "FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 15",
            (task_id,)
        ).fetchall()
        budget_trace = []
        for b in budget_rows:
            fr, content, reasoning = extract_finish_reason(b["response_data"])
            budget_trace.append({
                "id": b["id"],
                "agent_name": b["agent_name"],
                "finish_reason": fr,
                "content_preview": content,
                "reasoning_preview": reasoning,
                "prompt_cost": b["prompt_cost"],
                "generation_cost": b["generation_cost"],
                "created_at": b["created_at"],
            })

        # Correction sessions
        correction = conn.execute(
            "SELECT exit_reason, exit_summary, started_at, ended_at "
            "FROM agent_sessions WHERE task_id=? AND agent_type='planning_correction' "
            "ORDER BY id DESC LIMIT 5",
            (task_id,)
        ).fetchall()

        # Activity status
        if active:
            activity_status = "active"
        elif budget_trace:
            activity_status = "idle"
        else:
            activity_status = "no_activity"

        return {
            "task": task,
            "active_sessions": [dict(r) for r in active],
            "recent_sessions": [dict(r) for r in recent],
            "planning": planning,
            "gate_history": gate_transitions,
            "budget_trace": budget_trace,
            "correction_sessions": [dict(r) for r in correction],
            "activity_status": activity_status,
        }
    finally:
        conn.close()
```

---

### `mcp_tools/actions.py`

```python
from .helpers import get_conn

def append_task_description(task_id: str, text: str) -> str:
    """
    Appends text to a task's description. Does not replace existing content.
    Use for adding scope notes, constraints, or caveats before the next planning run.
    """
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    new_desc = (task.description or "") + f"\n\n{text}"
    update_task(task_id, description=new_desc)
    return f"OK: appended {len(text)} chars to task '{task_id}'."

def patch_planning_fields(result_id: int, fields: dict) -> str:
    """
    Patch fields on a planning_results row. Allowed: interface_contracts,
    dependency_graph, file_manifest, test_strategy, implementation_steps.
    """
    import json
    ALLOWED = {"interface_contracts", "dependency_graph", "file_manifest",
               "test_strategy", "implementation_steps"}
    invalid = set(fields.keys()) - ALLOWED
    if invalid:
        return f"ERROR: Invalid fields: {sorted(invalid)}. Allowed: {sorted(ALLOWED)}"
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import update_planning_result
    from app.database.session import SessionLocal
    serialized = {k: (v if isinstance(v, str) else json.dumps(v)) for k, v in fields.items()}
    db = SessionLocal()
    try:
        result = update_planning_result(db, result_id, **serialized)
        if result is None:
            return f"ERROR: planning_result id={result_id} not found."
        return f"Updated fields: {sorted(serialized.keys())}"
    finally:
        db.close()

def set_task_type(task_id: str, type: str) -> str:
    """Force a task to any pipeline stage."""
    ALLOWED_TYPES = {"idea","planning","indev","conceptual_review","optimization",
                     "security","full_review","completed","architecture"}
    if type not in ALLOWED_TYPES:
        return f"ERROR: Invalid type '{type}'. Allowed: {sorted(ALLOWED_TYPES)}"
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import update_task
    result = update_task(task_id, type=type)
    if result is None:
        return f"ERROR: Task '{task_id}' not found."
    return f"OK: task '{task_id}' set to type '{type}'."

def append_task_history(task_id: str, note: str) -> str:
    """Append a diagnostic note to a task's history."""
    import json, sys, os
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from app.database import get_task, update_task
    task = get_task(task_id)
    if not task:
        return f"ERROR: Task '{task_id}' not found."
    history = json.loads(task.history or "[]")
    history.append({"role": "system", "content": note, "source": "claude-mcp"})
    update_task(task_id, history=json.dumps(history))
    return f"OK: history note appended to task '{task_id}'."
```

---

## Testing the server before registration

```bash
# Verify it starts without errors
venv/Scripts/python.exe mcp_server.py

# Use mcp dev mode to inspect tools interactively
venv/Scripts/python.exe -m mcp dev mcp_server.py
```

`mcp dev` launches a local inspector UI at `http://localhost:5173` where you can
call each tool manually and inspect inputs/outputs before wiring it into Claude.

---

## Verification after registration

In a Claude Code session in this project:

```
/mcp
```

Should show:
```
maestro    connected    11 tools
```

Then: "Call maestro__diagnose_task with task_id task-1776548777.749239" should
return the full JSON snapshot without any subprocess, SQL, or inspect_cards.py invocation.

---

## What this replaces

| Current workflow | With MCP |
|---|---|
| `venv/Scripts/python.exe -c "import sqlite3..."` × 4 queries | `maestro__diagnose_task(task_id)` × 1 |
| Parse ASCII from `inspect_cards.py scheduler` | `maestro__get_scheduler_state()` → structured JSON |
| Construct SQL to check budget entries | `maestro__get_budget_trace(task_id)` |
| `curl localhost:8000/api/scheduler/status` + parse | `maestro__get_scheduler_state()` |
| Direct DB UPDATE to fix task description | `maestro__append_task_description(task_id, text)` |
| Multi-step gate failure diagnosis | `maestro__get_gate_history(task_id)` |

---

## Execution order

1. `venv/Scripts/python.exe -m pip install "mcp[cli]"` — install dependency
2. Create `mcp_tools/__init__.py`, `mcp_tools/helpers.py`, `mcp_tools/diagnostics.py`, `mcp_tools/actions.py`
3. Create `mcp_server.py`
4. `venv/Scripts/python.exe -m mcp dev mcp_server.py` — test each tool interactively
5. Add `.claude/settings.json` with server registration
6. Restart Claude Code; run `/mcp` to verify connected
7. Optionally add to `claude_desktop_config.json` for Desktop use
8. Add `mcp[cli]` to `requirements.txt`
