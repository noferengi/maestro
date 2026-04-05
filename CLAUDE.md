# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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

Current schema migrations (0001–0037, showing last 10):
- `0001–0027` — (earlier migrations; see migration files for history)
- `0028` — fix subdivision positions (data repair)
- `0029` — repair phantom `-subN` prerequisite IDs left by old subdivision code (data repair)
- `0030` — `inbox_messages` table
- `0031` — `is_active BOOLEAN DEFAULT 1` on tasks (soft-delete support)
- `0032` — `compute_nodes` table; `compute_node_id` FK on `llms`
- `0033–0034` — (reserved / applied)
- `0035` — `short_summary` column on `file_summaries`
- `0036` — `arch_gen_jobs` table; `project`, `category`, `llm_id`, `budget_id`, `status`, `priority` (1.0); index on `(status, priority, created_at)`
- `0037` — `retry_count INTEGER DEFAULT 0` on `arch_gen_jobs` (cap retries at 3; inbox notification on abandon)

**Full schema reference:** See `CLAUDE_SCHEMA.md` in the project root. Read that file whenever you need to query or modify `data/kanban.db` directly — it contains every table, column, type, nullability, and default value.

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

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists). Contains `_project_to_dict()` helper and `_pick_prewarm_resources()` / `_trigger_project_prewarm()` helpers that use the project's own `llm_id` and `budget_id` when set. Quick-action endpoints: `/demote`, `/set-stage`, `/clone`, `/pin`, `/run-planning`, `/run-review`, `/run-security`, `/run-full-review`.
- `database.py` — SQLAlchemy models (`ComputeNode`, `Task`, `LLM`, `Budget`, `Project`, `TransitionVote`, `TransitionResult`, `BudgetEntry`, `Expense`, `SubdivisionRecord`, `FileSummary`, `FileSummaryJob`) + all DB CRUD functions. `batch_update_map_positions(updates)` bulk-updates `map_x`/`map_y` without touching task history. `upsert_project()` uses `...` (Ellipsis) sentinel for `llm_id` and `budget_id` — pass Ellipsis to leave unchanged, pass None to clear. `delete_task()` is a **soft-delete**: sets `is_active=False` on the target and all descendants via BFS; returns count deactivated. All read queries (`get_tasks_by_project`, `get_tasks_by_type`, `get_all_tasks`) filter `is_active=True`. `ComputeNode` CRUD: `get_all_compute_nodes`, `get_compute_node`, `create_compute_node`, `update_compute_node`, `delete_compute_node` (all in `crud_infra.py`).
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.

