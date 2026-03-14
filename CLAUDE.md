# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Still young and actively being built.

## Running the server

```bash
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

Board is at `http://localhost:8000/`. The LLM agent expects `llama.cpp` running OmniCoder 9B on `http://localhost:8008/v1` (OpenAI-compatible).

## Database migrations

```bash
migrate.bat status      # see applied vs pending
migrate.bat migrate     # apply pending migrations
migrate.bat reset       # DESTRUCTIVE: drop everything, re-migrate, re-seed
```

Or directly: `venv\Scripts\python.exe app/migrations/runner.py <command>`

Migrations live in `app/migrations/versions/` as `NNNN_description.py`. Never edit an existing migration — always add a new one. Each exposes `up(conn)`, `down(conn)`, and `description`.

Current schema migrations in order:
1. `0001` — initial `tasks` table
2. `0002` — `prerequisites` column (JSON array of task IDs)
3. `0003` — `project` column (string, default `'TheMaestro'`)

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists).
- `database.py` — SQLAlchemy `Task` model + all DB functions. Also contains `seed_sample_tasks_raw()` (raw sqlite3, used by the migration runner's reset command).
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.
- `agent/` — the Maestro LLM loop (not yet wired into the board UI):
  - `config.py` — constants (endpoint, limits, archive path, branch prefix)
  - `tools.py` — 16 tools with OpenAI JSON schemas + `dispatch_tool()`. Safe-delete via `archive_file()` (moves to `.archive/`, never hard-deletes). `run_shell()` has a blocklist of destructive patterns.
  - `system_prompt.py` — `MAESTRO_SYSTEM_PROMPT`
  - `loop.py` — `MaestroLoop` class. `_ACTIVE_LOOPS` / `_LOOP_STATUS` dicts power the status/stop API endpoints.
  - `dag.py` — `DAGResolver`: Kahn's topological sort, ready-task finder, cycle detection.

### Frontend (`app/web/`)
- `index.html` — the board shell; project tabs, five columns (ARCHITECTURE, PLANNING, DEVELOPMENT, REVIEW, COMPLETED), modals for task create/edit and new project.
- `kanban.js` — all behaviour. Key globals: `taskData` (object keyed by task ID), `allTasks` (array), `currentProject` (string). On load fetches `/api/projects/{project}/tasks`, renders, starts 5-second auto-refresh polling the same endpoint.
- `style.css` — all styles.

### Data flow
Tasks are per-project. Switching projects calls `loadTasksFromDatabase()` which re-fetches `/api/projects/{name}/tasks` and fully rebuilds `taskData`. `renderTasksFromDatabase()` groups tasks by type, sorts each group by `position`, and appends cards to their column containers. Drag-and-drop reorder POSTs to `/api/tasks/{id}/reorder`, then re-fetches the full project task list to get authoritative positions before re-rendering.

### Key API routes
```
GET  /api/projects/{project_name}/tasks   — all tasks for a project
POST /api/tasks                           — create task (include project field)
PUT  /api/tasks/{id}                      — update task
POST /api/tasks/{id}/reorder              — {position, type} — reorder within column
POST /api/agent/run/{task_id}             — start MaestroLoop (background)
GET  /api/agent/status/{task_id}          — loop status
GET  /api/agent/tasks/ready               — DAG-ready tasks
```

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **`run_shell()` has a blocklist** — `rm -rf`, `del /s`, fork bombs, deep `../` traversal, etc. are all blocked at the tool level.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
