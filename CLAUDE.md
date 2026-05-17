# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Project Maestro — a general-purpose agentic workflow platform. The kanban board is the
face of an LLM orchestration backend that drives any workflow through a user-defined
pipeline of AI agents.

**The pipeline is malleable.** Stage definitions, agent assignments, transition edges,
and kanban columns are all stored in the DB as `pipeline_templates` — not hardcoded in
Python or JS. A visual **Litegraph.js node editor** at `/pipelines/{id}/edit` lets you
draw stages, wire transitions, and use ⚡ to generate system prompts. The kanban board
derives its columns from whatever template the project uses.

**Built-in templates** ship out of the box: Software Development (the original pipeline),
Novel Writing, Research Report, Data Analysis, Mathematics/Proof Exploration, Bug Triage,
and an Overnight Story Factory. Templates can be cloned, edited, exported as JSON, and
shared.

**Direction:** The hardcoded Software Development pipeline (`IDEA → PLANNING → INDEV →
CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FINAL_REVIEW → HUMAN_REVIEW → COMPLETED`)
is now the "Software Development" built-in template. `task.type` is being phased out in
favor of `task.stage_key`; both are kept in sync during the transition. New features
should be designed for the malleable system, not the hardcoded pipeline.

**See `ARCHITECTURE.md`** for the full system reference: compute resource model, scheduler
tick lifecycle, all agent types, git worktree isolation, and safety layers. **See
`CLAUDE_PIPELINE.md`** for the malleable pipeline system reference (templates, agent
registry, CRUD API, editor, document store, card factory, autopilot). **See
`plans/PRD.md`** for the product roadmap.

## Environment & configuration

**Database:** Configured via `.env` (loaded by `python-dotenv` in `app/agent/config.py`).
Copy `.env.example` to `.env` and fill in credentials. Key variables:

```
MAESTRO_USE_POSTGRES=false               # set true to use PostgreSQL
MAESTRO_DATABASE_URL=postgresql://...    # required when use_postgres=true
MAESTRO_ADMIN_DATABASE_URL=postgresql:// # used by migration runner (needs schema perms)
TAVILY_API_KEY=                          # optional; for Tavily web search
BRAVE_API_KEY=                           # optional; for Brave web search
MAESTRO_TEST_DB=data/test.db            # set in tests to use SQLite, never production
```

Config load order (highest priority wins): env vars → `maestro.ini` → built-in defaults.

**maestro.ini sections:** `[intake]`, `[subdivision]`, `[capacity]`, `[context_warnings]`,
`[scheduler]`, `[verdicts]`, `[search]`, `[pip]`, `[monitor]`, `[pipeline_editor]`.
`[pipeline_editor] llm_id` pins a cheap fast model for ⚡ field-generation calls.

**Full schema reference:** `CLAUDE_SCHEMA.md` — every table, column, type, nullability,
and default value. **Pipeline system reference:** `CLAUDE_PIPELINE.md`.

## Maestro Server

- **Maestro Server:** Assume the server is ALWAYS running on http://localhost:8000. You can verify this with a simple `curl` or `requests.get`. To apply backend code changes, use the `restart_server` tool.

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
| **Orient at session start** | `get_project_health(project?)` — stage counts, active sessions, spend, demotions, pending merges |
| How many LLM slots are free right now? | `get_capacity_status()` — per-node/LLM used/free/total table |
| What's ready to merge? | `list_pending_merges(project?)` — completed tasks with no merge_commit_sha |
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
| Trigger review / security / final_review | `run_pipeline_stage(task_id, stage)` |
| Stop a running MaestroLoop | `stop_agent(task_id)` |
| Restart the Maestro server | `restart_server()` — drains sessions, waits ~60 s |
| Anything not covered above | `run_inspect_cards(section, extra_args)` |

### When MCP tool calls hang or return no result

MCP tools in this project are **synchronous** and make direct PostgreSQL calls with no timeout
configured (`mcp_tools/helpers.py:15-26`). If the scheduler is holding a write lock (e.g.,
committing a task update, running an intake vote, persisting budget entries), MCP reads queue
behind it and can appear to hang.

**Root causes (structural — can happen any time):**
- `mcp_tools/helpers.py` — `get_conn()` / `get_rw_conn()` have no `timeout=` argument
- `app/database/session.py` — no `connect_args={"connect_timeout": N}` on the SQLAlchemy engine
- High-query tools (`get_scheduler_state`, `diagnose_task`, `find_stuck_tasks`) issue 4–8+
  SELECT statements per call, each of which must wait for any in-progress write lock

