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

### Graceful shutdown on Ctrl-C (`llm_client.py`, `research.py`, `main.py`)

**Problem:** Pressing Ctrl-C flooded the terminal with hundreds of
`RuntimeError: cannot schedule new futures after interpreter shutdown` errors
before the process died, continuing for several seconds.

**Root cause:** Python's interpreter teardown shuts down all `ThreadPoolExecutor`
instances before daemon threads finish. Agent threads (research agents, up to 3 lives ×
50 turns each) were still alive and retrying LLM calls. The research agent's `_run_life`
caught *every* exception with a blanket `except Exception`, logged ERROR, appended a
system message, and **`continue`d** to the next turn — a tight spin of ~12 errors/second
per agent.

**Fix (three-part):**
- `llm_client.py` — Added `_shutdown_event = threading.Event()` with `signal_shutdown()`
  and `is_shutting_down()` helpers. The `call_llm` retry loop checks the flag at the top
  of every iteration and raises `RuntimeError("Server is shutting down")` immediately.
- `research.py` — Added `if is_shutting_down(): raise` before the `continue` in
  `_run_life`'s except block, and in `_post_mortem_call` and `_forced_verdict_call`.
  Shutdown exceptions now propagate instead of being swallowed.
- `main.py` — Lifespan shutdown calls `signal_shutdown()` **before** `stop_scheduler()`,
  arming the flag while threads are still alive.

---

### llama.cpp 500 errors — PEG parser failure (`research.py`)

**Problem:** The llama.cpp server at port 21982 returned intermittent
`{"error": {"code": 500, "message": "Failed to parse input at pos ~1100-1135"}}`
during normal operation.

**Root cause:** The server runs Qwen3 with `chat_format: peg-native`. Qwen3 in thinking
mode opens a `<think>...</think>` scratchpad before producing visible output. The PEG
parser processes the raw token stream *after* generation and expects a properly closed
`</think>`. When thinking consumes the full token budget before `</think>` is written, the
parser hits end-of-stream mid-thought and fails at the character position where truncation
occurred (~4.4 chars/token × token limit ≈ 1100–1135 chars).

The GBNF grammar constraint (`grammar=_FORCED_VERDICT_GRAMMAR`) does **not** suppress
Qwen3 thinking — it constrains only the visible output *after* `</think>`. The model still
thinks first, potentially exhausting the budget before the JSON is ever reached.

**Fix:**
- Added `/no_think` at the start of the forced verdict epilogue system prompt. Qwen3
  recognises this and skips the thinking block, producing short grammar-constrained JSON
  that fits within any reasonable token budget.
- Raised `max_tokens` for the forced verdict call 512 → 4096 so that once the underlying
  token budget issue is resolved, the model has adequate headroom to reason before
  synthesising its verdict.

**Open:** Two hardcoded `max_tokens=256` calls were found in `planning.py` (planning
judge) and `arch_gen_agent.py` (arch card generation). Both will fail identically with
Qwen3 thinking mode — thinking consumes the full 256-token budget before the JSON output
is reached. These need either `/no_think` in their system prompts or a raised budget.
The root source of the 256-token cap on regular research agent calls (which send
`max_tokens=4096`) remains unconfirmed; `n_predict=-1` on the llama.cpp server rules out
a server-level cap. The proxy at port 8008 and Qwen3's chat template `thinking_budget`
default are the remaining suspects.

---

### Thundering herd under concurrent jobs (`llm_client.py`)

**Problem:** Running 3 simultaneous tasks produced noticeably more 500 errors than a
single task — bursts of two or three 500s at the same timestamp, brief recovery, then
another burst.

**Two compounding effects:**

**Statistical pile-up.** Each LLM call has some probability *P* of hitting the Qwen3
thinking truncation condition. With N concurrent callers the probability that *at least
one* fails in a given window is `1 − (1−P)^N`. At N=3 this is ~2.7× the single-caller
failure rate — more errors simply because more calls happen in parallel.

**Thundering herd amplifier.** The backoff state (`_endpoint_states`) is a global dict
shared across all threads. When all three agents receive 500s at the same timestamp:
1. Each increments `fail_count` and computes `wait = BACKOFF_BASE_DELAY = 3.0s`.
2. Each calls `await asyncio.sleep(3.0)` — all three sleep for *exactly* the same
   duration.
3. All three wake up simultaneously and fire their next request together.
4. The server receives another burst of 3 concurrent requests; the same ratio of
   successes to failures repeats, producing another burst of errors.

This is the textbook thundering herd: synchronised failure → synchronised retry →
synchronised failure again.

**Fix:** Added `random.uniform(0, wait * 0.5)` jitter to both retry sleep sites
(ConnectError handler and 500 handler). For the 3-second base wait, agents now sleep
3.0–4.5s independently. Three concurrent failures spread their retries across a 1.5s
window, each hitting the server individually with a fresh slot assignment.

