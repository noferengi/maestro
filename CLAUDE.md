# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED, gated by a multi-stage intake pipeline with LLM voting.

## MCP server — primary diagnostic interface

A `maestro` MCP server is registered in `.mcp.json` and enabled via `.claude/settings.local.json`.
**Prefer MCP tools over raw SQL queries or Bash scripts for all diagnostic and admin tasks.**

Run `/mcp` to confirm the server is connected (should show `maestro  connected` with tool list).
If disconnected, restart Claude Code.

### Default monitoring behavior

When asked to watch, monitor, or babysit Maestro, the default workflow is `/loop` with `monitor()`:

```
/loop
```

Each iteration calls `monitor()` with no arguments, blocks for the window defined in
`maestro.ini [monitor] duration_seconds` (default 5 minutes), then returns a structured
report. Review the report, take any corrective actions using the action tools below, then
the loop fires again automatically.

To run a single 5-minute monitoring window without looping:

```
mcp__maestro__monitor(duration_seconds=300)
```

The report includes new budget entries, session starts/completions, stage changes, and
five pattern flags: `rapid_cycling`, `token_limited`, `zombie_sessions`, `stage_thrash`,
`tool_call_storms`. When a flag fires, drill in with `diagnose_task` or `get_budget_entry_full`.

### When to use which tool

| Goal | Tool |
|---|---|
| Watch activity over time | `monitor()` — blocks N seconds, returns diff report + pattern flags |
| Why is task X stuck? | `diagnose_task(task_id)` — one call, complete picture |
| What's running right now? | `get_scheduler_state()` (DB) + `get_scheduler_api_status()` (live API) |
| Find tasks with no recent LLM activity | `find_stuck_tasks(idle_minutes=10)` |
| Inspect raw LLM call history | `get_budget_trace(task_id, n=20)` |
| Read full prompt/response for one LLM call | `get_budget_entry_full(entry_id)` |
| Check planning gate failure history | `get_gate_history(task_id)` |
| See full plan content (interface_contracts etc.) | `get_planning_result(task_id)` |
| List tasks by project or type | `list_tasks(project="Garden", type="planning")` |
| Add scope note to task description | `append_task_description(task_id, text)` |
| Fix interface_contracts / file_manifest in a plan | `patch_planning_fields(result_id, fields_dict)` |
| Force a task to a pipeline stage (no demotion record) | `set_task_type(task_id, "planning")` |
| Move task backward with demotion record | `demote_task(task_id, target_stage?)` |
| Trigger planning pipeline manually | `trigger_planning_run(task_id)` |
| Trigger review / security / full_review | `run_pipeline_stage(task_id, stage)` |
| Stop a running MaestroLoop | `stop_agent(task_id)` |
| Restart the Maestro server | `restart_server()` — drains sessions, waits ~60 s |
| Anything not covered above | `run_inspect_cards(section, extra_args)` |

### Key signal from `diagnose_task`

- `activity_status: "active — last LLM call at ..."` → session running normally
- `activity_status: "active — no budget entries yet"` → in survey phase or waiting for LLM slot
- `activity_status: "idle"` → session is a zombie (server restart); task needs re-dispatch
- `budget_trace[0].finish_reason == "length"` + empty `content_preview` → max_tokens too low for reasoning model
- `correction_sessions` present → PlanningCorrectionAgent has run; check `exit_reason`
- `planning.correction_attempts > 0` → gate has failed and correction was attempted

## Shell / path conventions (Windows)

The shell is bash. Use **forward slashes** — backslashes are treated as escape characters and
silently dropped, mushing the path together:

```
# Wrong
venv\Scripts\python.exe -m pytest app/tests/ -q
→ /usr/bin/bash: line 1: venvScriptspython.exe: command not found

# Correct
venv/Scripts/python.exe -m pytest app/tests/ -q
```

## Running the server

```bash
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000
```

**To restart a running server** — use the MCP tool, not a shell command:

```
mcp__maestro__restart_server()
```

Wait ~60 seconds after triggering. The server drains active sessions before exiting; the
`Launcher.ps1` process detects `restart.flag` and relaunches uvicorn automatically.
Do **not** use `pkill` or `Bash` to kill the process — that bypasses session drain.

Board is at `http://localhost:8000/`. LLM endpoints are configurable per-task via the UI (managed in the `llms` table). Default expects `llama.cpp` on `http://localhost:8008/v1` (OpenAI-compatible).

## Running tests

```bash
venv/Scripts/python.exe -m pytest app/tests/ -v
venv/Scripts/python.exe -m pytest app/tests/test_repl.py -v      # single file
venv/Scripts/python.exe -m pytest app/tests/test_repl.py -k "test_name" -v  # single test
```

## Database migrations

