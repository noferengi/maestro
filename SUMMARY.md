# Project Maestro — Summary

## What This Is

A Kanban board that doubles as the control surface for an agentic LLM orchestration system.
The board is real and functional. The agent backend includes a deterministic intake pipeline
that gates every column transition behind a multi-stage LLM voting system. The core engine is
the "Wiggum Loop" — a persistent Do-While that drives a local LLM through Orient → Plan →
Implement → Test → Verify cycles until every task in the DAG reaches ACCEPTED.

The LLM target is OmniCoder 9B (Qwen 3.5 base) running via llama.cpp on `localhost:8008`,
OpenAI API compatible.

---

## Recent Work (2026-03-22 session — Optimization Pipeline Overhaul)

### Optimization Pipeline: Multi-Metric Benchmarking & A/B Decision Framework (COMPLETED)

The optimization pipeline previously made accept/skip/reject decisions based on a single scalar
metric (`test_duration_ms` or `complexity_score`) against fixed 2%/5% thresholds. The whole
thing was retooled into a weighted multi-metric A/B framework.

**`app/agent/config.py`** — Added 7 new constants under `[optimization_weights]`:
- `OPTIMIZATION_COMPUTE_WEIGHT = 1.0` (most precious — time is the scarcest resource)
- `OPTIMIZATION_MEMORY_WEIGHT = 0.6`
- `OPTIMIZATION_STORAGE_WEIGHT = 0.3`
- `OPTIMIZATION_READABILITY_PENALTY_MAX = 0.5` (readability_cost=1.0 halves the score)
- `OPTIMIZATION_PREMATURE_MULTIPLIER = 2.0` (is_premature → need 2× threshold)
- `OPTIMIZATION_TECH_DEBT_BONUS_PCT = 1.0` (bonus % for tech_debt_resolved=true)
- `BIG_O_RANKING` dict (O(1)=1 … O(n!)=8) + `OPTIMIZATION_BIG_O_BONUS_PCT = 10.0`

**`maestro.ini`** — New `[optimization_weights]` section with all tuneable values documented.

**`app/agent/tools.py`** — Updated `record_benchmark` tool schema description. New expected
metrics JSON keys: `big_o_class`, `scale_n`, `readability_cost`, `is_premature`,
`tech_debt_resolved`, `notes` (all optional; old records with only `test_duration_ms` still work).

**`app/agent/optimization.py`** — Four changes:
1. **`_phase_profiling` prompt** rewritten with 8 explicit steps: read code → determine Big O
   by tracing the algorithm → run actual timed `timeit`/`perf_counter` benchmark via `run_shell`
   (with scale_n guidance: 10k I/O, 100k CPU, 1M trivial) → measure memory → identify hotspots
   → rate readability_cost → judge prematurity → judge tech debt. New required JSON output schema
   includes all new fields.
2. **JSON validation** now also accepts `big_o_class` as a valid profiling output signal.
3. **`_build_subtask_description`** updated: "compute time is the most precious resource" doctrine,
   actual benchmark command template, required JSON schema for `record_benchmark` calls,
   prematurity check ("is this bottleneck real and measured, or assumed?"), readability
   honesty check ("clever code has a carrying cost").
4. **`_compare_benchmarks`** replaced with weighted multi-metric algorithm:
   - Computes `compute_imp` and `memory_imp` per-task (lower-is-better formula)
   - Weighted aggregate: `(compute_imp × 1.0 + memory_imp × 0.6) / (1.0 + 0.6)`
   - Falls back to `complexity_score` if neither duration nor memory data present
   - Adds Big O bonus: `rank_delta × BIG_O_BONUS_PCT` per rank improvement
   - Applies readability penalty: `weighted_imp × (1 - readability_cost × 0.5)`
   - Applies tech debt bonus: `+1.0%` if `tech_debt_resolved=true`
   - Doubles effective threshold if `is_premature=true`
   - Summary string includes score breakdown, Big O transitions, N subtasks
   - Graceful degradation: missing fields are skipped, no KeyError
5. **`_compare_reports` fallback** updated: applies Big O bonus even when falling back to
   profiling dicts, notes it in the summary string.

