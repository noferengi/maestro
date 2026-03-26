# Project Maestro — Summary

## What This Is

A Kanban board that doubles as the control surface for an agentic LLM orchestration system.
The board is real and functional. The agent backend includes a deterministic intake pipeline
that gates every column transition behind a multi-stage LLM voting system. The core engine is
the "Wiggum Loop" — a persistent Do-While that drives a local LLM through Orient → Plan →
Implement → Test → Verify cycles until every task in the DAG reaches ACCEPTED.

The LLM target is OmniCoder 9B (Qwen 3.5 base) running via llama.cpp on `localhost:8008`,
OpenAI API compatible, router mode (sequential, parallel_sessions=1).

---

## Recent Work (2026-03-25 session — Column Map View (2D Radial Layout) — ALL COMPLETE)

### Column Map View — interactive 2D radial canvas per column ✓

Clicking any column header (ARCHITECTURE, IDEAS, PLANNING, etc.) or empty whitespace in a
column opens a full-screen **Column Map View** — a 2D radial canvas showing tasks as cards
with thick bezier arrows between connected nodes. Click the header again or "← Back to Board"
to return.

#### What was built

**Migration `0024_map_positions.py`** (NEW) — adds `map_x REAL` and `map_y REAL` (both
nullable) to the `tasks` table. Positions are `NULL` until a task is first rendered on the
map. (Note: originally named `0011` by mistake; renamed before applying to avoid collision
with the existing `0011` big-idea migration.)

**`database.py`** — `map_x` and `map_y` `Float` columns on `Task` model. New
`batch_update_map_positions(updates)` function: bulk-updates positions in a single DB
transaction without touching task history.

**`main.py`** — `map_x`/`map_y` added to `task_to_dict` and `allowed_fields`. New
`PATCH /api/tasks/map-positions` endpoint: accepts `[{id, map_x, map_y}, ...]`, calls
`batch_update_map_positions`, returns `{"updated": N}`. No history side-effects.

**`app/web/kanban.js`** — large new section at end of file:

- `openColumnMap(colType)` / `closeColumnMap()` — toggle between kanban and map view.
  Hides `.kanban-board`, shows `#column-map-container` (fixed overlay, `left: 240px`).
- `handleColumnClick(e, colType)` / `handleTasksContainerClick(e, colType)` — click
  guards that skip cards/buttons before opening the map.
- `_mapComputeLayout(tasks, colType)` — **three-phase** layout engine:
  - Phase 1: load saved `map_x / map_y` from task data into `nodePositions`
  - Phase 2: BFS fan-out — newly-subdivided children of positioned parents get radial
    positions derived from their parent (handles the subdivision case without recomputing
    the whole board)
  - Phase 3: standard radial `placeSubtree()` for completely-unpositioned subtrees
  - IDEAS/ARCHITECTURE: hierarchy via `parent_task_id`
  - All other columns: hierarchy via `prerequisites`
  - Returns `{nodes: [{id, x, y, task, newlyPositioned}], edges: [{fromId, toId}]}`
- `renderColumnMap(colType)` — computes bounding box, sets SVG/canvas size, populates
  shared state (`_mapCurrentEdges`, `_mapCurrentNodePositions`, `_mapCurrentColor`,
  `_mapOffsetX`, `_mapOffsetY`), calls `_mapRedrawArrows()`, renders `.map-node` divs,
  centers viewport, calls `_mapSavePositions()` for newly-positioned nodes.
- `_mapRedrawArrows()` — removes all SVG `<path>` elements and redraws cubic bezier
  arrows from current `_mapCurrentNodePositions`. Called once on render and on every
  node-drag tick. Arrowhead via `<marker id="map-arrowhead">`.
- `_mapScreenToCanvas(screenX, screenY)` — converts viewport coords to canvas-space
  coords accounting for pan (`mapTransform.x/y`) and zoom (`mapTransform.scale`).
