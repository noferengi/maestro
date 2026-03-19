# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session start checklist

Read `SUMMARY.md` in the project root. It contains recent work and prioritized next steps written by the previous session. After reading it, ask yourself: **what should be done next?** If the user hasn't given a specific instruction, surface the top item from the next-steps list and confirm before proceeding.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition IDEA → PLANNING → DEVELOPMENT → REVIEW → COMPLETED, gated by a multi-stage intake pipeline with LLM voting.

## Running the server

```bash
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Board is at `http://localhost:8000/`. LLM endpoints are configurable per-task via the UI (managed in the `llms` table). Default expects `llama.cpp` on `http://localhost:8008/v1` (OpenAI-compatible).

## Running tests

```bash
venv\Scripts\python.exe -m pytest app/tests/ -v
venv\Scripts\python.exe -m pytest app/tests/test_repl.py -v      # single file
venv\Scripts\python.exe -m pytest app/tests/test_repl.py -k "test_name" -v  # single test
```

## Database migrations

```bash
migrate.bat status      # see applied vs pending
migrate.bat migrate     # apply pending migrations
migrate.bat reset       # DESTRUCTIVE: drop everything, re-migrate, re-seed
```

Or directly: `venv\Scripts\python.exe app/migrations/runner.py <command>`

Migrations live in `app/migrations/versions/` as `NNNN_description.py`. Never edit an existing migration — always add a new one. Each exposes `up(conn)`, `down(conn)`, and `description`.