**`app/tests/test_optimization_subtasks.py`** — Added 8 new tests (14 → 22 total):
- `TestWeightedBenchmarkComparison` (6 tests): compute-weighted-over-memory, Big O bonus,
  readability penalty, premature multiplier, tech debt bonus, graceful degradation.
- `TestBigOFallback` (2 tests): Big O bonus in profiling-dict fallback, no-bonus when absent.

**Test count: 487 passing (was 479).**

### Prior Session: Fix test_intake_pipeline + E2E Test Expansion (COMPLETED)

- Fixed 16 failing `app/tests/test_intake_pipeline.py` tests: replaced
  `asyncio.get_event_loop().run_until_complete()` with `asyncio.run()` (Python 3.13 compat).
- Added `TestConceptualReviewPipelineE2E` (4), `TestFullReviewPipelineE2E` (4),
  `TestSchedulerFullChainE2E` (3) to `app/tests/test_e2e_pipeline.py`.
- Added `TestRunOptimizationSecurityTask` (4), `TestRunFullReviewTask` (5),
  `TestCheckCompletionRollupInline` (4) to `app/tests/test_scheduler_unit.py`.

---

## Next Steps

**P1 — Live integration verification of new benchmark framework**
- Run an actual task through the optimization stage. Confirm the profiling agent uses `run_shell`
  to benchmark, records all new metric fields via `record_benchmark`, and that `_compare_benchmarks`
  produces a weighted score in the improvement_summary. Check `optimization_benchmarks` table rows.
- No code change expected — this is observational.

**P2 — Surface benchmark data in the UI**
- The `optimization_benchmarks` table is populated but the board doesn't expose it. Add a
  "Benchmark Results" section to the task detail modal (or a new `GET /api/tasks/{id}/benchmarks`
  route + collapsible panel in the card). Would let humans see Big O transitions and weighted
  scores without querying the DB directly.

**P3 — Verify `record_benchmark` is reachable in INDEV_AGENT_TOOLS**
- `record_benchmark` appears in the `INDEV_AGENT_TOOLS` list. Confirm optimization sub-tasks
  (which run as `idea` → `indev` pipeline) actually have it in scope when the MaestroLoop runs.
  If not, it may need to be added to the optimization-specific tool set.

---

## File Structure

