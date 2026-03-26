# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Session start checklist

Read `SUMMARY.md` in the project root. It contains recent work and prioritized next steps written by the previous session. After reading it, ask yourself: **what should be done next?** If the user hasn't given a specific instruction, surface the top item from the next-steps list and confirm before proceeding.

## After major accomplishments

Run `/update-full-plan` automatically after completing any significant body of work (feature complete, bug fixed, tests passing), or whenever the user asks. There is an older skill that only updates one file — always use `/update-full-plan`.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED, gated by a multi-stage intake pipeline with LLM voting.

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

Current schema migrations (0001–0026):
- `0001` — initial `tasks` table
- `0002` — `prerequisites` column (JSON array of task IDs)
- `0003` — `project` column (string, default `'TheMaestro'`)
- `0004` — `llm_id` and `budget_id` columns on tasks
- `0005` — `llms` and `budgets` tables with foreign keys
- `0006` — `transition_votes` and `transition_results` tables
- `0007` — `parallel_sessions`, `max_context` columns on `llms`
- `0008` — `notes` column on `llms`
- `0009` — `budget_entries` table for per-call LLM usage tracking
- `0010` — `parent_task_id`, `subdivision_generation` on tasks; `subdivision_records` table
- `0011–0021` — big-idea flag, interface contracts, planning/review stages, demotion, file summaries, expenses
- `0022` — `file_summary_jobs` table (scheduler-dispatched summaries, priority -1.0)
- `0023` — `previous_summary` column on `file_summary_jobs`
- `0024` — `map_x`, `map_y` on tasks (Column Map View positions)
- `0025` — `llm_id` on `projects` (default LLM for project-level maintenance jobs)
- `0026` — `budget_id` on `projects` (default budget for project-level maintenance jobs)

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists). Contains `_project_to_dict()` helper and `_pick_prewarm_resources()` / `_trigger_project_prewarm()` helpers that use the project's own `llm_id` and `budget_id` when set.
- `database.py` — SQLAlchemy models (`Task`, `LLM`, `Budget`, `Project`, `TransitionVote`, `TransitionResult`, `BudgetEntry`, `Expense`, `SubdivisionRecord`, `FileSummary`, `FileSummaryJob`) + all DB CRUD functions. `batch_update_map_positions(updates)` bulk-updates `map_x`/`map_y` without touching task history. `upsert_project()` uses `...` (Ellipsis) sentinel for `llm_id` and `budget_id` — pass Ellipsis to leave unchanged, pass None to clear.
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.

### Agent system (`app/agent/`)
- `loop.py` — `MaestroLoop` class. `_ACTIVE_LOOPS` / `_LOOP_STATUS` dicts power the status/stop API endpoints. Drives Design → Implement → Test → Verify cycles. Uses task's project path for snapshot injection — skips snapshot silently if no project path is set.
- `intake.py` — Intake pipeline orchestrator for IDEA→PLANNING transitions. 4-stage voting: scope analysis, static analysis, feasibility, conflict detection. Passes `project_root` from task's project to `ResearchAgent` and `SubdivisionAgent`.
- `research.py` — Research agent with a "lives" system (max 3 per session). Accepts `project_root` param; injects project snapshot into initial context when set.
- `subdivide.py` — Subdivision agent for decomposing oversized ideas. Accepts `project_root` param; injects project snapshot when set. Triggered by SUBDIVIDE_IDEA verdict.
- `scheduler.py` — Push-first eager task scheduler. Dispatches DAG-ready tasks respecting per-endpoint capacity limits. Passes `project_root` to research jobs. `_dispatch_file_summary_jobs()` runs FIRST in `_tick()`. Completion registry: `get_or_create_completion_event()`, `signal_completion()`, `wait_for_completion()`.
- `llm_client.py` — Centralized HTTP client for all LLM calls. Requires both `llm_id` and `budget_id`. Logs every call to `budget_entries` + `expenses`.
- `verdicts.py` — Verdict classification with confidence ranges. `Vote` and `TallyResult` dataclasses. `tally_votes()` aggregation logic.
- `static_analysis.py` — Tree-sitter based deterministic Python code analysis for intake stage 2a.
- `tools.py` — Agent tools with OpenAI JSON schemas + `dispatch_tool()`. **Relative paths are resolved against `effective_root` (the project path), not the process CWD** — critical for agents operating on non-Maestro projects. Categories: file I/O (read/write/append/list/count), search, git, execution (run_shell with blocklist), deletion (archive_file — soft-delete only), task queries.
- `project_snapshot.py` — `build_project_snapshot(project_root)` and `build_snapshot_with_summaries(project_root)` now **require an explicit `project_root`** — there is no default fallback to TheMaestro's own directory. `async_build_file_summary()` uses enqueue+wait pattern. Session cache uses `("llm", path, mtime, size)` prefix to avoid collision with structural entries.
- `file_summary_agent.py` — `enqueue_file_summary()` + `execute_file_summary()`. Called by scheduler worker thread.
- `dag.py` — `DAGResolver`: Kahn's topological sort, ready-task finder, cycle detection.
- `config.py` — constants (endpoint, limits, archive path, branch prefix).
- `system_prompt.py` — `MAESTRO_SYSTEM_PROMPT`.
- `mock_llm.py` — Dictionary-based mock LLM for testing.