- `_mapStartNodeDrag(e, nodeId)` — initiates group drag. Collects dragged node +
  all descendants via `descendantIndex[nodeId]`. Snapshots layout positions for the
  whole group. Adds `.map-node-dragging` to grabbed node, `.map-node-dragging-child`
  to descendants.
- `_mapSavePositions(toSave)` — async fire-and-forget: `PATCH /api/tasks/map-positions`,
  mirrors new coords into live `taskData` so next reconcile sees them as saved.
- `setupMapInteraction()` / `teardownMapInteraction()` — mousedown/mousemove/mouseup/wheel
  handlers on `#column-map-scroll-wrap`. Mousemove checks `_mapNodeDrag.active` first
  (node drag), then falls through to canvas pan. Mouseup saves all moved nodes in one
  batch call.
- `reconcile()` — skips DOM reconciliation when `columnMapActive`; keeps `taskData`
  fresh so positions saved during map session are visible immediately on close.

**Group drag behaviour:**
- Dragging a parent node (BIG IDEA or any node with children) moves the entire cluster —
  grabbed node + all descendants — by the same delta simultaneously. Arrows redraw live.
- Dragging a leaf node moves only that node; parent and siblings stay.
- On mouseup: one `_mapSavePositions` batch call persists all moved nodes.

**Pan / zoom:**
- Mouse drag on empty canvas: pan. Scroll wheel: zoom toward cursor (0.15×–4×).
- Canvas transform: `translate(panX, panY) scale(zoom)` on `#column-map-canvas`.