### Agent system (`app/agent/`)
- `loop.py` — `MaestroLoop` class. `_ACTIVE_LOOPS` / `_LOOP_STATUS` dicts power the status/stop API endpoints. Drives Design → Implement → Test → Verify cycles. `_build_messages()` injects both the file-structure snapshot and the full architecture context (all categories) derived from the task's project.
- `intake.py` — Intake pipeline orchestrator for IDEA→PLANNING transitions. 4-stage voting: scope analysis, static analysis, feasibility, conflict detection. Passes `project_root` from task's project to `ResearchAgent` and `SubdivisionAgent`.
- `research.py` — Research agent with a "lives" system (max 3 per session). `_build_life_context()` on life 1 injects the file-structure snapshot followed by the full architecture context (all categories), then the investigation question. `WebSearchAgent` class (private to async `web_search` dispatch) — 10-turn agent that fetches pages and synthesizes findings; only tool available to it is `web_fetch`.
- `subdivide.py` — Subdivision agent for decomposing oversized ideas. `_build_context()` injects snapshot then filtered architecture context (Platform/Design/Testing/Performance/API/Data/Tooling/General). Triggered by SUBDIVIDE_IDEA verdict.
- `scheduler.py` — Push-first eager task scheduler. Dispatches DAG-ready tasks respecting per-endpoint capacity limits **and** per-compute-node capacity limits. Passes `project_root` to research jobs. `_dispatch_file_summary_jobs()` runs FIRST in `_tick()`. Completion registry: `get_or_create_completion_event()`, `signal_completion()`, `wait_for_completion()`. `_task_to_mini_dict` includes `parent_task_id` so `DAGResolver` can build the child index. `SCHEDULER_DISPATCHABLE_TYPES` default includes all pipeline stages (`idea, planning, indev, conceptual_review, optimization, full_review`) — orphaned mid-pipeline tasks are re-dispatched on restart; the `_active_sessions` alive-check prevents double-dispatch of running tasks. At the start of each `_tick()`, `node_active_counts` is built by summing `_llm_session_counts` grouped by `compute_node_id`; the node cap is checked before the per-LLM cap and the local count is incremented within the tick to prevent over-dispatch.
- `llm_client.py` — Centralized HTTP client for all LLM calls. Requires both `llm_id` and `budget_id`. Logs every call to `budget_entries` + `expenses`.
- `verdicts.py` — Verdict classification with confidence ranges. `Vote` and `TallyResult` dataclasses. `tally_votes()` aggregation logic.
- `static_analysis.py` — Tree-sitter based deterministic Python code analysis for intake stage 2a.
- `tools.py` — Agent tools with OpenAI JSON schemas + `dispatch_tool()`. **Relative paths are resolved against `effective_root` (the project path), not the process CWD** — critical for agents operating on non-Maestro projects. Categories: file I/O (read/write/append/list/count), search (`web_search` dispatches to DuckDuckGo or Brave based on `SEARCH_PROVIDER` config; `web_fetch` for direct URL retrieval), git, execution (run_shell with blocklist), deletion (archive_file — soft-delete only), task queries.
- `project_snapshot.py` — `build_project_snapshot(project_root)` and `build_snapshot_with_summaries(project_root)` now **require an explicit `project_root`** — there is no default fallback to TheMaestro's own directory. `async_build_file_summary()` uses enqueue+wait pattern. Session cache uses `("llm", path, mtime, size)` prefix to avoid collision with structural entries. `build_architecture_context(project_name, agent_type=None)` fetches `type='architecture'` tasks for the project and formats them as a structured constraint block; `ARCH_CATEGORY_RELEVANCE` dict maps agent type → relevant category set (None = all) so each agent receives only the categories that matter to its work.
- `file_summary_agent.py` — `enqueue_file_summary()` + `execute_file_summary()`. Called by scheduler worker thread. Injects a filtered architecture context preamble (Platform/Tooling/Data/General only) into all three prompt paths when a `task_id` is available.
- `dag.py` — `DAGResolver`: Kahn's topological sort, ready-task finder, cycle detection. `_children_by_parent` index (built from `parent_task_id` fields) enables `_is_effectively_done()` — a Big Idea parent satisfies a prerequisite edge once all its active (non-cancelled) children are recursively done, without the parent itself reaching `completed`. Parents with children are skipped in `get_ready_tasks()` (not directly dispatchable). Mid-pipeline stages (`indev`, `conceptual_review`, `optimization`, `full_review`) are no longer excluded from `get_ready_tasks()` — they surface as ready when their thread dies, enabling restart recovery.
- `config.py` — constants (endpoint, limits, archive path, branch prefix).
- `system_prompt.py` — `MAESTRO_SYSTEM_PROMPT`.
- `mock_llm.py` — Dictionary-based mock LLM for testing.

### Project isolation

Each project record has: `name` (PK), `path` (absolute filesystem root), `description`, `llm_id` (default LLM for maintenance), `budget_id` (default budget for maintenance).

- **Agent isolation** — `IntakePipeline`, `ResearchAgent`, `SubdivisionAgent`, and `MaestroLoop` all receive `project_root` derived from `get_project_path(task.project)`. Snapshot injection is scoped to the task's project, never Maestro's own source tree.
- **Architecture context injection** — `build_architecture_context(project_name, agent_type)` is called in `loop.py` (`_build_messages`), `research.py` (`_build_life_context` life 1), `subdivide.py` (`_build_context`), and `file_summary_agent.py` (`execute_file_summary`). Each agent type receives only the card categories relevant to its work, as defined by `ARCH_CATEGORY_RELEVANCE` in `project_snapshot.py`. Categories with `None` (research, loop, full_review) receive all cards; categories with a set receive only matching cards.
- **Tool isolation** — `_assert_safe_path()` in `tools.py` resolves relative paths against `effective_root` so `read_file("src/main.py")` opens the correct file in the task's project, not in `D:/workspace/TheMaestro/`.
- **LLM/budget inheritance** — When creating a new task, `openAddTaskModal()` pre-selects the current project's `llm_id` as the default LLM. Prewarm file-summary jobs use the project's `budget_id` when set; falls back to first infinite budget otherwise.
- **`allProjects`** global in `kanban.js` — `[{name, path, description, llm_id, budget_id}]`, kept in sync by `loadProjects()`.

### Frontend (`app/web/`)