### Project isolation

Each project record has: `name` (PK), `path` (absolute filesystem root), `description`, `llm_id` (default LLM for maintenance), `budget_id` (default budget for maintenance).

- **Agent isolation** — `IntakePipeline`, `ResearchAgent`, `SubdivisionAgent`, and `MaestroLoop` all receive `project_root` derived from `get_project_path(task.project)`. Snapshot injection is scoped to the task's project, never Maestro's own source tree.
- **Tool isolation** — `_assert_safe_path()` in `tools.py` resolves relative paths against `effective_root` so `read_file("src/main.py")` opens the correct file in the task's project, not in `D:/workspace/TheMaestro/`.
- **LLM/budget inheritance** — When creating a new task, `openAddTaskModal()` pre-selects the current project's `llm_id` as the default LLM. Prewarm file-summary jobs use the project's `budget_id` when set; falls back to first infinite budget otherwise.
- **`allProjects`** global in `kanban.js` — `[{name, path, description, llm_id, budget_id}]`, kept in sync by `loadProjects()`.

### Frontend (`app/web/`)

#### Board (`index.html` + `kanban.js` + `style.css`)
- `index.html` — board shell; project tabs, nine columns (ARCHITECTURE, IDEAS, PLANNING, INDEV, CONCEPTUAL_REVIEW, OPTIMIZATION, SECURITY, FULL_REVIEW, COMPLETED), the Column Map overlay (`#column-map-container`), eight modals (task create/edit, new project, edit project, transition, LLM endpoints, budgets, tools). New/Edit Project modals both have **Default LLM** and **Budget** dropdowns.
- `kanban.js` — all board behaviour. Key globals:
  - `taskData`, `allTasks`, `currentProject` — task state
  - `allLlms`, `allBudgets`, `allProjects` — endpoint/budget/project caches
  - `transitionCache`, `transitionPollers` — intake pipeline polling
  - `columnMapActive`, `columnMapType` — Column Map View state flag
  - `_mapCurrentEdges`, `_mapCurrentNodePositions`, `_mapCurrentColor`, `_mapOffsetX/Y` — shared map render state
  - `_mapNodeDrag` — drag state object (active, nodeId, startX/Y, etc.)
  - `_viewChildrenState`, `_childrenPollerTimer` — subdivision view/regen state
  - `currentBigIdeaFilter`, `breadcrumbStack`, `descendantIndex` — Big Idea zoom state
  - `_modalMousedownTarget` — drag-close fix (global mousedown listener, all modals)

#### Column Map View
Clicking any column header or empty whitespace in a column opens a full-screen **Column Map View** — a 2D radial canvas showing tasks as cards with thick bezier arrows between connected nodes. Click the header again or "← Back to Board" to return.

- `openColumnMap(colType)` / `closeColumnMap()` — toggle. Hides `.kanban-board`, shows `#column-map-container`.
- `handleColumnClick(e, colType)` / `handleTasksContainerClick(e, colType)` — click guards.
- `_mapComputeLayout(tasks, colType)` — three-phase layout engine: (1) load saved `map_x/map_y`; (2) BFS fan-out for newly-subdivided children; (3) standard radial `placeSubtree()` for unpositioned nodes. IDEAS/ARCHITECTURE use `parent_task_id` hierarchy; all others use `prerequisites`.
- `renderColumnMap(colType)` — computes bounding box, sets canvas size, populates shared state, calls `_mapRedrawArrows()`, renders `.map-node` divs, saves newly-positioned nodes.
- `_mapRedrawArrows()` — removes/redraws all SVG `<path>` cubic bezier arrows. Arrows run edge-to-edge (not center-to-center) via `_mapCardEdge()`.
- `_mapStartNodeDrag(e, nodeId)` — group drag: dragging a parent moves it + all descendants by the same delta simultaneously.
- `_mapSavePositions(toSave)` — async fire-and-forget: `PATCH /api/tasks/map-positions`.
- `setupMapInteraction()` / `teardownMapInteraction()` — mousedown/mousemove/mouseup/wheel on `#column-map-scroll-wrap`. Pan on empty canvas drag, zoom on scroll.
- **Positions** are in layout-space (centered around 0), not canvas-space. Canvas position = layout + `(_mapOffsetX, _mapOffsetY)`. Offset recomputed from bounding box each render — saved positions are stable across sessions.
- `reconcile()` skips DOM reconciliation when `columnMapActive`; keeps `taskData` fresh.

