# Project Maestro — Living Summary

## What this is

A Kanban board with an agentic LLM orchestration backend. The board is the UI face of a
"Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test →
Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition through a 9-stage
pipeline (IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY →
FULL_REVIEW → COMPLETED), gated by multi-stage intake voting. A horizontal Architecture
Bar above the pipeline columns holds architectural constraints injected into all agent
prompts. 52 migrations applied. 690 tests passing.

---

## Recent work (this session)

### Planning pipeline stuck-in-planning diagnosis and fixes

**Context:** Three tasks were stuck in `planning` type across two projects (AndroidStreetPass
and Garden). The scheduler was running and dispatching them, but none advanced to INDEV.

**Bug 1 — Design judge truncated by `max_tokens=256` (FIXED)**

The design judge call in `app/agent/planning.py` had `max_tokens=256` hardcoded. Reasoning
models (Qwen3.5) spend all 256 tokens on chain-of-thought (`reasoning_content`) and produce
empty `content`. `finish_reason: "length"` appeared in every judge budget entry. The judge
silently fell back to `designs[0]` via the `except Exception` handler.

**Fix:** Added `PLANNING_JUDGE_MAX_TOKENS` to `app/agent/config.py` (default 8192), wired
it into the `call_llm` call in `planning.py`. Set `maestro.ini [planning] judge_max_tokens = 8192`.

**Bug 2 — Design review panel starved by parallel LLM calls (FIXED)**