Use `/migrate` to check status or apply pending migrations — it wraps the commands below and
keeps things consistent. Prefer the skill over running the commands manually.

```bash
migrate.bat status      # see applied vs pending
migrate.bat migrate     # apply pending migrations
migrate.bat reset       # DESTRUCTIVE: drop everything, re-migrate, re-seed
```

Or directly: `venv/Scripts/python.exe app/migrations/runner.py <command>`

Migrations live in `app/migrations/versions/` as `NNNN_description.py`. Never edit an existing migration — always add a new one. Each exposes `up(conn)`, `down(conn)`, and `description`.

**Full schema reference:** See `CLAUDE_SCHEMA.md` in the project root — every table, column, type, nullability, and default value.

## Debugging scheduler and card status

Use `scripts/inspect_cards.py` to diagnose why cards aren't progressing. All output is ASCII-safe (Windows cp1252 terminal compatible).

```bash
venv/Scripts/python.exe scripts/inspect_cards.py                  # overview: all cards, transitions, subdivision records
venv/Scripts/python.exe scripts/inspect_cards.py prereqs          # prerequisite chain analysis — blocked/satisfied/phantom IDs
venv/Scripts/python.exe scripts/inspect_cards.py scheduler        # simulated scheduler state: READY/BLOCKED/PARENT_SKIP/DONE_SKIPPED/STUCK_SUBDIVIDING
venv/Scripts/python.exe scripts/inspect_cards.py activity         # recent LLM activity timeline + idle dispatchable tasks
venv/Scripts/python.exe scripts/inspect_cards.py activity --hours 48  # look back 48 hours
venv/Scripts/python.exe scripts/inspect_cards.py votes            # transition vote detail for all tasks
venv/Scripts/python.exe scripts/inspect_cards.py votes --task <id>   # votes for a specific task
venv/Scripts/python.exe scripts/inspect_cards.py budget           # LLM capacity and budget spending summary
venv/Scripts/python.exe scripts/inspect_cards.py children         # parent->child tree with LLM activity counts
venv/Scripts/python.exe scripts/inspect_cards.py all              # run all sections
```

Key diagnostics to check first when cards are stuck:
1. `scheduler` — shows READY (should dispatch), BLOCKED (waiting on prereqs), STUCK_SUBDIVIDING (needs recovery)
2. `prereqs` — reveals transitive DAG locks and phantom prerequisite IDs
3. `activity --hours 4` — confirms the scheduler is actually dispatching tasks

For stuck planning tasks use `diagnose_task(task_id)` — covers budget traces, gate history, and session state in one call.

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists). Quick-action endpoints: `/demote`, `/set-stage`, `/clone`, `/pin`, `/run-planning`, `/run-review`, `/run-security`, `/run-full-review`. Task serialization (`_task_to_dict`) always includes a `"pips"` array. `sync_update_llm_with_cache` / `sync_delete_llm_with_cache` call `invalidate_llm_cache` after LLM record mutations so stale context/capacity state is flushed immediately.
- `database.py` — SQLAlchemy models + all DB CRUD functions. `delete_task()` is a **soft-delete**: sets `is_active=False` on the target and all descendants via BFS. All read queries filter `is_active=True`. `upsert_project()` uses `...` (Ellipsis) sentinel for `llm_id`/`budget_id` — pass Ellipsis to leave unchanged, None to clear. PIP CRUD and resolution job CRUD are in `crud_tasks.py`. `pip_status_at_stage(pip, stage)` derives status at read time — no stored status column.
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.

### Agent system (`app/agent/`)

See **`app/agent/CLAUDE.md`** for per-file descriptions, key invariants, and project isolation details.

### Frontend (`app/web/`)

See **`app/web/CLAUDE.md`** for board, arch bar, column map, diagnostics viewer, and CSS reference.

### Configuration (`maestro.ini`)
INI file with sections: `[intake]`, `[subdivision]`, `[capacity]`, `[context_warnings]`, `[scheduler]`, `[verdicts]`, `[search]`, `[pip]`.

- `[intake]` — research lives, tiebreaker, allowed research tools, `context_budget_ratio` (fraction of context window for research agent, default 0.60). `research_agent_tools` includes `web_search` — dispatches `WebSearchAgent` asynchronously (search + fetch + synthesize). `web_fetch` is intentionally absent; it is private to `WebSearchAgent`.
- `[subdivision]` — max_depth, max_retries_per_level, max_total_sub_ideas, subdivision_agent_tools, `context_budget_ratio` (default 0.60). Both `subdivision_agent_tools` and `subdivision_planning_tools` include `web_search` for domain research during decomposition.
- `[search]` — `provider` (duckduckgo | brave, default duckduckgo), `brave_api_key` (required only if provider=brave). Env overrides: `MAESTRO_SEARCH_PROVIDER`, `BRAVE_API_KEY`.
- `[pip]` — `resolution_max_turns` (default: 20, max turns for `PIPResolutionAgent` before it auto-stalls).