**`app/web/style.css`** — new `Column Map View` section:
- `#column-map-container` — `position: fixed; left: 240px` overlay, `z-index: 50`
- `#column-map-scroll-wrap` — `cursor: grab`, `overflow: hidden`
- `#column-map-canvas`, `#column-map-svg` — `overflow: visible` (dragged nodes and
  arrows don't clip when moved outside initial bounds)
- `.map-node` — `cursor: grab; user-select: none`; hover lifts with shadow
- `.map-node.map-node-dragging` — `scale(1.04)`, heavy shadow, `transition: none`
  (instant follow, no spring lag), `z-index: 100`
- `.map-node.map-node-dragging-child` — lighter shadow, `opacity: 0.88`, `z-index: 50`
- `.map-btn` — small action buttons inside map nodes (Edit, Advance, Children, → Dev)
- `.column-header` — `cursor: pointer` + hover tint; `::after` adds `↗` hint glyph

**`app/web/index.html`** — `onclick="openColumnMap('...')"` on every column header;
`onclick="handleTasksContainerClick(event,'...')"` on every `.tasks-container`. New
`#column-map-container` div (fixed overlay) with header bar and `#column-map-scroll-wrap`.

---

## Recent Work (2026-03-24 session — Scheduler-Dispatched File Summaries + Testing — ALL COMPLETE)

### Scheduler-dispatched file summary jobs ✓

**Problem:** `read_file()` cache misses called `await call_llm()` inline, bypassing the scheduler's
LLM capacity tracking. The agent session was blocked waiting while the slot went untracked.

**Solution:** File summary LLM calls now route through the scheduler's job queue — same pattern
as research jobs — with a general-purpose `threading.Event` registry so the blocked agent wakes
up instantly when the job completes. File summary jobs get **top priority** (dispatched before all
other job types in `_tick()`) because the calling agent is blocked waiting.

#### Changes made:

- **Migration `0022_file_summary_jobs.py`** (NEW) — `file_summary_jobs` table. Priority `-1.0`
  (sorts before research jobs at `0.0`). Indexes on `(status, priority, created_at)` and
  `(sha1_hash, file_size_bytes)`. `file_content` stored in the job row (capped at 32k chars)
  so worker threads don't need filesystem access.

- **`database.py`** — `FileSummaryJob` SQLAlchemy model + 5 CRUD functions:
  `create_file_summary_job`, `get_pending_file_summary_jobs` (ordered by priority ASC),
  `get_file_summary_job_by_sha1` (dedup: finds pending/running jobs for same content),
  `update_file_summary_job` (auto-sets `completed_at` on terminal status),
  `count_pending_file_summary_jobs` (for scheduler status endpoint).

- **`scheduler.py`** — General-purpose completion registry (`_pending_completions` dict +
  lock) with three public functions: `get_or_create_completion_event(key)` (thread-safe
  get-or-create, returns `(Event, created: bool)`), `signal_completion(key)` (pops event,
  calls `.set()`), `wait_for_completion(key, timeout)` (returns `True` if key already gone =
  completed before wait started). `_dispatch_file_summary_jobs()` and
  `_run_file_summary_job()` mirror the research job pattern. `_tick()` now calls
  `_dispatch_file_summary_jobs()` first. `get_scheduler_status()` includes
  `pending_file_summary_jobs` count.

- **`file_summary_agent.py`** — Rewritten. Old `run_file_summary()` split into:
  - `enqueue_file_summary(abs_path, *, task_id, llm_id, budget_id)` — checks DB cache,
    deduplicates via `get_file_summary_job_by_sha1`, calls `get_or_create_completion_event`,
    creates DB job only if event was newly created and no existing job. Returns
    `(completion_key, sha1, filesize)` where `completion_key == ""` means cache hit.
  - `execute_file_summary(*, sha1, filesize, file_path, file_content, ...)` — performs the LLM
    call, stores result via `create_file_summary()`, returns token counts. Called by scheduler
    worker thread.

- **`project_snapshot.py`** — `async_build_file_summary()` updated to use enqueue + wait:
  calls `enqueue_file_summary()`, if `completion_key` non-empty awaits
  `loop.run_in_executor(None, wait_for_completion, key, 120.0)`, then reads from DB cache.
  Falls back to structural-only on timeout or error.

  **Production bug fixed:** `_file_summary_cache` was shared between `build_file_summary`
  (writes structural results under `(abs_path, mtime, size)`) and `async_build_file_summary`
  (was checking the same key for LLM-enhanced results). Every first call to
  `async_build_file_summary` was returning structural-only because `build_file_summary` —
  called at the top of the function — had already primed the cache before the session-cache
  check ran. Fixed by using `("llm", abs_path, mtime, size)` as the key in
  `async_build_file_summary`, keeping LLM-enhanced entries in distinct slots.

#### Race condition safety:
| Race | Outcome |
|------|---------|
| Two agents, same file | First gets `created=True`, creates job. Second gets `created=False`, shares Event. Worker signals once, both wake, both read cache. Correct. |
| Complete before wait | `signal_completion` removes key. `wait_for_completion` finds no key → `True`. DB has result. Correct. |
| Job fails | Worker signals in `finally` regardless. Caller wakes, cache miss, falls back to structural. No hang. |
| Scheduler not running | `event.wait(120)` times out → structural fallback. Job sits pending — harmless. |

### Testing: `async_build_file_summary` integration tests ✓

Added 14 new tests to `test_read_file_redesign.py`:

**Completion registry (3 tests):**
- `test_completion_registry_basic` — signal then wait returns True
- `test_completion_registry_timeout` — no signal, wait returns False
- `test_completion_registry_dedup` — same key twice → same Event, `created=False`

**`enqueue_file_summary` unit tests (4 tests):**
- `test_enqueue_cache_hit` — cached file → empty completion_key, no job
- `test_enqueue_creates_job` — uncached → pending job in DB, key returned
- `test_enqueue_dedup_shared_event` — two calls same content → one job, shared key
- `test_execute_stores_result` — mock `call_llm` → verify cache populated, token counts returned

**`async_build_file_summary` integration tests (7 tests):**
- `test_abfs_summary_length_none_skips_enqueue` — early return, enqueue never called
- `test_abfs_session_cache_hit` — in-memory LLM cache hit returns stored result
- `test_abfs_db_cache_hit` — enqueue returns `""` → reads DB, prepends `## Summary`
- `test_abfs_db_cache_hit_populates_session_cache` — combined result stored under `("llm",...)` key
- `test_abfs_cache_miss_waits_and_reads` — enqueue returns key, real `wait_for_completion` returns True immediately (key not in registry), DB read succeeds
- `test_abfs_timeout_falls_back_to_structural` — timeout → structural only
- `test_abfs_enqueue_error_falls_back_to_structural` — exception → structural only

**Test count: 572 tests, all passing.**

---

## Previous Sessions

### 2026-03-24 — P0-A/B + P1 + P2 + Hover Labels + Dock Zoom — ALL COMPLETE

All four previous next-steps items have been completed, plus a hover label overlay and
macOS Dock-style magnification on the context bar. See git log for full diffs.

- **P0-A** — TOO_LARGE verdict for context overflow ✓
- **P0-B** — GBNF epilogue 500 fallback ✓
- **P1** — Context size visualization in diagnostics turn table ✓
- **P2** — Color-coded agent type labels ✓
- **Context bar hover labels** ✓ — `#1992 call(863):13.6K pp=45.2K tg=863 $0.0012`
- **macOS Dock-style magnification** ✓ — cosine falloff, 5× peak, 24px radius

### 2026-03-23 — Research Agent Life Enrichment + Modal Fix + Live Testing
Post-mortem handoff system, `_modalMousedownTarget` drag-close fix, discovered P0-A/P0-B bugs. See git log.

### 2026-03-23 — Diagnostics & Scheduler Polish
`remote_call_id` tooltip, budget-exhaustion skip, LLM name in turn table, non-accumulating
session render fix, `ensure_git_repo()`. See git log.

---

## Next Steps

(No outstanding items — suggest new priorities.)

---

## File Structure

```
app/
├── main.py                  FastAPI app. lifespan context manager.
├── logging_config.py        configure_logging(). RotatingFileHandler guarded against double-add.
├── database.py              SQLAlchemy models + all CRUD.
│                            FileSummaryJob model + 5 CRUD functions (create/get_pending/
│                            get_by_sha1/update/count).
│                            append_task_history(task_id, status, message=None)
├── agent/
│   ├── config.py            Single config interface.
│   ├── json_utils.py        extract_json_block(), parse_json_block().
│   ├── tools.py             27 safe tools. ensure_git_repo(path).
│   ├── system_prompt.py     MAESTRO_SYSTEM_PROMPT
│   ├── loop.py              MaestroLoop.
│   ├── dag.py               DAGResolver — Kahn's sort, cycle detection.
│   ├── verdicts.py          Verdict enum, Vote, TallyResult, tally_votes()
│   ├── static_analysis.py   Tree-sitter deterministic code parser.
│   ├── intake.py            IntakePipeline (IDEA → PLANNING, 4-stage voting)
│   ├── planning.py          PlanningPipeline (5 stages, best-of-N).
│   ├── planning_gate.py     PlanningGate (7 checks)
│   ├── dev_orchestrator.py  DevOrchestrator (batch, parallel components)
│   ├── component_loop.py    ComponentLoop + ComponentToolDispatcher.
│   ├── conceptual_review.py ConceptualReviewPipeline
│   ├── optimization.py      OptimizationPipeline.
│   ├── security_review.py   SecurityPipeline (3 agents, veto power).
│   ├── full_review.py       FullReviewPipeline (4 parallel reviewers)
│   ├── merge.py             Deterministic git merge. ensure_git_repo() at top.
│   ├── merge_conflict_resolver.py  LLM-assisted resolver
│   ├── research.py          Research agent (lives system).
│   ├── subdivide.py         SubdivisionAgent
│   ├── scheduler.py         Priority queue. Budget pre-flight check.
│   │                        Completion registry: get_or_create_completion_event(),
│   │                        signal_completion(), wait_for_completion().
│   │                        _dispatch_file_summary_jobs() runs FIRST in _tick().
│   ├── llm_client.py        Centralized HTTP client. grammar kwarg → GBNF payload field.
│   ├── file_summary_agent.py  enqueue_file_summary() + execute_file_summary().
│   │                          (Old run_file_summary() removed.)
│   ├── project_snapshot.py  build_project_snapshot(), build_file_summary(),
│   │                        async_build_file_summary() — uses enqueue+wait pattern.
│   │                        Session cache uses ("llm",...) prefix to avoid collision
│   │                        with structural entries from build_file_summary().
│   └── mock_llm.py          Dictionary-based mock LLM
├── migrations/
│   ├── runner.py            Standalone sqlite3 migration engine
│   └── versions/            0001–0024 applied
│                            0022 = file_summary_jobs table
│                            0023 = previous_summary column
│                            0024 = map_x / map_y on tasks (Column Map View)
├── models/
│   └── dags.py              TaskDAG, TaskNode
├── services/
│   └── repl.py              CheckpointManager.
├── tests/                   572 tests total, all passing
│   ├── test_verdicts.py             24 tests
│   ├── test_research_agent_unit.py  47 tests
│   ├── test_budget_cost.py          16 tests
│   ├── test_read_file_redesign.py   29 tests (15 new this session)
│   └── [23 other test files]
└── web/
    ├── CLAUDE.md            Frontend guide: file map, function index, CSS class reference.
    ├── index.html           Board UI.
    ├── kanban.js            All board behaviour (monolithic).
    ├── style.css            All board styles (monolithic).
    ├── diagnostics.html     Standalone diagnostics page. Loads diag-*.js in order.
    ├── diag-utils.js        Shared state globals + pure helpers.
    ├── diag-tasks.js        Left panel.
    ├── diag-entries.js      Middle panel.
    ├── diag-session.js      Turn summary table, entry selection, jumpToEntry().
    ├── diag-render.js       Right panel: renderConversation(), buildCtxBar(),
    │                          _initDockZoom() IIFE (cosine falloff, 5× peak, 24px radius).
    └── diagnostics.css      Layout + diagnostic styles.
data/
└── kanban.db                SQLite (24 migrations applied)
logs/
└── maestro.log              Rotating log file
scripts/
└── inspect_llm_turns.py     CLI for browsing budget_entries
Launcher.bat                 Launches uvicorn server from repo root via %~dp0
maestro.ini                  Master config
```

---

## The 9-Stage Pipeline

```
IDEA → [intake] → PLANNING → [planning + gate] → INDEV → [dev_orchestrator]
     → CONCEPTUAL_REVIEW → [conceptual_review] → OPTIMIZATION → [optimization]
     → SECURITY → [security_review] → FULL_REVIEW → [full_review] → COMPLETED
```

### Advance Handlers

| Column              | Handler                      | Trigger  |
|---------------------|------------------------------|----------|
| `idea`              | `_run_intake_pipeline`       | Auto (scheduler) + manual `/advance` |
| `planning`          | `_run_planning_pipeline_bg`  | Auto (scheduler + manual) |
| `indev`             | `_run_dev_orchestrator_bg`   | Auto (scheduler) |
| `conceptual_review` | `_advance_to_optimization`   | Auto (scheduler) + manual |
| `optimization`      | `_run_security_pipeline_bg`  | Auto (scheduler) + manual |
| `security`          | `_run_full_review_bg`        | Auto (scheduler, transient) + manual |
| `full_review`       | `_execute_merge_bg`          | Auto (scheduler) + manual |

---

## Tool System (27 tools)

- **Path containment** — `_assert_safe_path()` resolves symlinks then checks `startswith(effective_root)`.
- **Soft-delete only** — `archive_file` moves to `.archive/<timestamp>/`.
- **Shell blocklist** — 19 regex patterns. `wget ... | sh` is NOT blocked (known gap).
- **Git branch allowlist** — only `maestro/task-*` + `main`/`master`.
- **`record_benchmark`** — params: `task_id`, `parent_task_id`, `benchmark_type`, `metrics` (JSON string)
- **`ensure_git_repo(path)`** — called before every `_git_run()`. Auto-inits bare directories once per path per process.
- **`read_file(path)`** — returns structural summary for files >25 lines; raw content for ≤25 lines. Marks file as prepped. Uses `build_file_summary()` (sync, no LLM).
- **`read_file_harder(path, start, end|count)`** — requires prior `read_file()` prep. Returns raw source lines (250-line cap). For LLM-enhanced summaries, call `async_build_file_summary()` with `summary_length != "none"`.

---

## Test Suite

**572 tests total, all passing.**

```bash
venv/Scripts/python.exe -m pytest app/tests/ -v
venv/Scripts/python.exe -m pytest app/tests/test_read_file_redesign.py -v
```

Key patching patterns:
- **Intake tests**: patch `app.agent.intake.call_llm` directly
- **llm_client tests**: patch `httpx.AsyncClient` with `_make_mock_client(post_response)`
- **e2e tests**: patch `httpx.AsyncClient` with `_mock_client_cls(mock_llm)`
- **Sync test methods calling async**: use `asyncio.run(coro)` — NOT `get_event_loop().run_until_complete()`
- **Research epilogue tests**: use `_sequential_llm(*responses)`. Format: raw JSON no code fences.
  `{"grade": 8000, "justification": "...", "verdict": "POSSIBLE"}` → confidence = 80.
- **Post-mortem tests**: each exhausted life adds 1 extra LLM call. With `max_turns_per_life=1,
  max_lives=2`: 5 total calls (life1_turn, life1_pm, life2_turn, life2_pm, epilogue).
- **`async_build_file_summary` tests**: patch `app.agent.file_summary_agent.enqueue_file_summary`
  and `app.database.get_file_summary` (on the db module object). Use `clean_session_cache`
  fixture (autouse=False) to clear `project_snapshot._file_summary_cache` between tests.
  Session cache uses `("llm", abs_path, mtime, size)` key — NOT `(abs_path, mtime, size)`.
- **`enqueue_file_summary` tests**: use `_make_db_patch(monkeypatch, tmp_path)` to redirect
  DB to a temp file. Pass `llm_id=None, budget_id=None` to avoid FK constraint failures on
  empty test DBs.

---

## Running Locally

```bash
Launcher.bat                                          # server
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000  # manual
migrate.bat status / migrate / reset
venv/Scripts/pip.exe install -e .
```

Board: `http://localhost:8000` | Diagnostics: `http://localhost:8000/diagnostics`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B, router mode, parallel=1)
To advance a task via curl: `curl -X POST http://localhost:8000/api/tasks/{id}/advance`