```
app/
├── main.py                  FastAPI app. lifespan context manager (not on_event).
│                            Routes: /api/tasks/{id}/research-jobs,
│                                    /api/research-jobs/{job_id}
├── logging_config.py        configure_logging(). RotatingFileHandler guarded against double-add.
├── database.py              SQLAlchemy models + all CRUD.
│                            Models: Task, LLM, Budget, TransitionVote, TransitionResult,
│                            BudgetEntry, SubdivisionRecord, PlanningResult, ComponentResult,
│                            OptimizationResult, SecurityReviewResult, FullReviewResult,
│                            MergeRecord, ResearchJob, OptimizationBenchmark, Project
├── agent/
│   ├── config.py            Single config interface. 19 sections (added optimization_weights).
│   ├── json_utils.py        extract_json_block(), parse_json_block().
│   ├── tools.py             27 safe tools. record_benchmark schema updated with new fields.
│   ├── system_prompt.py     MAESTRO_SYSTEM_PROMPT (ACCEPTED / REVERT / NEEDS_RESEARCH docs)
│   ├── loop.py              MaestroLoop. async _handle_tool_calls. NEEDS_RESEARCH handler.
│   ├── dag.py               DAGResolver — Kahn's sort, cycle detection.
│   ├── verdicts.py          Verdict enum, Vote, TallyResult, tally_votes().
│   ├── static_analysis.py   Tree-sitter deterministic code parser.
│   ├── intake.py            IntakePipeline (IDEA → PLANNING, 4-stage voting)
│   ├── planning.py          PlanningPipeline (5 stages, best-of-N)
│   ├── planning_gate.py     PlanningGate (7 checks, one optional LLM check)
│   ├── dev_orchestrator.py  DevOrchestrator (batch, parallel components)
│   ├── component_loop.py    ComponentLoop + ComponentToolDispatcher
│   ├── conceptual_review.py ConceptualReviewPipeline (4 det. + 4 LLM reviewers)
│   ├── optimization.py      OptimizationPipeline. Weighted multi-metric A/B framework.
│   │                        _compare_benchmarks: compute+memory weights, Big O bonus,
│   │                        readability penalty, premature multiplier, tech debt bonus.
│   │                        _phase_profiling: 8-step prompt, actual run_shell benchmarks.
│   │                        _build_subtask_description: compute-precious doctrine + full schema.
│   ├── security_review.py   SecurityPipeline (3 agents, veto power).
│   │                        run_security_pipeline() calls set_task_git_cwd() at entry.
│   ├── full_review.py       FullReviewPipeline (4 parallel reviewers, 3 if no frontend)
│   ├── merge.py             Deterministic git merge. Push retries with backoff.
│   ├── merge_conflict_resolver.py  LLM-assisted resolver
│   ├── research.py          Research agent (lives system)
│   ├── subdivide.py         SubdivisionAgent
│   ├── scheduler.py         Priority queue. Research job dispatch. DAG-aware ordering.
│   │                        Dispatchers: _run_conceptual_review_task,
│   │                        _run_optimization_security_task, _run_full_review_task.
│   │                        _record_demotion_inline(), _check_completion_rollup_inline().
│   ├── llm_client.py        Centralized HTTP client. Enforces budget_id + llm_id.
│   └── mock_llm.py          Dictionary-based mock LLM (PatternRule + scenarios)
├── migrations/
│   ├── runner.py            Standalone sqlite3 migration engine
│   └── versions/
│       ├── 0001–0017        Full schema history (projects table = 0017)
│       └── 0018             research_jobs + optimization_benchmarks tables
├── models/
│   └── dags.py              TaskDAG, TaskNode
├── services/
│   └── repl.py              CheckpointManager.
├── tests/
│   ├── conftest.py
│   ├── test_config.py
│   ├── test_integration.py
│   ├── test_repl.py
│   ├── test_subdivision.py
│   ├── test_pipeline_routing.py
│   ├── test_json_utils.py           13 tests
│   ├── test_dag_resolver.py         12 tests
│   ├── test_merge_pipeline.py       13 tests
│   ├── test_verdicts.py             21 tests
│   ├── test_tools_safety.py         17 tests
│   ├── test_static_analysis.py      12 tests
│   ├── test_intake_pipeline.py      17 tests  ← all passing (fixed 2026-03-22)
│   ├── test_planning_unit.py        16 tests (PlanningGate all 7 checks)
│   ├── test_security_review_unit.py 14 tests (allowlist + pipeline verdicts)
│   ├── test_llm_client.py           13 tests (HTTP layer, budget logging)
│   ├── test_e2e_pipeline.py         20 tests (9 original + 11 new)
│   ├── test_scheduler_unit.py       ~50 tests (existing + 13 new)
│   ├── test_research_jobs.py        12 tests (CRUD + API routes)
│   └── test_optimization_subtasks.py 22 tests (benchmarks + weighted comparison + Big O fallback)
└── web/
    ├── index.html           Board UI
    ├── kanban.js            All frontend behaviour. viewResearchJobs() + Research Jobs button.
    └── style.css            All styles
data/
└── kanban.db                SQLite (18 migrations applied)
logs/
└── maestro.log              Rotating log file
.maestro/
└── task_dag.json            Legacy REPL state
maestro.ini                  Master config (optimization_weights section added)
```

---

## The 9-Stage Pipeline

```
IDEA → [intake] → PLANNING → [planning + gate] → INDEV → [dev_orchestrator]
     → CONCEPTUAL_REVIEW → [conceptual_review] → OPTIMIZATION → [optimization]
     → SECURITY → [security_review] → FULL_REVIEW → [full_review] → COMPLETED
```

Optimization Phase 4 creates child `idea` tasks that re-enter at IDEA and flow through
the full pipeline independently. The parent task polls for their completion.

