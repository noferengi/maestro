# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session start checklist

Read `SUMMARY.md` in the project root. It contains recent work and prioritized next steps written by the previous session. After reading it, ask yourself: **what should be done next?** If the user hasn't given a specific instruction, surface the top item from the next-steps list and confirm before proceeding.

## After major accomplishments

Run `/update-full-plan` automatically after completing any significant body of work (feature complete, bug fixed, tests passing), or whenever the user asks. There is an older skill that only updates one file — always use `/update-full-plan`.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition IDEA → PLANNING → DEVELOPMENT → REVIEW → COMPLETED, gated by a multi-stage intake pipeline with LLM voting.

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
- `diagnostics.html` — standalone three-panel LLM diagnostics page. Loads `diag-*.js` in order.
- `diag-utils.js` — shared globals + pure helpers (`escapeHtml`, `fmtTokens`, `labelEntry`).
- `diag-tasks.js` — left panel: task list, search filter.
- `diag-entries.js` — middle panel: entry timeline, session detection, task summary.
- `diag-session.js` — turn summary table (`buildSessionSummary()`), entry selection (3 fetch paths), `jumpToEntry()`.
- `diag-render.js` — right panel: `renderConversation()`, `buildCtxBar()` (context-window usage bar with per-segment hover labels), macOS Dock-style magnification (`_initDockZoom()` IIFE — cosine falloff, 5× peak, 24px radius), message rendering, toggle handlers, `DOMContentLoaded` init.
- `diagnostics.css` — all diagnostics styles including context bar segments (`.ctx-seg`), Dock zoom (`.dock-zooming`), entry type colours, warning banners.

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

## Working with this user

### Always challenge the prompt
The user self-describes as weak at prompting. Before executing any non-trivial request, ask:
- Is the idea completely formed? Are there unstated assumptions?
- Have edge and corner cases been identified?
- Is there a Devil's Advocate approach — a different angle that might be more effective?
- Is there a simpler or more direct solution being overlooked?

Push back when the framing seems incomplete. A better-formed problem produces a better solution.

### Python explanations — frame for a C++ background
The user is a strong C++ engineer learning Python. When explaining Python concepts, use C++ analogues:

- **`async`/`await`** — Python's cooperative multitasking. Unlike C++ threads (which are OS-scheduled preemptively and share memory across actual CPU cores), Python's `asyncio` runs on a **single OS thread** with a single GIL-held interpreter. `await` is a voluntary yield point — the coroutine suspends itself and returns control to the event loop, which can run another coroutine. Think of it like a cooperative fiber/coroutine scheduler (similar to Boost.Coroutine or C++20 coroutines), not pthreads. No true parallelism for CPU-bound work; it shines for I/O-bound work (HTTP calls, disk) where the bottleneck is waiting, not computing.

- **The GIL** — the Global Interpreter Lock. Only one thread executes Python bytecode at a time, even on a multi-core machine. True CPU parallelism in Python requires `multiprocessing` (separate processes, separate memory spaces — like `fork()`). `asyncio` sidesteps the GIL issue because it's single-threaded by design.

- **Memory model** — Python objects live on the heap, reference-counted (like `shared_ptr` everywhere). There are no stack-allocated value types, no cache-line-aware struct layout, no RAII in the C++ sense. The CPython allocator has its own arena/pool system but you don't control it. Variables are always references (pointers), never values. Assignment copies the pointer, not the object — same as `shared_ptr<T> b = a`.

- **Cache behaviour** — Python makes no guarantees about cache-line layout. Objects are heap-allocated individually with header overhead; a Python list of ints is a list of pointers to boxed int objects, not a contiguous int array. For cache-friendly numeric work, use `numpy` (which wraps contiguous C arrays). Don't reason about L1/L3 locality from Python-level code — the abstraction is too high.

- **`asyncio` event loop** — conceptually the same as an `epoll`/`io_uring` + callback loop in C++. One thread, one loop, coroutines registered as tasks. `await asyncio.gather(a, b, c)` runs three coroutines concurrently on that one thread — they interleave at `await` points, not truly in parallel.

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **`run_shell()` has a blocklist** — `rm -rf`, `del /s`, fork bombs, deep `../` traversal, etc. are all blocked at the tool level.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