#### Board (`index.html` + `kanban.js` + `style.css`)
- `index.html` — board shell; project tabs, **`#arch-bar`** (horizontal architecture bar spanning full width above the board), eight pipeline columns (IDEAS, PLANNING, INDEV, CONCEPTUAL_REVIEW, OPTIMIZATION, SECURITY, FULL_REVIEW, COMPLETED), the Column Map overlay (`#column-map-container`), nine modals (task create/edit, new project, edit project, transition, LLM endpoints, budgets, tools, **compute nodes**). New/Edit Project modals both have **Default LLM** and **Budget** dropdowns. The **LLM Endpoints** modal Add/Edit panes each have a **Compute Node** dropdown.
- `kanban.js` — all board behaviour. Key globals:
  - `taskData`, `allTasks`, `currentProject` — task state
  - `allLlms`, `allBudgets`, `allComputeNodes`, `allProjects` — endpoint/budget/compute node/project caches
  - `ARCH_CATEGORY_COLORS` — category name → hex colour for arch card badges (14 entries)
  - `_archBarCollapsed` — boolean, persisted in `localStorage`; drives `#arch-bar.collapsed` CSS class
  - `transitionCache`, `transitionPollers` — intake pipeline polling
  - `columnMapActive`, `columnMapType` — Column Map View state flag
  - `_mapCurrentEdges`, `_mapCurrentNodePositions`, `_mapCurrentColor`, `_mapOffsetX/Y` — shared map render state
  - `_mapNodeDrag` — drag state object (active, nodeId, startX/Y, etc.)
  - `_viewChildrenState`, `_childrenPollerTimer` — subdivision view/regen state
  - `currentBigIdeaFilter`, `breadcrumbStack`, `descendantIndex` — Big Idea zoom state
  - `_modalMousedownTarget` — drag-close fix (global mousedown listener, all modals)
  - `_stagePickerTaskId` — currently open stage-picker flyout task ID (null = closed)

#### Architecture Bar (`#arch-bar`)
A dark navy horizontal band rendered **above** the kanban pipeline columns (not inside them). Architecture tasks (`type='architecture'`) live here exclusively — they are not rendered in any pipeline column.

- `renderArchBar()` — rebuilds all `.arch-card` elements from `taskData`; sorts by priority (`critical→high→normal→low`) then `position`. Called by `renderTasksFromDatabase()` and after any arch card create/edit/delete. Also called by `reconcile()` when any arch task fingerprint changes.
- `toggleArchBar()` — flips `_archBarCollapsed`, saves to `localStorage`, toggles `#arch-bar.collapsed` class.
- **Arch card schema** (`content` JSON): `category` (one of 14 fixed values: Platform/Design/Testing/Security/Performance/API/Tooling/Data/UX/Accessibility/Compliance/Deployment/Observability/General) and `priority` (critical/high/normal/low). The card body is the task's `description` field. LLM, budget, owner, tags are not used.
- **Modal integration** — `openAddTaskModal('architecture')` and `editArchitectureTask(taskId)` both use the shared task modal but call `showArchContentFields('architecture')` which shows the `#arch-category` / `#arch-priority` selects, hides LLM/budget/owner/tags fields, and relabels the description field as "Body (the constraint or fact)".
- **`reconcile()` handling** — arch tasks are explicitly skipped in the card-cache loop (no `.task-card` DOM element created); fingerprint changes set `archChanged = true` which triggers `renderArchBar()` at the end.
- **`deleteTask()` handling** — detects `task.type === 'architecture'` and calls `renderArchBar()` instead of searching for a `.task-card` DOM node.

**Card toolbar** — hover-revealed on every card (and map node), `flex-wrap` layout in three groups separated by `.toolbar-sep` dividers:
- **Agents**: 🔍 Research · ✂ Subdivide · 📋 Planning pipeline · 👁 Conceptual Review · 🔒 Security pipeline · ⌨ Manual Session
- **Control**: ▶ Run Agent · ⏹ Stop · ↩ Demote · ⚙ Stage picker
- **Actions**: 📊 Diagnostics · ⧉ Clone · 📌 Pin · 🔗 Map

`toolbarStagePicker(taskId, btn)` — opens a positioned flyout (`.stage-picker-flyout`) listing all 9 pipeline stages; current stage highlighted. Closes on outside click. `toolbarOpenMap(taskId)` — calls `openColumnMap(task.type, taskId)` which pans the map to center on the node and pulses a gold ring (`map-node-focus` animation). `toolbarOpenDiagnostics(taskId)` — opens `/diagnostics?task=<id>` in a new tab.