### Advance Handlers (`ADVANCE_HANDLERS` in `main.py`)

| Column              | Handler                      | Trigger  |
|---------------------|------------------------------|----------|
| `idea`              | `_run_intake_pipeline`       | Auto (scheduler) |
| `planning`          | `_run_planning_pipeline_bg`  | Auto (scheduler + manual) |
| `indev`             | `_run_dev_orchestrator_bg`   | Auto (scheduler) |
| `conceptual_review` | `_advance_to_optimization`   | Auto (scheduler) + manual |
| `optimization`      | `_run_security_pipeline_bg`  | Auto (scheduler) + manual |
| `security`          | `_run_full_review_bg`        | Auto (scheduler, transient) + manual |
| `full_review`       | `_execute_merge_bg`          | Auto (scheduler) + manual |

### Scheduler Dispatch

```ini
dispatchable_types = idea, planning, indev, conceptual_review, optimization, full_review
```

`security` is intentionally absent — the scheduler's `_run_optimization_security_task`
advances through `security` atomically within the same thread before stopping at `full_review`.

---

## Intake Pipeline — IDEA → PLANNING Gate

Four stages; tally rules fire in priority order:

| Stage | Type | Runs |
|-------|------|------|
| 1. Scope Analysis | LLM | Always first |
| 2a. Static Analysis | Tree-sitter (deterministic) | Parallel with Stage 3 |
| 3. Conflict Detection | LLM | Parallel with Stage 2a |
| 2b. Feasibility Analysis | LLM | After Stage 2a completes |

**Tally rules (in order):**
0. Any `SUBDIVIDE_IDEA` → subdivide (spawn SubdivisionAgent)
1. Any `REJECTED` → rejected
2. Majority `NOT_SUITABLE` → rejected
3. Any `NEEDS_RESEARCH` → spawn ResearchAgent per flagged stage
4. Equal pass/fail split → spawn tie-breaker ResearchAgent
5. Default → passed

---

## Tool System (27 tools)

### Sandboxing model

- **Path containment** — `_assert_safe_path()` resolves symlinks then checks `startswith(effective_root)`.
- **`.git` hard rejection** — `_assert_archivable()` blocks `.git` at the tool layer.
- **Soft-delete only** — `archive_file` moves to `.archive/<timestamp>/`. Hard deletion impossible.
- **Shell blocklist** — 19 regex patterns. Note: `wget ... | sh` is NOT blocked (known gap).
- **Git branch allowlist** — only `maestro/task-*` + `main`/`master`.

### Tool: `record_benchmark`
- Params: `task_id`, `parent_task_id`, `benchmark_type` (`before`|`after`), `metrics` (JSON string)
- Required metric keys: `test_duration_ms`, `memory_peak_mb`, `complexity_score`
- New recommended keys: `big_o_class`, `scale_n`, `readability_cost`, `is_premature`, `tech_debt_resolved`, `notes`
- Writes to `optimization_benchmarks` table
- Available in `INDEV_AGENT_TOOLS` (and thus in MaestroLoop's tool schema)
- Phase 5 `_compare_benchmarks()` reads this table for weighted multi-metric A/B comparison

---

## Test Suite

**487 tests total, all passing.**

```bash
venv/Scripts/python.exe -m pytest app/tests/ -v
venv/Scripts/python.exe -m pytest app/tests/test_optimization_subtasks.py -v
venv/Scripts/python.exe -m pytest app/tests/test_e2e_pipeline.py -v
venv/Scripts/python.exe -m pytest app/tests/test_scheduler_unit.py -v
```

Key patching patterns:
- **Intake tests**: patch `app.agent.intake.call_llm` directly
- **llm_client tests**: patch `httpx.AsyncClient` with `_make_mock_client(post_response)`
- **e2e tests**: patch `httpx.AsyncClient` with `_mock_client_cls(mock_llm)` which wires `mock_llm.handle_post`
- **Sync test methods calling async**: use `asyncio.run(coro)` — NOT `get_event_loop().run_until_complete()`
- **async pipeline mocks**: Python 3.8+ `patch` auto-creates `AsyncMock` for `async def` targets
- **`_check_completion_rollup_inline`**: uses `import app.database as db` inline → patch
  `app.database.get_task` etc. directly (NOT `app.agent.scheduler.db`).

---

## Running Locally

```bash
# Server
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

# Database
migrate.bat status
migrate.bat migrate
migrate.bat reset      # destructive

# Dependencies
venv/Scripts/pip.exe install -e .
```

Board: `http://localhost:8000`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B)