**What to do when a tool call silently stalls:**
1. Check the server log for active LLM calls — if several are in-flight the DB is under write
   pressure; wait a few seconds and retry the tool call
2. If retrying fails, use `restart_server()` to drain sessions and clear lock contention
3. Fall back to `venv/Scripts/python.exe scripts/inspect_cards.py <section>` for the same
   diagnostic information without going through the MCP layer

### Key signal from `diagnose_task`

- `activity_status: "active — last LLM call at ..."` → session running normally
- `activity_status: "active — no budget entries yet"` → in survey phase or waiting for LLM slot
- `activity_status: "idle"` → session is a zombie (server restart); task needs re-dispatch
- `budget_trace[0].finish_reason == "length"` + empty `content_preview` → max_tokens too low for reasoning model
- `correction_sessions` present → PlanningCorrectionAgent has run; check `exit_reason`
- `planning.correction_attempts > 0` → gate has failed and correction was attempted

## Shell / path conventions (Windows)

The primary environment is **Windows 11 PowerShell**.

- **PATH**: `C:\Program Files\Git\usr\bin` is included in the PATH, which means Unix-style utilities like `grep`, `ls`, `head`, and `tail` are available. However, PowerShell-native commands (e.g., `Select-String`, `Get-ChildItem`) are often more reliable for complex pipes in this specific terminal context.
- **Inference Hardware**: The local inference engine (`llama.cpp`) hosting LLM 1 (Qwen 3.6 35B) supports 5 parallel sessions, but **application usage must be limited to 4 sessions** to always keep one spare slot for the Maestro orchestrator and high-priority discovery tasks.
- **Config Data**: Other LLM endpoints (IDs > 1) in the `llms` table are fictional test data used for configuration validation; only LLM 1 is backed by real hardware.

If you are **Claude**, the shell is virtual bash environment. Use **forward slashes** — backslashes are treated as escape characters and
silently dropped, mushing the path together:

```
# Wrong
venv\Scripts\python.exe -m pytest app/tests/ -q
→ /usr/bin/bash: line 1: venvScriptspython.exe: command not found

# Correct
venv/Scripts/python.exe -m pytest app/tests/ -q
```

If you are **Qwen**, the shell is actually Windows Powershell. Use **backward slashes** — forward slashes are instead
silently dropped, mushing the path together:

```
# Wrong
venv/Scripts/python.exe -m pytest app/tests/ -q
→ line 1: venvScriptspython.exe: command not found

# Correct
venv\Scripts\python.exe -m pytest app\tests\ -q
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
venv/Scripts/python.exe -m pytest app/tests/test_planning_unit.py -v      # single file
venv/Scripts/python.exe -m pytest app/tests/test_planning_unit.py -k "test_name" -v  # single test
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

To scaffold the next migration file automatically:

```bash
venv/Scripts/python.exe scripts/create_migration.py "your migration name"
# prints the path of the created file, e.g. app/migrations/versions/0055_your_migration_name.py
```

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

## Direct PostgreSQL access

`scripts/psql.py` — runs SQL against the live database using credentials from `.env`. No
connection string needed; the script loads `MAESTRO_DATABASE_URL` (app user, default) or
`MAESTRO_ADMIN_DATABASE_URL` (`--admin`, required for VACUUM / DDL).

```bash
# Common one-liners
venv/Scripts/python.exe scripts/psql.py --list-tables               # all tables: size, live/dead rows
venv/Scripts/python.exe scripts/psql.py --budget-entries            # heap vs TOAST vs indexes for budget_entries
venv/Scripts/python.exe scripts/psql.py "SELECT count(*) FROM tasks WHERE is_active"
venv/Scripts/python.exe scripts/psql.py --admin "SELECT pg_size_pretty(pg_database_size('maestro_db'))"
venv/Scripts/python.exe scripts/psql.py --vacuum-full budget_entries  # VACUUM FULL shortcut (admin + autocommit)
echo "SELECT version()" | venv/Scripts/python.exe scripts/psql.py --admin -  # stdin
```

Use `--admin` for anything that needs DDL privileges: `VACUUM FULL`, `ALTER TABLE`,
`GRANT`, `REINDEX`. The `--vacuum-full <table>` shorthand handles autocommit automatically
and prints the new size when done.

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists) + `_check_builtin_templates()` (drift warning). Quick-action endpoints: `/demote`, `/set-stage`, `/clone`, `/pin`, `/run-planning`, `/run-review`, `/run-security`, `/run-final-review`. Task serialization (`_task_to_dict`) always includes a `"pips"` array. `sync_update_llm_with_cache` / `sync_delete_llm_with_cache` call `invalidate_llm_cache` after LLM record mutations.
- `database/` — DB package. See `app/database/CLAUDE.md` for full file map. Core modules: `models.py` (all ORM models), `crud_tasks.py` (task + PIP CRUD), `crud_projects.py`, `crud_infra.py`, `crud_costs.py` (budget entries store **deltas** only since migration 0076; `reconstruct_messages_for_entry(entry_id, db)` accumulates them back to full history), `crud_pipeline.py`, `crud_jobs.py`, `crud_files.py`, `crud_malleable.py` (pipeline templates, stages, transitions, arch categories, custom agent defs, system_settings, project_settings — 50+ functions), `crud_documents.py` (project document store), `crud_factory.py` (factory_runs audit), `session.py`. `delete_task()` is a **soft-delete** (BFS, sets `is_active=False`). `upsert_project()` uses `...` (Ellipsis) sentinel for `llm_id`/`budget_id`.
- `migrations/runner.py` — PostgreSQL migration engine (SQLite only for tests via `MAESTRO_TEST_DB`). Latest migrations: 0074 (expand AI review group), 0075 (custom agent definition extensions), 0076 (budget_entries delta storage — adds `prompt_message_count`).

### Agent system (`app/agent/`)

See **`app/agent/CLAUDE.md`** for per-file descriptions, key invariants, and project isolation details. New files since the malleable pipeline work: `pipeline_router.py` (stage transitions), `agent_registry.py` (AGENT_REGISTRY dict), `custom_llm_agent.py` (DB-driven agent), `verifiers.py` (formal verification gate), `workspace.py` (deletion-protected file ops), `doc_store.py` (shared document store), `card_factory.py` + `factory_sources.py` (batch card creation from external data).

### Frontend (`app/web/`)

See **`app/web/CLAUDE.md`** for board, arch bar, column map, diagnostics viewer, and CSS reference. New pages: `pipeline_editor.html` / `pipeline_editor.js` / `pipeline_editor.css` (Litegraph canvas editor at `/pipelines/{id}/edit`), `gallery.html` (template gallery at `/pipelines`).

### Configuration (`maestro.ini`)
INI file. Key sections:

- `[intake]` — research lives, tiebreaker, allowed research tools, `context_budget_ratio` (default 0.60). `research_agent_tools` includes `web_search` (dispatches `WebSearchAgent`). `web_fetch` is private to `WebSearchAgent`.
- `[subdivision]` — max_depth, max_retries_per_level, max_total_sub_ideas, `context_budget_ratio` (default 0.60).
- `[search]` — `provider` (duckduckgo | brave | tavily). Env overrides: `MAESTRO_SEARCH_PROVIDER`, `BRAVE_API_KEY`, `TAVILY_API_KEY`.
- `[pip]` — `resolution_max_turns` (default: 20).
- `[monitor]` — `duration_seconds` (default: 300, blocking window for `mcp__maestro__monitor()`).
- `[pipeline_editor]` — `llm_id` (cheap fast model for ⚡ field-generation calls; optional).

### Data flow
Tasks are per-project. Switching projects calls `loadTasksFromDatabase()` which re-fetches `/api/projects/{name}/tasks`, the project's active pipeline template (`/api/pipelines/{id}`), and arch categories (`/api/projects/{name}/arch-categories`). `renderTasksFromDatabase()` derives kanban columns from `activePipelineTemplate.stages` (sorted by `position`) — no hardcoded column list. Architecture tasks (`type='architecture'`) are excluded from pipeline columns and rendered only in the arch bar. Drag-and-drop reorder POSTs to `/api/tasks/{id}/reorder`. When `columnMapActive` is true, `reconcile()` skips DOM reconciliation.

### Key API routes
```
# Projects & tasks
GET    /api/projects                      — list projects
POST   /api/projects                      — create project
PUT    /api/projects/{name}               — update (llm_id/budget_id use Ellipsis sentinel)
DELETE /api/projects/{name}               — delete project record
GET    /api/projects/{project_name}/tasks — all tasks (active only)
POST   /api/tasks                         — create task
PUT    /api/tasks/{id}                    — update task
DELETE /api/tasks/{id}                    — soft-delete (BFS); returns {deactivated: N}
POST   /api/tasks/{id}/reorder            — {position, type}
PATCH  /api/tasks/map-positions           — [{id, map_x, map_y}] bulk-save
POST   /api/tasks/{task_id}/advance       — intake pipeline (IDEA→first stage)
POST   /api/tasks/{task_id}/demote        — backward; optional {target}; records demotion
POST   /api/tasks/{task_id}/set-stage     — {stage} force (no demotion record)
POST   /api/tasks/{task_id}/clone / pin / run-planning / run-review / run-security / run-final-review