Current schema migrations (1–10):
1. `0001` — initial `tasks` table
2. `0002` — `prerequisites` column (JSON array of task IDs)
3. `0003` — `project` column (string, default `'TheMaestro'`)
4. `0004` — `llm_id` and `budget_id` columns on tasks
5. `0005` — `llms` and `budgets` tables with foreign keys
6. `0006` — `transition_votes` and `transition_results` tables
7. `0007` — `parallel_sessions`, `max_context` columns on `llms`
8. `0008` — `notes` column on `llms`
9. `0009` — `budget_entries` table for per-call LLM usage tracking
10. `0010` — `parent_task_id`, `subdivision_generation` on tasks; `subdivision_records` table

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists).
- `database.py` — SQLAlchemy models (`Task`, `LLM`, `Budget`, `TransitionVote`, `TransitionResult`, `BudgetEntry`, `SubdivisionRecord`) + all DB CRUD functions. Also contains `seed_sample_tasks_raw()` (raw sqlite3, used by migration runner's reset command).
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.

### Agent system (`app/agent/`)
- `loop.py` — `MaestroLoop` class. `_ACTIVE_LOOPS` / `_LOOP_STATUS` dicts power the status/stop API endpoints. Drives Design → Implement → Test → Verify cycles.
- `intake.py` — Intake pipeline orchestrator for IDEA→PLANNING transitions. 4-stage voting: scope analysis, static analysis, feasibility, conflict detection. Calls `run_intake_pipeline()`.
- `research.py` — Research agent with a "lives" system (max 3 per session) for investigating unknowns when votes return tie/needs_research.
- `subdivide.py` — Subdivision agent for decomposing oversized ideas into smaller sub-ideas. Triggered by SUBDIVIDE_IDEA verdict. Read-only tools, structured JSON output with sub-idea specs.
- `scheduler.py` — Push-first eager task scheduler. Dispatches DAG-ready tasks respecting per-endpoint capacity limits. Runs as a background thread with configurable tick interval.
- `llm_client.py` — Centralized HTTP client for all LLM calls (intake, research, MaestroLoop). Handles budget tracking and logs full prompt/response payloads to `budget_entries`.
- `verdicts.py` — Verdict classification with confidence ranges. `Vote` and `TallyResult` dataclasses. `tally_votes()` aggregation logic. Includes `SUBDIVIDE_IDEA` verdict (Rule 0 — highest priority).
- `static_analysis.py` — Tree-sitter based deterministic Python code analysis for intake stage 2a. Extracts classes, functions, imports, line ranges.
- `tools.py` — Agent tools with OpenAI JSON schemas + `dispatch_tool()`. Categories: file I/O (read/write/append/list/count), search (search_files, find_files), git (status/diff/log/blame/show/checkout/branch/commit/push), execution (run_shell with blocklist), deletion (archive_file — soft-delete only), task queries (get/list/update/append_history).
- `dag.py` — `DAGResolver`: Kahn's topological sort, ready-task finder, cycle detection.
- `config.py` — constants (endpoint, limits, archive path, branch prefix).
- `system_prompt.py` — `MAESTRO_SYSTEM_PROMPT`.
- `mock_llm.py` — Dictionary-based mock LLM for testing. OpenAI-compatible response format with scenario presets.

### Models & Services
- `app/models/dags.py` — `TaskDAG` and `TaskNode` classes with state transitions (PENDING→ACTIVE→VERIFYING→ACCEPTED). Ready-task resolution, JSON serialization.
- `app/services/repl.py` — `CheckpointManager` for git-based task persistence (add/commit/checkout).

### Frontend (`app/web/`)
- `index.html` — board shell; project tabs, five columns (ARCHITECTURE, PLANNING, DEVELOPMENT, REVIEW, COMPLETED), modals for task create/edit, new project, LLM Endpoints and Budgets management.
- `kanban.js` — all behaviour. Key globals: `taskData`, `allTasks`, `currentProject`, `allLlms`, `allBudgets`, `transitionCache`, `transitionPollers`. Handles transition status polling, LLM/Budget dropdowns on tasks, drag-and-drop reorder. 5-second auto-refresh.
- `style.css` — all styles.

### Configuration (`maestro.ini`)
INI file with sections: `[intake]` (research lives, tiebreaker, LLM temperature, allowed research tools), `[subdivision]` (max_depth, max_retries_per_level, max_total_sub_ideas, llm_temperature, subdivision_agent_tools), `[capacity]` (parallel session limits, context window constraints), `[context_warnings]` (three-tier saturation thresholds at 50%/75%/90%), `[scheduler]` (tick interval, enabled flag), `[verdicts]` (confidence range mappings).

### Data flow
Tasks are per-project. Switching projects calls `loadTasksFromDatabase()` which re-fetches `/api/projects/{name}/tasks` and fully rebuilds `taskData`. `renderTasksFromDatabase()` groups tasks by type, sorts each group by `position`, and appends cards to their column containers. Drag-and-drop reorder POSTs to `/api/tasks/{id}/reorder`, then re-fetches the full project task list to get authoritative positions before re-rendering.

### Key API routes
```
GET  /api/projects/{project_name}/tasks   — all tasks for a project
POST /api/tasks                           — create task (include project field)
PUT  /api/tasks/{id}                      — update task
POST /api/tasks/{id}/reorder              — {position, type} — reorder within column
POST /api/tasks/{task_id}/advance         — trigger intake pipeline (IDEA→PLANNING)
GET  /api/tasks/{task_id}/transition-status — latest transition result + vote history
POST /api/agent/run/{task_id}             — start MaestroLoop (background)
GET  /api/agent/status/{task_id}          — loop status
POST /api/agent/stop/{task_id}            — request graceful stop
GET  /api/agent/tasks/ready               — DAG-ready tasks
GET  /api/scheduler/status                — scheduler state
CRUD /api/llms, /api/llms/{id}            — LLM endpoint management
CRUD /api/budgets, /api/budgets/{id}      — budget management
GET  /api/budget-entries                  — budget entry listing
GET  /api/budget-entries/{id}/full        — single entry with full prompt/response
GET  /api/budgets/{id}/summary            — aggregated budget usage
GET  /api/tasks/{id}/children            — direct child tasks of a subdivided task
GET  /api/tasks/{id}/subdivision-records — audit trail of subdivision attempts
```

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **`run_shell()` has a blocklist** — `rm -rf`, `del /s`, fork bombs, deep `../` traversal, etc. are all blocked at the tool level.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