5 reviewer calls were fired via `asyncio.gather`. With 3 concurrent planning sessions ×
5 reviewers = 15 simultaneous LLM requests. The per-call `total_timeout_secs=90` caused
most reviewers to time out, returning `NEEDS_RESEARCH` votes. `tally_votes` Rule 3 ("any
NEEDS_RESEARCH → needs_research outcome") blocked advancement even when 3/5 reviewers
succeeded legitimately.

**Fix:** Reviewer calls now run **sequentially** in a for-loop instead of `asyncio.gather`.
The `total_timeout_secs=90` parameter was removed. Each call gets a clean LLM slot with no
contention; total review time is similar (calls were queuing internally anyway).

**Bug 3 — PlanningCorrectionAgent never triggered (IN PROGRESS)**

`PlanningCorrectionAgent` was implemented by the prior Claude session (`planning_correction.py`,
migration 0052, `update_plan_fields` tool, scheduler integration at `scheduler.py:3089`).
Zero `planning_correction` sessions have ever run. Root cause: all prior planning sessions
for the stuck tasks ended in `shutdown` (server restarts) before completing the full
pipeline → gate → correction path. Current sessions (started 07:13 UTC) are the **first**
to run with the correction agent wired in. They need to reach the gate to test it.

The gate fails with `interface_completeness` hard fail: designs include intra-file type refs
(e.g., `BlePacketType`, `PacketPayload`) in `interface_contracts.consumes` with no matching
`provides`. The correction agent prompt already explains this pattern and instructs the agent
to remove them from `consumes`.

**Bug 4 — Design review content failures (OPEN)**

- *SQL Migration:* LLM designs a "simplified" migration removing `password_hash`, `is_active`,
  `last_login_at`. Security reviewer correctly REJECTs this. After 3 retries the pipeline
  proceeds anyway with the bad design, hits the gate, and the correction agent should fix
  `interface_completeness`.
- *Create Supporting Types:* Interface reviewer gives NEEDS_RESEARCH because `PacketMetadata`
  is already in the codebase but the design still references it as a new file. After 3 retries
  the pipeline proceeds.

Both are task-description clarity issues, not code bugs.

### `scripts/inspect_cards.py` — major expansion (DONE)

Added new diagnostic sections:
- `scheduler` — simulates scheduler state (READY/BLOCKED/PARENT_SKIP/DONE_SKIPPED/STUCK_SUBDIVIDING)
- `activity` — recent LLM activity timeline with `--hours` flag
- `votes` — transition vote detail with `--task` filter
- `budget` — LLM capacity and spending summary
- `children` — parent→child tree with activity counts

### `CLAUDE.md` — planning pipeline diagnostic playbook added (DONE)

Added a "Diagnosing cards stuck in PLANNING" section with step-by-step DB queries,
signal interpretation tables, and a common root causes table.

---

## Files changed this session

| File | Change |
|---|---|
| `app/agent/config.py` | `PLANNING_JUDGE_MAX_TOKENS` (default 8192), `CORRECTION_MAX_TURNS`, `CORRECTION_SKIP_AFTER_FAILURES` |
| `app/agent/planning.py` | Judge call: hardcoded 256 → `PLANNING_JUDGE_MAX_TOKENS`; reviewers `asyncio.gather` → sequential for-loop |
| `app/agent/planning_correction.py` | **New** — `PlanningCorrectionAgent` class (prior session, untracked) |
| `app/agent/scheduler.py` | Correction agent integration: `_run_planning_correction()` helper, trigger at gate failure |
| `app/agent/tools.py` | `update_plan_fields()` tool + schema + `CORRECTION_AGENT_TOOLS` list |
| `app/migrations/versions/0052_planning_correction_tracking.py` | **New** — adds `correction_attempts` column to `planning_results` |
| `maestro.ini` | `[planning] judge_max_tokens = 8192` |
| `scripts/inspect_cards.py` | Major expansion: scheduler/activity/votes/budget/children sections |
| `CLAUDE.md` | Planning pipeline diagnostic playbook |

---

## Open / next steps

**P0 — Verify correction agent triggers end-to-end**

The current planning sessions (3 tasks) need to complete their design cycles and hit the
gate. After the gate fails with `interface_completeness`, verify `planning_correction` rows
appear in `agent_sessions`. If not, debug `scheduler.py:3089` trigger condition.

```bash
venv/Scripts/python.exe -c "
import sqlite3
conn = sqlite3.connect('data/kanban.db')
print(conn.execute(\"SELECT task_id, exit_reason, exit_summary, started_at FROM agent_sessions WHERE agent_type='planning_correction' ORDER BY id DESC LIMIT 5\").fetchall())
"
```

**P0 — Fix SQL Migration task description**

The LLM keeps proposing to strip security columns because the task description doesn't say
to preserve them. Edit via board UI: add "The existing migration at
`migrations/001_create_users_table.sql` is authoritative; preserve all existing columns and
extend if needed."

**P0 — Fix Create Supporting Types task description**

`PacketMetadata.kt` already exists at
`core/models/src/main/java/com/androidstreetpass/core/models/PacketMetadata.kt`. Task
description should say so, limiting scope to PacketPayload only.

**P1 — If correction agent stalls: soften `interface_completeness` gate check**

`app/agent/planning_gate.py:165` — change `hard_fail=True` to `hard_fail=False` for
`interface_completeness`. This demotes it from a blocking gate failure to a warning. The
correction agent then has a chance to patch it before the next planning cycle rather than
requiring an inline correction under time pressure.

**P1 — Call `supersede_planning_results` at start of each planning run**

`app/database/crud_pipeline.py:225` defines `supersede_planning_results(task_id)` but it's
never called. Add a call in `_run_planning_task` in `scheduler.py` (around line 2988, before
`run_planning_pipeline(...)`). Prevents zombie `status='active'` rows accumulating.

**P2 — Populate deduplication (arch bar)**

Before creating `arch_gen_jobs`, check for already-pending/running jobs for the same
category (filter `status IN ('pending', 'running')`). See old PLAN.md for code snippet.

**P2 — Populate prewarm gate**

Return HTTP 409 if no file summaries exist for the project path. See old PLAN.md for
code snippet.

---

## 9-stage pipeline

```
IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED
```

Special types: `architecture` (arch bar only), `subdividing` (Big Idea mid-subdivision).

### PLANNING stage detail

`_run_planning_task` in `scheduler.py`:
1. `run_planning_pipeline` — survey → best-of-N designs → sequential design review panel →
   pitfall detection → plan consolidation → store to `planning_results`
2. `run_planning_gate` — deterministic + LLM gate checks (interface_completeness,
   file_safety, context_budget, feasibility_recheck)
3. On gate pass → `update_task(type='indev')`
4. On gate fail → if hard failures + correction attempts < cap: `_run_planning_correction()`
   (PlanningCorrectionAgent patches `interface_contracts` via `update_plan_fields` tool) →
   re-run gate → if now passes, advance to INDEV
5. After correction (pass or fail) → apply 5-min rejection cooldown

---

## Scheduler job types and priorities

| Type | Priority | Notes |
|---|---|---|
| `FileSummaryJob` | -1.0 | Highest — callers block on completion event |
| `ResearchJob` | 0.0 | Background investigations |
| `ArchGenJob` | 1.0 | Fire-and-forget arch card generation |
| DAG tasks | computed | Based on pipeline stage + position |

All jobs respect: per-LLM `parallel_sessions` cap, per-node `max_parallel_sessions` cap,
5-min retry cooldown on failure, orphan rescue on restart.

---

## File structure (key files)

```
app/
  main.py                         FastAPI app, all routes
  agent/
    arch_gen_agent.py             Arch card generation from file summaries
    config.py                     INI-driven constants (incl. PLANNING_JUDGE_MAX_TOKENS)
    dag.py                        DAGResolver (Kahn's topo sort)
    file_summary_agent.py         File summary generation agent
    intake.py                     IDEA→PLANNING pipeline
    llm_client.py                 Centralised LLM HTTP client
    loop.py                       MaestroLoop (Design→Implement→Test→Verify)
    planning.py                   Planning pipeline (survey→design→review→consolidate)
    planning_correction.py        PlanningCorrectionAgent (patches failing plan fields)
    planning_gate.py              Gate checks (interface_completeness, file_safety, etc.)
    conceptual_review.py / security_review.py / full_review.py / optimization.py
    pip_agent.py / pip_resolution.py  PIP generation and resolution
    project_snapshot.py           build_project_snapshot, build_architecture_context
    research.py                   Research agent (lives system)
    scheduler.py                  Push-first eager scheduler
    subdivide.py                  Subdivision agent
    survey_orchestrator.py        Hierarchical project summarization
    tools.py                      Agent tools (incl. update_plan_fields)
    verdicts.py                   Vote tally logic
  database/
    __init__.py                   Re-exports everything
    models.py                     All SQLAlchemy models
    crud_tasks.py / crud_projects.py / crud_infra.py / crud_costs.py
    crud_pipeline.py              Planning results, gate checks, correction_attempts
    crud_jobs.py / crud_files.py / crud_inbox.py
    session.py                    Engine, SessionLocal, Base
  migrations/
    runner.py                     Standalone sqlite3 migration engine
    versions/0001–0052            52 applied migrations
  web/
    index.html                    Board shell
    kanban.js                     All board behaviour
    style.css                     Board styles
    diagnostics.html + diag-*.js  LLM conversation viewer
data/
  kanban.db                       SQLite database
maestro.ini                       Runtime configuration
scripts/
  inspect_cards.py                Multi-section diagnostic tool
```

---

## Running locally

```bash
# Server
venv/Scripts/python.exe -m uvicorn app.main:app --port 8000

# Tests (690 passing)
venv/Scripts/python.exe -m pytest app/tests/ -v

# Migrations
migrate.bat status
migrate.bat migrate

# Diagnostics
venv/Scripts/python.exe scripts/inspect_cards.py scheduler
venv/Scripts/python.exe scripts/inspect_cards.py activity --hours 4
venv/Scripts/python.exe scripts/inspect_cards.py votes --task <id>
```

---

## Key design decisions

- **Planning reviewers run sequentially, not in parallel.** Parallel `asyncio.gather` with
  90s timeout caused starvation under concurrent planning sessions (15 simultaneous LLM
  requests), producing spurious NEEDS_RESEARCH votes that blocked the tally. Sequential
  execution is slightly slower per-review but eliminates false failures entirely.

- **`PLANNING_JUDGE_MAX_TOKENS = 8192` (not `MAX_TOKENS_PER_TURN`).** The judge needs more
  headroom than 256 for reasoning models but doesn't need 32k. 8192 covers Qwen3.5's
  chain-of-thought for typical 3-5 design comparison tasks.

- **Correction agent runs inline in the planning session thread.** The `_run_planning_correction`
  call is synchronous within `_run_planning_task`. No new scheduler slot is consumed — the
  correction happens as part of the same `agent_sessions` row lifecycle (separate row
  created with `agent_type='planning_correction'`).

- **`interface_completeness` remains a hard gate failure.** It catches designs that reference
  types across tasks without explicit contracts. The correction agent (not a gate softening)
  is the intended fix path for the common false-positive case of intra-file refs.