# Malleable pipeline (full CRUD — see CLAUDE_PIPELINE.md for detail)
GET/POST/PUT/DELETE  /api/pipelines[/{id}]
GET/POST/PUT/DELETE  /api/pipelines/{id}/stages[/{stage_id}]
POST                 /api/pipelines/{id}/stages/{stage_id}/delete-with-redirect
GET/POST/PUT/DELETE  /api/pipelines/{id}/transitions[/{t_id}]
GET/POST/PUT/DELETE  /api/pipelines/{id}/groups[/{g_id}]
GET/POST/PUT/DELETE  /api/pipelines/{id}/arch-categories[/{c_id}]
GET/PUT/DELETE       /api/projects/{name}/documents[/{key}]    — document store
GET                  /api/projects/{name}/arch-categories      — active template categories
POST                 /api/projects/{name}/pipeline             — assign template (body: {template_id})
POST                 /api/projects/{name}/use-template         — alias for /pipeline
GET/POST/PUT/DELETE  /api/agent-definitions[/{id}]             — custom agent definitions
GET/POST             /api/settings/autopilot                   — autopilot toggle + mission
GET/POST             /api/projects/{name}/settings             — per-project overrides
GET                  /api/pipelines/{id}/export                — JSON
POST                 /api/pipelines/import                     — JSON → new template
POST                 /api/pipelines/generate-field             — ⚡ LLM field generation
GET                  /api/pipelines/agent-types                — registry listing
POST                 /api/pipelines/stages/{id}/trigger-factory — manual factory trigger
GET                  /api/tasks/{id}/archived-files            — workspace deletion audit
POST                 /api/tasks/{id}/undelete                  — restore archived file

# Agent & scheduler
POST   /api/agent/run/{task_id}           — start MaestroLoop
GET    /api/agent/status/{task_id}        — loop status
POST   /api/agent/stop/{task_id}          — graceful stop
GET    /api/agent/tasks/ready             — DAG-ready tasks
GET    /api/scheduler/status              — scheduler state

# Infrastructure
CRUD   /api/llms[/{id}]                   — LLM endpoints
CRUD   /api/budgets[/{id}]                — budgets
CRUD   /api/compute-nodes[/{id}]          — compute nodes
GET    /api/budget-entries                — budget entries (task_id=__file_summaries__ for prewarm)
GET    /api/budget-entries/{id}/full      — full reconstructed prompt/response (accumulates deltas)
GET    /api/sessions/{session_id}/entries/full — all entries for a session with raw prompt_delta per entry
GET    /api/budgets/{id}/summary          — aggregated usage

# Diagnostics / task detail
GET    /api/tasks/{id}/children / subdivision-records / pips / stage-summary
GET    /api/tasks/{id}/planning-result / component-status / optimization-status
GET    /api/tasks/{id}/security-status / final-review-status / merge-status
GET    /api/tasks/{id}/diff / research-jobs / transition-status / documents
GET    /api/diagnostics/tasks
GET    /api/projects/{name}/arch-gen-jobs
```

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **Named shell tools replace grouped `run_shell_*` tools.** Each tool does exactly one operation with no hidden allowlist for agents to guess. Key tools: `run_pytest`, `run_mypy`, `run_ruff`, `run_black_check`, `run_unittest`, `run_npm_test`, `run_cargo_test`, `run_go_test` (testing); `run_make`, `run_cargo_build`, `run_go_build`, `run_npm_build`, `run_tsc` (build); `run_pip_install`, `run_npm_install`, `run_cargo_fetch` (deps); `run_bandit`, `run_pip_audit`, `run_semgrep`, `run_npm_audit` (security); `git_restore`, `git_add`, `git_unstage` (git helpers). Per-stage access controlled by `build_tool_schemas(allowed_names)` in `config.py`.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