---

### Arch Bar Populate feature (previous session, complete)

Added a `⚡ Populate` button to the architecture bar header. When clicked it queues
scheduler jobs to generate one architecture card per missing category, using existing
file summaries as context. No existing cards are modified.

**Migration 0036** — `arch_gen_jobs` table:
- `project`, `category`, `llm_id`, `budget_id`, `status`, `priority` (1.0), token counts,
  `error_message`, `created_at`, `completed_at`
- Index on `(status, priority, created_at)` for fast dispatch

**`app/agent/arch_gen_agent.py`** (new) — single-call agent: fetches file summaries,
builds prompt (relative path + 2 sentences each), calls LLM with `temperature=0.4`,
`max_tokens=256`, creates architecture task.

**`app/agent/scheduler.py`** — `_dispatch_arch_gen_jobs()`, `_run_arch_gen_job()`, rescue
block in `_rescue_stale_jobs()`, and `_tick()` step 5.5.

**`app/main.py`** — `POST /api/projects/{project_name}/populate-arch`.

**`app/web/`** — `⚡ Populate` button in arch bar header; `populateArchBar()` in
`kanban.js`.

---

## Files changed this session

| File | Change |
|---|---|
| `app/agent/llm_client.py` | Shutdown flag + helpers; shutdown check in retry loop; jitter on both sleep sites; `import random` |
| `app/agent/research.py` | Import `is_shutting_down`; re-raise on shutdown in `_run_life`, `_post_mortem_call`, `_forced_verdict_call`; `/no_think` in forced verdict system prompt; `max_tokens` 512 → 4096 |
| `app/main.py` | `signal_shutdown()` before `stop_scheduler()` in lifespan shutdown |

---

## Open questions / next steps

- **`planning.py` and `arch_gen_agent.py` hardcoded `max_tokens=256`** — will fail with
  Qwen3 thinking mode. Add `/no_think` to those system prompts or raise budget.
- **Root cause of 256-token cap on research agent calls** — proxy at port 8008 config
  or Qwen3 `thinking_budget` default. Check proxy configuration.
- **Populate deduplication** — clicking Populate twice creates duplicate arch_gen_jobs.
  Add a pending-job check before creating.
- **Populate prewarm gate** — if no file summaries exist, jobs silently fail. Return 409
  with a helpful message.
- **Populate progress indicator** — arch bar subtitle showing "Generating N categories…"
  while jobs are pending/running.

---

## 9-stage pipeline

```
IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED
```

Special types: `architecture` (arch bar only, never dispatched), `subdividing` (Big Idea
mid-subdivision).

---

## Scheduler job types and priorities

| Type | Priority | Notes |
|---|---|---|
| `FileSummaryJob` | -1.0 | Highest — callers block on completion event |
| `ResearchJob` | 0.0 | Background investigations |
| `ArchGenJob` | 1.0 | Fire-and-forget arch card generation |
| DAG tasks | computed | Based on pipeline stage + position |

All jobs respect: one-LLM-at-a-time policy, per-LLM `parallel_sessions` cap,
per-node `max_loaded_models` + `max_parallel_sessions` caps, 5-min retry cooldown on
failure, orphan rescue on restart.

---

## File structure (key files)

```
app/
  main.py                    FastAPI app, all routes
  agent/
    arch_gen_agent.py        Arch card generation from file summaries
    config.py                INI-driven constants
    dag.py                   DAGResolver (Kahn's topo sort)
    file_summary_agent.py    File summary generation agent
    intake.py                IDEA→PLANNING pipeline
    llm_client.py            Centralised LLM HTTP client (shutdown flag, jitter)
    loop.py                  MaestroLoop (Design→Implement→Test→Verify)
    planning.py / planning_gate.py
    conceptual_review.py / security_review.py / full_review.py / optimization.py
    project_snapshot.py      build_project_snapshot, build_architecture_context
    research.py              Research agent (lives system, shutdown-aware)
    scheduler.py             Push-first eager scheduler (tick loop)
    subdivide.py             Subdivision agent
    tools.py                 Agent tool implementations
    verdicts.py              Vote tally logic
  database/
    __init__.py              Re-exports everything
    models.py                All SQLAlchemy models (incl. ArchGenJob)
    crud_tasks.py / crud_projects.py / crud_infra.py / crud_costs.py
    crud_pipeline.py / crud_jobs.py / crud_files.py / crud_inbox.py
    session.py               Engine, SessionLocal, Base
  migrations/
    runner.py                Standalone sqlite3 migration engine
    versions/0001–0036       36 applied migrations
  web/
    index.html               Board shell
    kanban.js                All board behaviour
    style.css                Board styles
    diagnostics.html + diag-*.js   LLM conversation viewer
data/
  kanban.db                  SQLite database
maestro.ini                  Runtime configuration
```

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
