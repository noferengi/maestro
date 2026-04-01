# Project Maestro — Living Summary

## What this is

A Kanban board with an agentic LLM orchestration backend. The board is the UI face of a
"Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test →
Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition through a 9-stage
pipeline (IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY →
FULL_REVIEW → COMPLETED), gated by multi-stage intake voting. A horizontal Architecture
Bar above the pipeline columns holds architectural constraints that are injected into all
agent prompts.

---

## Recent work (this session)

### Arch Bar Populate feature (complete)

Added a `⚡ Populate` button to the architecture bar header. When clicked it queues
scheduler jobs to generate one architecture card per missing category, using existing
file summaries as context. No existing cards are modified.

**Migration 0036** — `arch_gen_jobs` table:
- `project`, `category`, `llm_id`, `budget_id`, `status`, `priority` (1.0), token counts,
  `error_message`, `created_at`, `completed_at`
- Index on `(status, priority, created_at)` for fast dispatch

**`app/database/models.py`** — `ArchGenJob` SQLAlchemy model added after `OptimizationBenchmark`.

**`app/database/crud_jobs.py`** — Four new functions: `create_arch_gen_job`,
`get_pending_arch_gen_jobs`, `update_arch_gen_job`, `get_retriable_arch_gen_jobs`.

**`app/database/crud_files.py`** — `get_file_summaries_for_project_root(project_root)`:
returns all `FileSummary` rows whose `file_path` is under the given root (handles both
`/` and `\` separators via LIKE).

**`app/database/__init__.py`** — all new symbols re-exported; `ArchGenJob` in models
block; reload cascade list unchanged (submodule list already covers `crud_jobs` and
`crud_files`).

**`app/agent/arch_gen_agent.py`** (new) — single-call agent (Option A):
1. `get_file_summaries_for_project_root(project_root)` → list of `FileSummary` rows
2. Builds prompt: each file → relative path + up to 2 sentences (short_summary preferred,
   truncated with `_two_sentences()`)
3. `call_llm()` with `temperature=0.4`, `max_tokens=256`
4. `create_task(type='architecture', content={"category": cat, "priority": "normal"}, ...)`
5. Raises on empty response or missing summaries so scheduler marks the job `failed`

**`app/agent/scheduler.py`** additions:
- `_ARCH_GEN_RETRY_COOLDOWN = 300.0` constant
- `_dispatch_arch_gen_jobs()` — same pattern as `_dispatch_file_summary_jobs`: one-LLM
  gate, node/LLM capacity check, spawns `_run_arch_gen_job` thread; returns void (no
  blocked caller to propagate `allowed_llm_id` to)
- `_run_arch_gen_job()` — new event loop per thread, calls `execute_arch_gen_job`,
  marks completed/failed, decrements `_llm_session_counts`
- `_rescue_stale_jobs()` extended with arch gen rescue block (orphaned `running` +
  cooled-down `failed` → reset to `pending`)
- `_tick()` step 5.5 added: `_dispatch_arch_gen_jobs(...)` between research and
  subdivision recovery

**`app/main.py`** — `POST /api/projects/{project_name}/populate-arch`:
- Reads existing arch tasks for the project, collects used categories
- For each of the 14 missing categories, calls `create_arch_gen_job`
- Uses `_pick_prewarm_resources()` for LLM/budget selection
- Returns `{"queued": N, "categories": [...]}` or 503 if no LLM/budget available

**`app/web/index.html`** — `⚡ Populate` button added between `+ Add` and `▲` toggle.

**`app/web/kanban.js`** — `populateArchBar()`: POSTs, shows "Queued N" or "All done",
button disabled + label restored after 3 s.

### Migration naming fix
`0035_arch_gen_jobs.py` had a naming collision with the existing
`0035_file_summary_short_summary.py`. Renamed to `0036_arch_gen_jobs.py` and re-migrated.
Both tables correctly applied; 36 total migrations.

---

## Next steps

### P0 — Nothing blocking; all features functional

### P1 — Improvements

**Populate status feedback** — the button shows "Queued N" but the user has no visibility
into when the jobs complete. Options: poll `GET /api/scheduler/status` or add a dedicated
`GET /api/projects/{name}/arch-gen-jobs` endpoint returning counts by status. The
`reconcile()` 5-second loop will surface new cards automatically once they land.

**Populate deduplication** — if the user clicks Populate twice quickly, duplicate jobs are
created for the same category. The endpoint doesn't check for already-pending jobs. Add a
check: skip categories that already have a pending/running `arch_gen_job`.

**Short summary column usage** — migration 0035 added `short_summary` to `file_summaries`,
but `file_summary_agent.py` needs to be verified to actually populate it. If it doesn't,
arch gen falls back to the full summary and `_two_sentences()` truncation, which is fine
but not ideal.

**Populate with prewarm gate** — if no file summaries exist yet, `execute_arch_gen_job`
raises. The endpoint could detect this and return a helpful 409 suggesting the user run a
prewarm first, rather than silently creating jobs that will all fail.

### P2 — Future

- **Populate progress indicator** — arch bar subtitle showing "Generating N categories…"
  while arch_gen_jobs are pending/running, cleared when all complete.
- **Regenerate single card** — toolbar button on an arch card to re-queue an arch_gen_job
  for just that category (replacing the existing card on completion).
- **Per-category quality gate** — after generation, run a quick self-critique pass asking
  the LLM to rate the note's specificity; retry if it scores poorly.

---

## File structure (key files)

```
app/
  main.py                    FastAPI app, all routes
  agent/
    arch_gen_agent.py        NEW: arch card generation from file summaries
    config.py                INI-driven constants
    dag.py                   DAGResolver (Kahn's topo sort)
    file_summary_agent.py    File summary generation agent
    intake.py                IDEA→PLANNING pipeline
    llm_client.py            Centralized LLM HTTP client
    loop.py                  MaestroLoop (Design→Implement→Test→Verify)
    planning.py / planning_gate.py
    conceptual_review.py / security_review.py / full_review.py / optimization.py
    project_snapshot.py      build_project_snapshot, build_architecture_context
    research.py              Research agent (lives system)
    scheduler.py             Push-first eager scheduler (tick loop)
    subdivide.py             Subdivision agent
    tools.py                 Agent tool implementations
    verdicts.py              Vote tally logic
  database/
    __init__.py              Re-exports everything
    models.py                All 23 SQLAlchemy models (incl. ArchGenJob)
    crud_tasks.py            Task CRUD + history
    crud_projects.py         Project CRUD
    crud_infra.py            LLM + Budget + ComputeNode CRUD
    crud_costs.py            BudgetEntry + Expense
    crud_pipeline.py         Pipeline audit tables
    crud_jobs.py             ResearchJob + FileSummaryJob + OptimizationBenchmark + ArchGenJob
    crud_files.py            FileSummary + SearchCache (+ get_file_summaries_for_project_root)
    crud_inbox.py            InboxMessage
    session.py               Engine, SessionLocal, Base
  migrations/
    runner.py                Standalone sqlite3 migration engine
    versions/
      0001–0036              Applied migrations (36 total)
      0036_arch_gen_jobs.py  arch_gen_jobs table
  web/
    index.html               Board shell (arch bar + 8 pipeline columns + 9 modals)
    kanban.js                All board behaviour
    style.css                Board styles
    scheduler.html / scheduler.js   Scheduler debug view
    diagnostics.html + diag-*.js   LLM conversation viewer
  tests/                     pytest suite
data/
  kanban.db                  SQLite database
maestro.ini                  Runtime configuration
```

---

## 9-stage pipeline

```
IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED
```

Special types: `architecture` (arch bar only, never dispatched), `subdividing` (Big Idea
mid-subdivision).

Handlers:
- **IDEA**: `IntakePipeline` (4-stage vote: scope/static/feasibility/conflict) → PLANNING or SUBDIVIDE_IDEA
- **PLANNING**: `PlanningPipeline` + `PlanningGate`
- **INDEV**: `MaestroLoop` (Wiggum loop, dispatched by scheduler)
- **CONCEPTUAL_REVIEW**: `ConceptualReviewPipeline`
- **OPTIMIZATION**: `OptimizationPipeline`
- **SECURITY**: `SecurityPipeline`
- **FULL_REVIEW**: `FullReviewPipeline`
- **COMPLETED**: terminal

---

## Scheduler job types and priorities

| Type | Priority | Notes |
|---|---|---|
| `FileSummaryJob` | -1.0 | Highest — callers block on completion event |
| `ResearchJob` | 0.0 | Background investigations |
| `ArchGenJob` | 1.0 | NEW — fire-and-forget arch card generation |
| DAG tasks | computed | Based on pipeline stage + position |

All jobs respect: one-LLM-at-a-time policy, per-LLM `parallel_sessions` cap,
per-node `max_loaded_models` + `max_parallel_sessions` caps, 5-min retry cooldown on
failure, orphan rescue on restart.

---

## Architecture Bar

14 fixed categories: `Platform`, `Design`, `Testing`, `Security`, `Performance`, `API`,
`Tooling`, `Data`, `UX`, `Accessibility`, `Compliance`, `Deployment`, `Observability`,
`General`.

Cards: `type='architecture'`, `content={"category": str, "priority": critical|high|normal|low}`,
body in `description`. Never appear in pipeline columns. Injected into all agent prompts
via `build_architecture_context(project_name, agent_type)` with per-agent category
filtering (`ARCH_CATEGORY_RELEVANCE` in `project_snapshot.py`).

Header buttons: `+ Add`, `⚡ Populate` (NEW), `▲` collapse.

---

## Tool system

`app/agent/tools.py` — all tools use `effective_root` for path resolution. Categories:
- **File I/O**: `read_file`, `write_file`, `append_file`, `list_files`, `count_lines`
- **Search**: `web_search` (DuckDuckGo or Brave via `SEARCH_PROVIDER`), `web_fetch` (private to WebSearchAgent)
- **Git**: `git_status`, `git_diff`, `git_commit`, `git_checkout` (maestro/* branches only)
- **Exec**: `run_shell` (blocklist enforced)
- **Soft-delete**: `archive_file` (moves to `.archive/`, no hard delete)
- **Task queries**: `get_task_info`, `list_tasks`

---

## Test suite

~200+ tests across `app/tests/`. Key patterns:
- `conftest.py` sets `MAESTRO_TEST_DB` env var to a temp path; all tests use isolated DB
- `importlib.reload(app.database)` cascade re-initializes the engine in each test that
  patches the DB path via monkeypatch
- Mock LLM via `app.agent.mock_llm` — dictionary-based response fixture
- Scheduler tested via `test_scheduler_unit.py` (tick isolation, capacity caps, DAG dispatch)

---

## Running locally

```bash
# Server
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

# Tests
venv/Scripts/python.exe -m pytest app/tests/ -v

# Migrations
venv/Scripts/python.exe app/migrations/runner.py status
venv/Scripts/python.exe app/migrations/runner.py migrate

# Diagnostics
venv/Scripts/python.exe scripts/inspect_cards.py scheduler
venv/Scripts/python.exe scripts/inspect_cards.py activity --hours 4
```

---

## Key design decisions

- **Arch gen is Option A (direct LLM call)** — not a full agent. The input (file summaries)
  is already computed; the task is a single inference step. No tool use, no multi-turn.
  Consistent with `file_summary_agent.py`. Full resilience via existing scheduler retry
  machinery (no completion events needed since no caller is blocked waiting).

- **Priority 1.0 for arch gen** — lower than research (0.0) since arch gen has no blocked
  caller; higher number = lower priority in the scheduler.

- **`CREATE TABLE IF NOT EXISTS` in migrations** — idempotent. Renaming `0035` → `0036`
  re-ran the migration safely; the table already existed from the first (duplicate-ID) run.

- **`_pick_prewarm_resources()` reused** — populate-arch uses the same LLM/budget
  fallback logic as project prewarm. Project must have a default LLM+budget or a global
  one must exist.

- **No completion events for arch gen** — unlike file summaries (which block agent threads
  and need `signal_completion()`), arch gen is purely fire-and-forget. Cards appear when
  ready; `reconcile()` surfaces them within 5 seconds.