### Data flow
Tasks are per-project. Switching projects calls `loadTasksFromDatabase()` which re-fetches `/api/projects/{name}/tasks` and fully rebuilds `taskData`. `renderTasksFromDatabase()` groups tasks by type, sorts each group by `position`, appends pipeline cards to their column containers, and calls `renderArchBar()` to rebuild the architecture bar. Architecture tasks (`type='architecture'`) are excluded from the pipeline columns array and rendered only in the arch bar. Drag-and-drop reorder POSTs to `/api/tasks/{id}/reorder`, then re-fetches the full project task list to get authoritative positions before re-rendering. When `columnMapActive` is true, `reconcile()` only refreshes `taskData` and skips DOM reconciliation.

### Key API routes
```
GET    /api/projects                      — list projects (name, path, description, llm_id, budget_id)
POST   /api/projects                      — create project
PUT    /api/projects/{name}               — update project (llm_id/budget_id use Ellipsis sentinel)
DELETE /api/projects/{name}               — delete project record
GET    /api/projects/{project_name}/tasks — all tasks for a project (active only)
POST   /api/tasks                         — create task (include project field)
PUT    /api/tasks/{id}                    — update task
DELETE /api/tasks/{id}                    — soft-delete: sets is_active=False on task + all descendants; returns {deactivated: N}
POST   /api/tasks/{id}/reorder            — {position, type} — reorder within column
PATCH  /api/tasks/map-positions           — [{id, map_x, map_y}] — bulk-save 2D positions (no history)
POST   /api/tasks/{task_id}/advance       — trigger intake pipeline (IDEA→PLANNING)
GET    /api/tasks/{task_id}/transition-status — latest transition result + vote history
POST   /api/tasks/{task_id}/demote        — move one stage backward; optional body {target} to force a stage; records demotion
POST   /api/tasks/{task_id}/set-stage     — {stage} force to any pipeline stage (no demotion record)
POST   /api/tasks/{task_id}/clone         — duplicate as new IDEA in same project
POST   /api/tasks/{task_id}/pin           — set position=0 (top of column)
POST   /api/tasks/{task_id}/run-planning  — trigger PlanningPipeline + gate in background
POST   /api/tasks/{task_id}/run-review    — trigger ConceptualReviewPipeline in background
POST   /api/tasks/{task_id}/run-security  — trigger OptimizationPipeline + SecurityPipeline in background
POST   /api/tasks/{task_id}/run-full-review — trigger FullReviewPipeline in background
POST   /api/agent/run/{task_id}           — start MaestroLoop (background)
GET    /api/agent/status/{task_id}        — loop status
POST   /api/agent/stop/{task_id}          — request graceful stop (MaestroLoop only; pipeline agents are not stoppable)
GET    /api/agent/tasks/ready             — DAG-ready tasks
GET    /api/scheduler/status              — scheduler state
CRUD   /api/llms, /api/llms/{id}          — LLM endpoint management (compute_node_id accepted in create/update)
CRUD   /api/budgets, /api/budgets/{id}    — budget management
CRUD   /api/compute-nodes, /api/compute-nodes/{id} — compute node management
GET    /api/budget-entries                — budget entry listing; task_id=__file_summaries__ returns null-task entries
GET    /api/budget-entries/{id}/full      — single entry with full prompt/response
GET    /api/budgets/{id}/summary          — aggregated budget usage
GET    /api/tasks/{id}/children           — direct child tasks of a subdivided task
GET    /api/tasks/{id}/subdivision-records — audit trail of subdivision attempts
GET    /api/diagnostics/tasks             — tasks with LLM activity + synthetic __file_summaries__ row
GET    /api/projects/{name}/arch-gen-jobs — pending/running arch gen jobs [{id, category, status, created_at, retry_count}]
GET    /api/tasks/{id}/pips               — full PIP list with verification history per PIP (for PIP detail modal)
```

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **Named shell tools replace grouped `run_shell_*` tools.** Each tool does exactly one operation with no hidden allowlist for agents to guess. Key tools: `run_pytest`, `run_mypy`, `run_ruff`, `run_black_check`, `run_unittest`, `run_npm_test`, `run_cargo_test`, `run_go_test` (testing); `run_make`, `run_cargo_build`, `run_go_build`, `run_npm_build`, `run_tsc` (build); `run_pip_install`, `run_npm_install`, `run_cargo_fetch` (deps); `run_bandit`, `run_pip_audit`, `run_semgrep`, `run_npm_audit` (security); `git_restore`, `git_add`, `git_unstage` (git helpers). Per-stage access controlled by `build_tool_schemas(allowed_names)` in `config.py`.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