#### View Children (Subdivision Sets)
Clicking "View Children" on a Big Idea task opens the transition modal showing all subdivision sets as a paginated collection (← older · N of M · newer →).

- **Active set** — the set currently feeding child tasks to the board. Non-active sets show an **"Activate this set"** button in the footer to switch.
- **Regeneration** — clicking "Regenerate" keeps the modal open, injects a synthetic `{status: 'generating'}` placeholder as set 1, and starts `_startChildrenPoller(taskId)`. The poller (500ms interval) calls `GET /api/tasks/{id}/subdivision-records` until the newest record transitions out of `generating` status, then stops and re-renders. The footer shows `generating…` in orange while in progress.
- `_viewChildrenState = { taskId, records, childMap, idx }` — records are sorted newest-first (index 0 = newest).
- `_childrenPollerTimer` — `setInterval` ID; cleared by `_stopChildrenPoller()`.

#### Diagnostics (`diagnostics.html` + `diag-*.js` + `diagnostics.css`)
A standalone three-panel LLM conversation viewer at `/diagnostics`.

- `diag-utils.js` — shared state globals + pure helpers. `labelEntry(systemContent)` classifies by system message. **`labelEntryFromUser(userContent)`** — fallback classifier for system-less calls (e.g. file summaries); detects `file_summary` type from user message content.
- `diag-tasks.js` — left panel; fetches `/api/diagnostics/tasks`.
- `diag-entries.js` — middle panel. Handles synthetic `__file_summaries__` task ID: fetches `GET /api/budget-entries?task_id=__file_summaries__` which returns entries where `task_id IS NULL`.
- `diag-session.js` — turn summary table, entry selection (3 fetch paths), `jumpToEntry()`.
- `diag-render.js` — right panel; `renderConversation()`, `buildCtxBar()`, `_initDockZoom()` IIFE. Falls back to `labelEntryFromUser()` when no system message found. `TYPE_COLORS` includes `file_summary: '#17a2b8'` (teal).
- `diagnostics.css` — `.type-file_summary` added alongside other entry type colours.

**File Summaries in diagnostics** — `GET /api/diagnostics/tasks` returns a synthetic `{id: "__file_summaries__", type: "file_summary"}` row at the top when any `budget_entries` have `task_id IS NULL` (project prewarm calls that aren't tied to a specific task card).

### Configuration (`maestro.ini`)
INI file with sections: `[intake]`, `[subdivision]`, `[capacity]`, `[context_warnings]`, `[scheduler]`, `[verdicts]`.

### Data flow
Tasks are per-project. Switching projects calls `loadTasksFromDatabase()` which re-fetches `/api/projects/{name}/tasks` and fully rebuilds `taskData`. `renderTasksFromDatabase()` groups tasks by type, sorts each group by `position`, and appends cards to their column containers. Drag-and-drop reorder POSTs to `/api/tasks/{id}/reorder`, then re-fetches the full project task list to get authoritative positions before re-rendering. When `columnMapActive` is true, `reconcile()` only refreshes `taskData` and skips DOM reconciliation.

### Key API routes
```
GET    /api/projects                      — list projects (name, path, description, llm_id, budget_id)
POST   /api/projects                      — create project
PUT    /api/projects/{name}               — update project (llm_id/budget_id use Ellipsis sentinel)
DELETE /api/projects/{name}               — delete project record
GET    /api/projects/{project_name}/tasks — all tasks for a project
POST   /api/tasks                         — create task (include project field)
PUT    /api/tasks/{id}                    — update task
POST   /api/tasks/{id}/reorder            — {position, type} — reorder within column
PATCH  /api/tasks/map-positions           — [{id, map_x, map_y}] — bulk-save 2D positions (no history)
POST   /api/tasks/{task_id}/advance       — trigger intake pipeline (IDEA→PLANNING)
GET    /api/tasks/{task_id}/transition-status — latest transition result + vote history
POST   /api/agent/run/{task_id}           — start MaestroLoop (background)
GET    /api/agent/status/{task_id}        — loop status
POST   /api/agent/stop/{task_id}          — request graceful stop
GET    /api/agent/tasks/ready             — DAG-ready tasks
GET    /api/scheduler/status              — scheduler state
CRUD   /api/llms, /api/llms/{id}          — LLM endpoint management
CRUD   /api/budgets, /api/budgets/{id}    — budget management
GET    /api/budget-entries                — budget entry listing; task_id=__file_summaries__ returns null-task entries
GET    /api/budget-entries/{id}/full      — single entry with full prompt/response
GET    /api/budgets/{id}/summary          — aggregated budget usage
GET    /api/tasks/{id}/children           — direct child tasks of a subdivided task
GET    /api/tasks/{id}/subdivision-records — audit trail of subdivision attempts
GET    /api/diagnostics/tasks             — tasks with LLM activity + synthetic __file_summaries__ row
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