---

## Key Design Decisions

- **Single config interface** — `config.py` is the only import for tuneable values.
- **Logging wired before imports** — `configure_logging()` is called at the top of `main.py`.
- **Per-task git CWD via ContextVar** — `_task_git_cwd` in `tools.py`. Not inherited across thread
  boundaries. All 6 pipeline entry points now call `set_task_git_cwd(project_path)` at entry.
- **push_failure is a first-class merge status** — Failed push returns `MergeResult(status="push_failure")`.
- **Verdict enum is canonical** — `intake.py` derives string constants from `Verdict.XXX.value`.
- **Soft-delete everywhere** — `archive_file` is the only deletion primitive.
- **Agent branches isolated** — every MaestroLoop run creates `maestro/task-{id}`.
- **`call_llm` enforces `budget_id`** — raises `ValueError` if `budget_id is None`.
- **NEEDS_RESEARCH is non-terminal** — loop continues after research. ACCEPTED and REVERT_TO_DESIGN
  are terminal. The signal is recognized in `_extract_signal()`.
- **Optimization sub-tasks inherit parent's prereqs, not parent's ID** — deadlock prevention.
- **Research jobs as first-class DB rows** — both inline (NEEDS_RESEARCH signal) and queued
  (scheduler background) research requests write to `research_jobs`.
- **Scheduler priority formula** — `depth * DEPTH_PENALTY + column_order * 100 + position`.
  Shallower DAG nodes and earlier pipeline stages dispatch first.
- **`idea` tasks now in default dispatchable types** — scheduler auto-advances ideas through
  intake if they have description + llm_id + budget_id set.
- **`_handle_tool_calls` is now async** — prerequisite for `spawn_research_agent` working in MaestroLoop.
- **`security` excluded from scheduler dispatchable_types** — it's transient; the scheduler's
  `_run_optimization_security_task` transitions through `security` atomically.
- **Scheduler-local `_record_demotion_inline()`** — avoids circular import from main.py. Mirrors
  `_record_demotion()` in main.py exactly. Keep them in sync if demotion schema changes.
- **Scheduler-local `_check_completion_rollup_inline()`** — same rationale. Recursively walks
  parent chain. Mirrors `main._check_completion_rollup`. Keep in sync if rollup logic changes.
- **RotatingFileHandler guarded** — `configure_logging()` checks for existing instance before
  adding, matching the StreamHandler guard. Both handlers are now idempotent on repeated calls.
- **Research Jobs button scoped** — shown on all statuses except `idea`, `subdividing`,
  `architecture`. Research only fires after intake, so idea/subdividing have no jobs to show.
- **Weighted multi-metric optimization comparison** — `_compare_benchmarks` uses compute/memory
  weights (1.0/0.6), Big O rank-improvement bonus (10% per rank), readability penalty (up to 50%
  reduction), premature multiplier (2×), tech debt bonus (1%). Compute time is the most precious
  resource — weighted highest. Gracefully degrades to single-metric if new fields absent.
- **Big O bonus applies in profiling fallback too** — `_compare_reports` applies Big O rank bonus
  even when falling back to complexity_score dicts, if both baseline and post have `big_o_class`.
- **lifespan over on_event** — FastAPI lifespan context manager replaces deprecated
  `@app.on_event` decorators.
- **ConceptualReview D1–D4 are all synchronous** — `_EMPTY_PLAN` makes all 4 trivially pass.
- **FullReviewPipeline reviewer count is conditional** — 3 reviewers always; UX reviewer added
  only if `_has_frontend_changes()` returns True.
- **`asyncio.run()` in sync tests** — correct pattern for Python 3.12+.