#### Column Map View
Clicking any column header or empty whitespace in a column opens a full-screen **Column Map View** — a 2D radial canvas showing tasks as cards with thick bezier arrows between connected nodes. Click the header again or "← Back to Board" to return.

- `openColumnMap(colType, focusNodeId?)` / `closeColumnMap()` — toggle. Hides `.kanban-board`, shows `#column-map-container`. Optional `focusNodeId` pans to center on that node and plays a 3× gold-pulse animation (`_mapFocusNode`).
- `_mapFocusNode(nodeId)` — reads node's layout position from `_mapCurrentNodePositions`, sets `mapTransform` to center on it, redraws arrows, adds `.map-node-focus` CSS class (keyframe animation, auto-removed after 2s).
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

- `diag-utils.js` — shared state globals and pure helpers. Shared constants: `TYPE_COLORS` (agent type → hex), `TOOL_COLORS` (tool category → hex), `TOOL_CATEGORY_MAP` (tool name → category). Functions: `escapeHtml`, `fmtTokens`, `formatTimestamp`, `labelEntry(systemContent)` (classifies by system prompt; covers surveyor/designer/judge/reviewer/research/pitfall/security/optimization/subdivision/web_agent/maestro_loop), `labelEntryFromUser(userContent)` (fallback for system-less calls; detects `file_summary`), `labelTool(toolName)` (maps tool name to category), `getConceptualTurns(group)` (builds SYSTEM Prompt + USER Prompt + Turn N structure for a session group).
- `diag-tasks.js` — left panel; fetches `/api/diagnostics/tasks`. After render, checks `?task=<id>` URL query param and auto-calls `selectTask()` if the ID exists in the list (deep-link support from the board's 📊 toolbar button).
- `diag-entries.js` — middle panel. Handles synthetic `__file_summaries__` task ID: fetches `GET /api/budget-entries?task_id=__file_summaries__` which returns entries where `task_id IS NULL`. Session detection allows up to 15% context drop before splitting a session.
- `diag-session.js` — turn summary table (`buildSessionSummary()`), entry selection (3 fetch paths), `jumpToEntry()`. `groupMessages()` accepts `allBoundaries` to break at every conceptual turn boundary.
- `diag-render.js` — right panel; `renderConversation(…, targetMsgIdx)`, `buildCtxBar()`, UI toggles, `_initCtxTooltip()` IIFE, `DOMContentLoaded` init. **`_initDockZoom()` removed** — cosine-falloff neighbor magnification is gone. Segment hover is now pure CSS (`scaleY(1.15)` upward pop, yellow ring). `_initCtxTooltip()` floats a JS-positioned tooltip above the cursor showing agent-type badge, context %, and tool call name/args. Segments are **clickable** — clicking calls `selectEntry(fe.id)` to jump to that turn. Segment colors are tool-category-based (inline `background-color` from `TOOL_COLORS`).
- `diagnostics.css` — entry type colours including `type-file_summary`. **`.ctx-bar.dock-zooming` removed.** `.ctx-bar` is `overflow-x: auto` (scrolls when narrow). `.ctx-seg` has `min-width: 12px`; hover is CSS-only (`scaleY(1.15)`, yellow `box-shadow`). `#ctx-tooltip` + `.ctx-tip-*` classes added for the JS floating tooltip.

**File Summaries in diagnostics** — `GET /api/diagnostics/tasks` returns a synthetic `{id: "__file_summaries__", type: "file_summary"}` row at the top when any `budget_entries` have `task_id IS NULL` (project prewarm calls that aren't tied to a specific task card).

### Configuration (`maestro.ini`)
INI file with sections: `[intake]`, `[subdivision]`, `[capacity]`, `[context_warnings]`, `[scheduler]`, `[verdicts]`, `[search]`.

- `[intake]` — research lives, tiebreaker, LLM temperature, allowed research tools, `context_budget_ratio` (fraction of context window for research agent, default 0.60). `research_agent_tools` includes `web_search` — dispatches `WebSearchAgent` asynchronously (search + fetch + synthesize). `web_fetch` is intentionally absent; it is private to `WebSearchAgent`.
- `[subdivision]` — max_depth, max_retries_per_level, max_total_sub_ideas, llm_temperature, subdivision_agent_tools, `context_budget_ratio` (default 0.60). Both `subdivision_agent_tools` and `subdivision_planning_tools` include `web_search` for domain research during decomposition.
- `[search]` — `provider` (duckduckgo | brave, default duckduckgo), `brave_api_key` (required only if provider=brave). Env overrides: `MAESTRO_SEARCH_PROVIDER`, `BRAVE_API_KEY`.

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