---

## Key Design Decisions

- **Single config interface** — `config.py` is the only import for tuneable values.
- **`call_llm` enforces `budget_id`** — raises `ValueError` if `budget_id is None`.
- **`extract_json_block` returns a raw string** — callers must `json.loads()`. NOT a dict.
- **`response_format` removed from ALL planning LLM calls** — llama.cpp grammar validator + 4096 token limit = reliable 500 on large outputs.
- **`slots=True` dataclasses require `asdict()` not `vars()`**.
- **Forced verdict epilogue uses GBNF grammar-constrained generation** — eliminates hallucinated fields at the token-sampler level. Known issue: grammar string fails to parse on some llama.cpp builds (pos 657); currently falls back to NOT_SUITABLE/40.
- **`grade` is int 0–10000** — hundredths of a percent (9258 = 92.58%). `confidence = grade // 100`, clamped to verdict's valid range.
- **Epilogue NEEDS_RESEARCH tagged `source: "research_agent_epilogue"`** — tally_votes() Rule 3 source guard treats it as neutral abstention.
- **`lifespan` over `on_event`** — FastAPI lifespan context manager.
- **`Launcher.bat` uses `%~dp0`** — works from any CWD.
- **Diagnostics page is a separate route** — three-panel layout needs full viewport width.
- **`budget_entries` stores one row per LLM call** — `prompt_data` = full messages JSON, `response_data` = full OpenAI response JSON.
- **Session detection by context growth** — consecutive entries with growing `prompt_cost` AND time gap < 5 min = same session.
- **Cost stored as µ¢ (microcents)** — rate $/M × 100 = µ¢/token. `dollar_amount == -1` → infinite budget, tokens still tallied.
- **`isAccumulating` drives all session render choices** — `effectiveMessages`, `effectiveBoundaries`, `effectiveHighlight`, `hlClass`, and `jumpToEntry` eligibility all derived from it.
- **`ensure_git_repo()` is idempotent per path** — `_git_init_attempted` set prevents retry on failure.
- **Column Map positions are in layout-space, not canvas-space** — `map_x/map_y` are the coords produced by the radial layout algorithm (centered around 0). Canvas position = layout + `(_mapOffsetX, _mapOffsetY)`. The offset is recomputed from the bounding box each render, so saved positions are stable across sessions regardless of how the bounding box shifts.
- **`batch_update_map_positions` skips task history** — position saves are high-frequency (one per drag-drop) and must not pollute the `history` column. Dedicated DB function bypasses `update_task()`.
- **`PATCH /api/tasks/map-positions` must come before `DELETE /api/tasks/{task_id}`** — FastAPI path matching: a literal segment (`map-positions`) must be registered before a parameterised one (`{task_id}`) or the literal is swallowed as a task ID.
- **Group drag uses `descendantIndex`** — dragging any node moves it + all descendants by the same delta. `descendantIndex` is already built by `buildDescendantIndex()` on every task load; no extra traversal needed at drag time.
- **`#column-map-canvas` and `#column-map-svg` both need `overflow: visible`** — without it, nodes dragged outside the initial bounding box clip at the canvas edge and arrows disappear past the SVG viewport.
- **`append_task_history()` touches only the history column** — avoids the side-effect of `update_task()`.
- **Post-mortem is non-blocking and always caught** — `_post_mortem_call()` returns `""` on any exception. A failed post-mortem silently degrades to the old "exhausted N turns" findings text.
- **`_modalMousedownTarget` is global** — one `mousedown` listener covers all modals. Close only fires when both `mousedown` and `click` land on the backdrop element.
- **Post-mortem call uses full message history** — if context is already overflowing (400s on final turns), the post-mortem call will also 400. Known limitation; TOO_LARGE handling (P0-A) will short-circuit before this point.
- **File summary jobs use priority -1.0** — dispatched before research jobs (0.0) in `_tick()` because the calling agent thread is blocked waiting on a `threading.Event`.
- **Completion registry is general-purpose** — `_pending_completions` dict in `scheduler.py` uses opaque string keys (e.g. `"file_summary:{sha1}:{size}"`). Future tool-needs-LLM patterns reuse it with different prefixes.
- **`_file_summary_cache` uses `("llm", path, mtime, size)` for LLM-enhanced entries** — prevents collision with structural entries that `build_file_summary()` writes under `(path, mtime, size)`. Without the prefix, every first call to `async_build_file_summary()` silently returned structural-only because `build_file_summary()` runs first and primes the cache.
- **`enqueue_file_summary` returns `("", sha1, filesize)` on cache hit** — empty string signals "no wait needed; read DB directly". Caller checks `if completion_key:` to branch.
- **Worker always signals completion even on failure** — `signal_completion(key)` called in `finally` block of `_run_file_summary_job`. Waiters never hang regardless of job outcome.
