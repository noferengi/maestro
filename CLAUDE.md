# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Project Maestro — a Kanban board with an agentic LLM orchestration backend. The board is the UI face of a "Wiggum Loop": a Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED. Tasks transition IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED, gated by a multi-stage intake pipeline with LLM voting.

## MCP server — primary diagnostic interface

A `maestro` MCP server is registered in `.mcp.json` and enabled via `.claude/settings.local.json`.
**Prefer MCP tools over raw SQL queries or Bash scripts for all diagnostic and admin tasks.**

Run `/mcp` to confirm the server is connected (should show `maestro  connected` with tool list).
If disconnected, restart Claude Code.

### Default monitoring behavior

When asked to watch, monitor, or babysit Maestro, the default workflow is `/loop` with `monitor()`:

```
/loop
```

Each iteration calls `monitor()` with no arguments, blocks for the window defined in
`maestro.ini [monitor] duration_seconds` (default 5 minutes), then returns a structured
report. Review the report, take any corrective actions using the action tools below, then
the loop fires again automatically.

To run a single 5-minute monitoring window without looping:

```
mcp__maestro__monitor(duration_seconds=300)
```

The report includes new budget entries, session starts/completions, stage changes, and
five pattern flags: `rapid_cycling`, `token_limited`, `zombie_sessions`, `stage_thrash`,
`tool_call_storms`. When a flag fires, drill in with `diagnose_task` or `get_budget_entry_full`.

### When to use which tool

| Goal | Tool |
|---|---|
| Watch activity over time | `monitor()` — blocks N seconds, returns diff report + pattern flags |
| Why is task X stuck? | `diagnose_task(task_id)` — one call, complete picture |
| What's running right now? | `get_scheduler_state()` (DB) + `get_scheduler_api_status()` (live API) |
| Find tasks with no recent LLM activity | `find_stuck_tasks(idle_minutes=10)` |
| Inspect raw LLM call history | `get_budget_trace(task_id, n=20)` |
| Read full prompt/response for one LLM call | `get_budget_entry_full(entry_id)` |
| Check planning gate failure history | `get_gate_history(task_id)` |
| See full plan content (interface_contracts etc.) | `get_planning_result(task_id)` |
| List tasks by project or type | `list_tasks(project="Garden", type="planning")` |
| Add scope note to task description | `append_task_description(task_id, text)` |
| Fix interface_contracts / file_manifest in a plan | `patch_planning_fields(result_id, fields_dict)` |
| Force a task to a pipeline stage (no demotion record) | `set_task_type(task_id, "planning")` |
| Move task backward with demotion record | `demote_task(task_id, target_stage?)` |
| Trigger planning pipeline manually | `trigger_planning_run(task_id)` |
| Trigger review / security / full_review | `run_pipeline_stage(task_id, stage)` |
| Stop a running MaestroLoop | `stop_agent(task_id)` |
| Restart the Maestro server | `restart_server()` — drains sessions, waits ~60 s |
| Anything not covered above | `run_inspect_cards(section, extra_args)` |

### Key signal from `diagnose_task`

- `activity_status: "active — last LLM call at ..."` → session running normally
- `activity_status: "active — no budget entries yet"` → in survey phase or waiting for LLM slot
- `activity_status: "idle"` → session is a zombie (server restart); task needs re-dispatch
- `budget_trace[0].finish_reason == "length"` + empty `content_preview` → max_tokens too low for reasoning model
- `correction_sessions` present → PlanningCorrectionAgent has run; check `exit_reason`
- `planning.correction_attempts > 0` → gate has failed and correction was attempted

### Source files

```
mcp_server.py          ← entry point, tool registration
mcp_tools/
  helpers.py           ← DB connection, response field extraction
  diagnostics.py       ← all read-only tools
  actions.py           ← write/admin tools
  monitor.py           ← blocking monitor tool + pattern detectors
.mcp.json              ← server registration (picked up by Claude Code)
```

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

**To restart a running server** — use the MCP tool, not a shell command:

```
mcp__maestro__restart_server()
```

Wait ~60 seconds after triggering. The server drains active sessions before exiting; the
`Launcher.ps1` process detects `restart.flag` and relaunches uvicorn automatically.
Do **not** use `pkill` or `Bash` to kill the process — that bypasses session drain.

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

Migrations `0001–0042` live in `app/migrations/versions/`. Current highest: `0042` (`pip_resolution_jobs`). See `CLAUDE_SCHEMA.md` for the full schema.

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

### Diagnosing cards stuck in PLANNING

When tasks are in `planning` type but not advancing, run through this checklist:

**Step 1 — confirm scheduler is running and tasks are active:**
```bash
curl -s http://localhost:8000/api/scheduler/status | python -m json.tool
```
Look for `active_sessions` containing the stuck task IDs.

**Step 2 — check recent budget entries for those tasks:**
```python
# Quick DB query (run via venv/Scripts/python.exe -c "...")
import sqlite3, json
conn = sqlite3.connect('data/kanban.db')
rows = conn.execute('''
    SELECT id, agent_name, prompt_cost, generation_cost,
           substr(response_data, 1, 200), created_at
    FROM budget_entries WHERE task_id=? ORDER BY id DESC LIMIT 5
''', ('task-id-here',)).fetchall()
for r in rows: print(r)
```
Key signals:
- `finish_reason: "length"` with empty `content` and non-empty `reasoning_content` → LLM hit `max_tokens` during chain-of-thought; increase the relevant `*_MAX_TOKENS` config value
- Rapid small calls (< 700 prompt_cost) cycling every 30s → gate checks looping on a failure
- No entries for > 10 minutes despite active session → pipeline waiting for LLM slot behind other tasks (check LLM capacity)

**Step 3 — check planning results and gate history:**
```python
conn.execute('SELECT id, status, gate_checks, correction_attempts, created_at FROM planning_results WHERE task_id=? ORDER BY id DESC LIMIT 3', (task_id,)).fetchall()
conn.execute("SELECT transition, outcome, substr(vote_summary,1,300), created_at FROM transition_results WHERE task_id=? AND transition='planning_gate' ORDER BY id DESC LIMIT 3", (task_id,)).fetchall()
```
- `gate_checks` empty + `correction_attempts=0` + gate transitions showing "rejected" → correction agent trigger is being evaluated but conditions may not be met
- `correction_attempts > 0` → correction agent ran (check agent_sessions for outcome)
- Multiple `status=active` rows → `supersede_planning_results` not being called (cosmetic, doesn't break logic)

**Step 4 — check agent session history:**
```python
conn.execute('SELECT agent_type, exit_reason, exit_summary, started_at, ended_at FROM agent_sessions WHERE task_id=? ORDER BY id DESC LIMIT 8', (task_id,)).fetchall()
```
- `exit_reason=None, ended_at=None` → session still running (expected for active tasks)
- `exit_reason='rejected'` with `exit_summary='Planning pipeline passed but gate checks failed.'` → gate is failing; correction agent should have triggered
- `exit_reason='planning_correction'` entries → correction agent did run
- Multiple `shutdown` entries → repeated server restarts are interrupting long-running sessions

**Step 5 — check design review vote pattern:**

Look at budget entries for the task in order. The design review runs 5 sequential reviewer calls (each ~5-15 min on a loaded LLM). Each reviewer LLM response should have a verdict in the content:
- `LIKELY` / `POSSIBLE` → reviewer approved the design
- `NEEDS_RESEARCH` / `REJECTED` → reviewer flagged an issue; check the justification — it may be a real design flaw or a false positive

If `tally_votes` returns `needs_research` (any NEEDS_RESEARCH vote) or `rejected` (any REJECTED), the design retry loop fires. After `PLANNING_MAX_DESIGN_RETRIES` (default 3) retries, the pipeline proceeds to consolidation and gate regardless.

**Step 6 — check the interface_completeness gate check:**

The most common gate failure. The check computes `all_consumes - all_provides` across `interface_contracts`. If the LLM puts intra-file refs (e.g., language stdlib types, sealed class subtypes defined in the same file) in `consumes` without matching `provides` entries, the gate hard-fails. The `PlanningCorrectionAgent` is supposed to fix this by removing them from `consumes`. Verify it triggers by looking for `planning_correction` agent sessions.

**Common root causes table:**

| Symptom | Root cause | Fix |
|---------|-----------|-----|
| Design judge has `finish_reason: "length"`, empty `content` | `PLANNING_JUDGE_MAX_TOKENS` too low for reasoning model | Increase in `maestro.ini [planning] judge_max_tokens` |
| Design review NEEDS_RESEARCH from timed-out reviewer | Reviewers ran in parallel; LLM slot starvation under concurrent sessions | Reviewers now run sequentially (fixed) |
| Design review REJECTED by security reviewer | Design proposes removing security-critical columns | Improve planning prompt or task description |
| Gate fails `interface_completeness` every run | Design puts stdlib/intra-file types in `consumes` | `PlanningCorrectionAgent` should patch this; verify it triggers |
| `correction_attempts=0` despite gate failures | Correction agent code is new; gate hasn't been reached in current session | Wait for pipeline to complete; check agent_sessions after |
| `planning_correction` session stalled | LLM can't determine a fix | Check exit_summary; may need manual task description update |

## Architecture

### Backend (`app/`)
- `main.py` — FastAPI app. All routes. Mounts static files from `app/web/`. On startup calls `init_db()` + `seed_sample_tasks()` (skips seeding if data exists). Contains `_project_to_dict()` helper and `_pick_prewarm_resources()` / `_trigger_project_prewarm()` helpers that use the project's own `llm_id` and `budget_id` when set. Quick-action endpoints: `/demote`, `/set-stage`, `/clone`, `/pin`, `/run-planning`, `/run-review`, `/run-security`, `/run-full-review`. Task serialization (`_task_to_dict`) always includes a `"pips"` array — each PIP has `id`, `origin_stage`, `requirements` (list), `created_at`, `status` (derived via `pip_status_at_stage`), `last_summary`, `last_checked`. Empty list `[]` when no PIPs. `sync_update_llm_with_cache` / `sync_delete_llm_with_cache` call `invalidate_llm_cache` after LLM record mutations so stale context/capacity state is flushed immediately.
- `database.py` — SQLAlchemy models + all DB CRUD functions. Key models: `Task`, `LLM`, `Budget`, `Project`, `ComputeNode`, `BudgetEntry`, `TransitionVote`, `TransitionResult`, `SubdivisionRecord`, `FileSummary`, `FileSummaryJob`, `PerformanceImprovementPlan`, `PipVerification`, `PipResolutionJob`. `batch_update_map_positions(updates)` bulk-updates `map_x`/`map_y` without touching task history. `upsert_project()` uses `...` (Ellipsis) sentinel for `llm_id`/`budget_id` — pass Ellipsis to leave unchanged, None to clear. `delete_task()` is a **soft-delete**: sets `is_active=False` on the target and all descendants via BFS. All read queries filter `is_active=True`. `ComputeNode` CRUD is in `crud_infra.py`; PIP CRUD (create/get/verify/status derivation) and resolution job CRUD are in `crud_tasks.py`. `pip_status_at_stage(pip, stage)` derives status at read time — no stored status column (`unverified`/`satisfied`/`unsatisfied`/`checking`).
- `migrations/runner.py` — standalone sqlite3 migration engine, no SQLAlchemy dependency.

### Agent system (`app/agent/`)
- `loop.py` — `MaestroLoop` class. `_ACTIVE_LOOPS` / `_LOOP_STATUS` dicts power the status/stop API endpoints. Drives Design → Implement → Test → Verify cycles. `_build_messages()` injects both the file-structure snapshot and the full architecture context (all categories) derived from the task's project.
- `intake.py` — Intake pipeline orchestrator for IDEA→PLANNING transitions. 4-stage voting: scope analysis, static analysis, feasibility, conflict detection. Passes `project_root` from task's project to `ResearchAgent` and `SubdivisionAgent`.
- `research.py` — Research agent with a "lives" system (max 3 per session). `_build_life_context()` on life 1 injects the file-structure snapshot followed by the full architecture context (all categories), then the investigation question. `WebSearchAgent` class (private to async `web_search` dispatch) — 10-turn agent that fetches pages and synthesizes findings; only tool available to it is `web_fetch`. `ContextTooLargeError` is caught explicitly before the generic `Exception` handler and returns a `TOO_LARGE` verdict immediately without consuming a life or retrying.
- `subdivide.py` — Subdivision agent for decomposing oversized ideas. `_build_context()` injects snapshot then filtered architecture context (Platform/Design/Testing/Performance/API/Data/Tooling/General). Triggered by SUBDIVIDE_IDEA verdict. `ContextTooLargeError` breaks the turn loop immediately with no retry and no appended message.
- `scheduler.py` — Push-first eager task scheduler. Dispatches DAG-ready tasks respecting per-endpoint capacity limits **and** per-compute-node capacity limits. Passes `project_root` to research jobs. Tick order: `_dispatch_file_summary_jobs()` → `_dispatch_arch_gen_jobs()` → `_dispatch_scope_survey_jobs()` → `_dispatch_pip_resolution_jobs()` → pipeline tasks → `_dispatch_dreamer()`. **Dreamer capacity**: `_dispatch_dreamer()` accepts the same capacity params as other dispatchers; calls `_check_and_reserve_slot()` before firing and registers the dreamer thread in `_active_sessions`/`_session_llm_ids` — Dreamers consume one LLM slot for their full run and are visible to the one-LLM-at-a-time policy. Completion registry: `get_or_create_completion_event()`, `signal_completion()`, `wait_for_completion()`. `_task_to_mini_dict` includes `parent_task_id` so `DAGResolver` can build the child index. `SCHEDULER_DISPATCHABLE_TYPES` includes all pipeline stages — orphaned mid-pipeline tasks are re-dispatched on restart; `_active_sessions` alive-check prevents double-dispatch. At the start of each `_tick()`, `node_active_counts` is built by summing `_llm_session_counts` grouped by `compute_node_id`; node cap is checked before per-LLM cap. `_run_subdivision_recovery()` always applies a cooldown after any recovery attempt (uses `_apply_cooldown` flag so `ShutdownError` skips it); this prevents the infinite-retry loop on persistently broken tasks. **PIP pre-flight gate**: each review stage worker calls `_run_pip_preflight_and_gate(task_id, stage, ...)` before running the pipeline — if any PIP fails, `_schedule_pip_resolution_jobs()` creates `pip_resolution_jobs` rows and the stage pipeline is skipped. `_dispatch_pip_resolution_jobs()` drives the research → resolution lifecycle: `pending` → dispatches `ResearchAgent` thread (status `researching`) → completion fires → dispatches `PIPResolutionAgent` thread (status `resolving`) → completion fires → status `done`, scheduler re-dispatches the parent stage. Active `pip_resolution_{pip_id}` sessions count against per-LLM and per-node caps. Tasks with active pip_resolution_jobs are skipped in stage dispatch (guarded by `get_active_pip_resolution_jobs_for_task`).
- `llm_client.py` — Centralized HTTP client for all LLM calls. Requires both `llm_id` and `budget_id`. Logs every call to `budget_entries` + `expenses`. `ContextTooLargeError` (carries `estimated_tokens`, `max_context`) is raised as a pre-flight check before any HTTP call — estimation is `total_chars // 3` (conservative over-estimate), checked against `context_window - max_tokens`; callers must treat this as a clean abort, not an infrastructure error. `_get_llm_max_context(llm_id)` is a module-level cache of context window sizes. All message content is NFKD-normalized and stripped to ASCII before sending (prevents llama.cpp chat-template parse errors on Unicode/control chars). **Hardened backoff**: `_EndpointState` now tracks `fail_count_connect` (server down: ConnectError/ConnectTimeout, cap 15 min) and `fail_count_response` (server overloaded/bad prompt: ReadTimeout/5xx/parse errors, cap 1 min) separately — overload events back off slowly; connection failures back off aggressively. `invalidate_llm_cache(llm_id)` + `update_llm_context_cache(llm_id, max_context)` allow `main.py` to evict stale context/capacity state after LLM record updates.
- `verdicts.py` — Verdict classification with confidence ranges. `Vote` and `TallyResult` dataclasses. `tally_votes()` aggregation logic.
- `static_analysis.py` — Tree-sitter based deterministic Python code analysis for intake stage 2a.
- `tools.py` — Agent tools with OpenAI JSON schemas + `dispatch_tool()`. Named shell tools (`run_pytest`, `run_mypy`, `run_ruff`, `git_restore`, `git_add`, `git_unstage`, etc.) replace the old grouped `run_shell_indev/build/deps/security/review` tools — each tool does exactly one operation. **Path policy**: `_assert_safe_path()` (reads) allows navigation anywhere on the PC but blocks `.git` internals and `.archive`. `_assert_safe_write_path()` (writes) additionally enforces that the path must be inside `effective_root` (the project root) and must not traverse `venv`, `__pycache__`, `node_modules`, etc. Relative paths are always resolved against `effective_root`, not the process CWD. **gitignore policy**: gitignore filtering applies to automatic operations only (prewarm, `walk_safe`, arch-gen, snapshot) — it does NOT block manual tool reads. An agent can call `read_file("app.log")` even if `*.log` is in `.gitignore`; the agent just can't write to gitignored paths. `list_directory` shows ALL entries (never hides them), annotating with: `[PROTECTED - git internals; use git tools, no direct writes]`, `[AUTO-EXCLUDED - skipped by agent tools and summarization]` (venv/build dirs), or `[GITIGNORED - excluded from auto-summarization; read_file/search_files access is allowed]`. All tool results pass through `_cap_tool_result()` which hard-truncates at 200 KiB with a notice.
- `project_snapshot.py` — `build_project_snapshot(project_root)` and `build_snapshot_with_summaries(project_root)` **require an explicit `project_root`** — no default fallback to TheMaestro's own directory. **`build_project_snapshot` respects `.gitignore`**: at each directory level, candidate paths are filtered via `app.agent.path_filter.filter_paths` (consolidates built-in exclusions, hidden files, and .gitignore via `git check-ignore`). `build_file_summary` / `async_build_file_summary` also skip binary files. `async_build_file_summary()` uses enqueue+wait pattern. Session cache uses `("llm", path, mtime, size)` prefix. `build_architecture_context(project_name, agent_type=None)` fetches `type='architecture'` tasks and formats them as a structured constraint block; `ARCH_CATEGORY_RELEVANCE` maps agent type → relevant category set (None = all).
- `path_filter.py` — Central authority for path exclusions. `is_ignored(path, root)` and `filter_paths(paths, root)` combine `TOOL_LISTING_EXCLUDED_DIRS` (venv, node_modules, etc), hidden-file rules, and batch `.gitignore` checks. `walk_safe(root)` provides an `os.walk` replacement that prunes ignored directories in-place for efficiency.
- `survey_orchestrator.py` — Hierarchical project summarization engine. Generates scope summaries bottom-up: Files → Directories → Modules → Project, fitting within LLM context windows by summarizing child summaries. Driven by `ScopeSurveyJob` records in the scheduler.
- `file_summary_agent.py` — `enqueue_file_summary()` + `execute_file_summary()`. Called by scheduler worker thread. Injects a filtered architecture context preamble (Platform/Tooling/Data/General only) into all three prompt paths when a `task_id` is available. `enqueue_file_summary()` returns `("", "", 0)` immediately for binary files (null bytes in first 512 bytes). `execute_file_summary()` repeats the binary check before the LLM call and marks the job completed silently if binary.
- `dag.py` — `DAGResolver`: Kahn's topological sort, ready-task finder, cycle detection. `_children_by_parent` index (built from `parent_task_id` fields) enables `_is_effectively_done()` — a Big Idea parent satisfies a prerequisite edge once all its active (non-cancelled) children are recursively done, without the parent itself reaching `completed`. Parents with children are skipped in `get_ready_tasks()` (not directly dispatchable). Mid-pipeline stages (`indev`, `conceptual_review`, `optimization`, `full_review`) are no longer excluded from `get_ready_tasks()` — they surface as ready when their thread dies, enabling restart recovery.
- `config.py` — constants (endpoint, limits, archive path, branch prefix). `SCHEDULER_DISPATCHABLE_TYPES` includes `pip_resolution` (and no longer includes `pip_verification`). `PIPELINE_COLUMN_ORDER` does not contain `pip_verification`.
- `system_prompt.py` — `MAESTRO_SYSTEM_PROMPT`.
- `mock_llm.py` — Dictionary-based mock LLM for testing.
- `pip_agent.py` — PIP generator and pre-flight gate. `generate_pip(task_id, origin_stage, reason)` — called after any demotion; captures `created_at_commit` via `git rev-parse HEAD` (stores `"none"` when no git history). `run_pip_preflight(task_id, stage, llm_id, budget_id, project_root) -> dict` — runs all PIPs for a task concurrently via `asyncio.gather`; each PIP gets a focused LLM check using git diff stat since `created_at_commit` plus current snapshot; persists a `pip_verifications` row per PIP; returns `{"all_passed": bool, "results": [...]}`. `_get_git_diff_stat(project_root, from_commit)` — `git diff {from_commit}..HEAD --stat`; returns fallback text if commit is `"none"`.
- `pip_resolution.py` — `PIPResolutionAgent` class. Targeted implementation agent that closes specific PIP gaps. Receives `requirements`, `last_verification_findings`, and `research_findings` (from the preceding Research Agent phase). Same tool set as `MaestroLoop` (no `web_search`/`web_fetch`). Max turns: `[pip] resolution_max_turns`. Emits `{"signal": "RESOLUTION_STALLED"}` after 3 consecutive tool failures. Calls `signal_completion(f"pip_resolution_{pip_id}")` on exit so the scheduler re-dispatches the parent stage.

### Project isolation

Each project record has: `name` (PK), `path` (absolute filesystem root), `description`, `llm_id` (default LLM for maintenance), `budget_id` (default budget for maintenance).

#### Why isolation matters

Multiple agent sessions can be dispatched simultaneously — one per task, each running in its own thread. Without isolation, they would share a single git working tree. Agent A calling `git checkout maestro/task-A` would silently switch HEAD for agent B, causing B to read wrong files, write to the wrong branch, or commit changes into A's history. Worse, an agent working on a user project that happens to be hosted inside `D:/workspace/TheMaestro/` could write to — or delete — Maestro's own source files while the server is running.

Isolation is enforced at three levels:

1. **Git worktree isolation** — each dispatched task gets its own independent checkout via `git worktree add`. The checkout lives at `{project_path}/.maestro-worktrees/{task_id}/` and has its own HEAD, index, and working files backed by the shared `.git` object store. Agent writes on one task's branch never touch another task's files.

2. **Filesystem write isolation** — `_assert_safe_write_path()` in `tools.py` enforces that every write, move, or delete targets a path *inside* `effective_root` (the task's project root / worktree). Paths traversing into `venv/`, `__pycache__/`, `node_modules/`, or any other excluded directory are rejected before the operation executes.

3. **Self-protection** — Maestro's own source tree (`D:/workspace/TheMaestro/`) must never be a target project. An agent running against a task whose project path is Maestro itself could corrupt the running server. The rule is: **never configure Maestro's own directory as a project path**. Additionally, `_assert_safe_path()` (reads) blocks `.git` internals, and the `git_checkout` tool blocks any branch that does not start with `maestro/*`, `main`, or `master`.

#### Worktree lifecycle (`app/agent/worktree.py`)

- `setup_task_worktree(task_id, project_path)` — called at the top of `_run_task()` in `scheduler.py`. Creates `{project_path}/.maestro-worktrees/{task_id}/` and a `maestro/task-{task_id}` branch. If the branch already exists (re-dispatched task after restart), calls `git worktree add` without `-b`. Returns `None` for non-git projects; `_run_task` silently falls back to bare `project_path`.
- `teardown_task_worktree(task_id, project_path)` — called in the `finally` block of `_run_task()` so cleanup always runs, even on exception or shutdown. Calls `git worktree remove --force` then `git worktree prune`.
- `prune_orphaned_worktrees(project_paths)` — called once in `start_scheduler()` at server startup. Walks `git worktree list --porcelain` for every project and removes any entries whose path falls under `.maestro-worktrees/`, clearing state left by a previous crashed process.
- `.maestro-worktrees/` is automatically appended to the project's `.gitignore` on first use; the lock `_gitignore_lock` prevents concurrent threads double-writing the entry.

- **Agent isolation** — `IntakePipeline`, `ResearchAgent`, `SubdivisionAgent`, and `MaestroLoop` all receive `project_root` derived from `get_project_path(task.project)`. Snapshot injection is scoped to the task's project, never Maestro's own source tree.
- **Architecture context injection** — `build_architecture_context(project_name, agent_type)` is called in `loop.py` (`_build_messages`), `research.py` (`_build_life_context` life 1), `subdivide.py` (`_build_context`), and `file_summary_agent.py` (`execute_file_summary`). Each agent type receives only the card categories relevant to its work, as defined by `ARCH_CATEGORY_RELEVANCE` in `project_snapshot.py`. Categories with `None` (research, loop, full_review) receive all cards; categories with a set receive only matching cards.
- **Tool isolation** — `_assert_safe_path()` in `tools.py` resolves relative paths against `effective_root` so `read_file("src/main.py")` opens the correct file in the task's project, not in `D:/workspace/TheMaestro/`.
- **LLM/budget inheritance** — When creating a new task, `openAddTaskModal()` pre-selects the current project's `llm_id` as the default LLM. Prewarm file-summary jobs use the project's `budget_id` when set; falls back to first infinite budget otherwise.
- **`allProjects`** global in `kanban.js` — `[{name, path, description, llm_id, budget_id}]`, kept in sync by `loadProjects()`.

### Frontend (`app/web/`)

#### Board (`index.html` + `kanban.js` + `style.css`)
- `index.html` — board shell; project tabs, **`#arch-bar`** (horizontal architecture bar spanning full width above the board), eight pipeline columns (IDEAS, PLANNING, INDEV, CONCEPTUAL_REVIEW, OPTIMIZATION, SECURITY, FULL_REVIEW, COMPLETED), the Column Map overlay (`#column-map-container`), nine modals (task create/edit, new project, edit project, transition, LLM endpoints, budgets, tools, **compute nodes**). New/Edit Project modals both have **Default LLM** and **Budget** dropdowns. The **LLM Endpoints** modal Add/Edit panes each have a **Compute Node** dropdown.
- `kanban.js` — all board behaviour. **PIP card stack**: tasks with PIPs render as a `.task-card-group` wrapper containing the `.task-card` followed by one `.pip-card` per PIP. Tasks with zero PIPs render as bare `.task-card`. Status badge classes: `pip-status--satisfied/unsatisfied/unverified/checking`. The `reconcile()` fingerprint includes `pip.status + pip.last_checked` for in-place badge updates. `draggable="true"` is on `.task-card-group`; drag listeners match both via `isDraggable()`. Key globals (see top of file for full list): `taskData`/`allTasks`/`currentProject`; LLM/budget/compute/project caches; `_archGenJobs`, `_schedulerState`, `columnMapActive`, `_mapNodeDrag`.

#### Architecture Bar (`#arch-bar`)
A dark navy horizontal band rendered **above** the kanban pipeline columns (not inside them). Architecture tasks (`type='architecture'`) live here exclusively — they are not rendered in any pipeline column.

- `renderArchBar()` — rebuilds all `.arch-card` elements from `taskData`; sorts by priority (`critical→high→normal→low`) then `position`. After real cards, appends `.arch-card.ghost` placeholders from `_archGenJobs` for any category not yet covered by a real card (70% opacity, dashed border, breathing animation, shows running/pending dot). Called by `renderTasksFromDatabase()`, after arch card create/edit/delete, and by `reconcile()` on fingerprint change.
- `loadArchGenJobs()` — fetches `GET /api/projects/{name}/arch-gen-jobs`, stores in `_archGenJobs`, calls `renderArchBar()`.
- `_refreshJobIndicators(schedulerData)` — walks `cardCache`, updates `#ji-{taskId}` indicator elements with `.ji-running` (blue) or `.ji-queued` (amber) classes based on scheduler active/queued lists.
- `toggleArchBar()` — flips `_archBarCollapsed`, saves to `localStorage`, toggles `#arch-bar.collapsed` class.
- **Arch card schema** (`content` JSON): `category` (one of 14 fixed values: Platform/Design/Testing/Security/Performance/API/Tooling/Data/UX/Accessibility/Compliance/Deployment/Observability/General) and `priority` (critical/high/normal/low). The card body is the task's `description` field. LLM, budget, owner, tags are not used.
- **Modal integration** — `openAddTaskModal('architecture')` and `editArchitectureTask(taskId)` both use the shared task modal but call `showArchContentFields('architecture')` which shows the `#arch-category` / `#arch-priority` selects, hides LLM/budget/owner/tags fields, and relabels the description field as "Body (the constraint or fact)".
- **`reconcile()` handling** — arch tasks are explicitly skipped in the card-cache loop (no `.task-card` DOM element created); fingerprint changes set `archChanged = true` which triggers `renderArchBar()` at the end.
- **`deleteTask()` handling** — detects `task.type === 'architecture'` and calls `renderArchBar()` instead of searching for a `.task-card` DOM node.

Each card has a `<div class="card-job-indicator" id="ji-{taskId}">` element showing a blue (`.ji-running`) or amber (`.ji-queued`) dot, updated every 5 s by `_refreshJobIndicators`.

**Card toolbar** — hover-revealed; three groups: agent actions, control (run/stop/demote/stage-picker), and utility actions (diagnostics/clone/pin/map). `toolbarStagePicker` opens a flyout listing all 9 stages. `toolbarOpenMap` pans the Column Map to the node with a gold-pulse animation.

#### Column Map View
Clicking any column header or empty whitespace in a column opens a full-screen **Column Map View** — a 2D radial canvas showing tasks as cards with thick bezier arrows between connected nodes. Click the header again or "← Back to Board" to return.

- `openColumnMap(colType, focusNodeId?)` — optional `focusNodeId` pans to center on that node and plays a 3× gold-pulse animation (`.map-node-focus` keyframe, auto-removed after 2s).
- `_mapComputeLayout(tasks, colType)` — three-phase layout: (1) load saved `map_x/map_y`; (2) BFS fan-out for newly-subdivided children; (3) radial `placeSubtree()` for unpositioned nodes. IDEAS/ARCHITECTURE use `parent_task_id` hierarchy; all others use `prerequisites`.
- `_mapStartNodeDrag` — group drag: moving a parent moves it + all descendants by the same delta simultaneously.
- **Positions** are in layout-space (centered around 0), not canvas-space. Canvas position = layout + `(_mapOffsetX, _mapOffsetY)`. Offset recomputed from bounding box each render — saved positions are stable across sessions.
- `reconcile()` skips DOM reconciliation when `columnMapActive`; keeps `taskData` fresh.

#### View Children (Subdivision Sets)
"View Children" opens a paginated modal over subdivision sets (oldest→newest). The active set feeds child tasks to the board; non-active sets show "Activate this set". "Regenerate" polls `GET /api/tasks/{id}/subdivision-records` until the new record leaves `generating` status.

#### Diagnostics (`diagnostics.html` + `diag-*.js`)
Standalone three-panel LLM conversation viewer at `/diagnostics`. Deep-link: `?task=<id>`. `GET /api/diagnostics/tasks` includes a synthetic `__file_summaries__` entry for prewarm calls (`task_id IS NULL`).

### Configuration (`maestro.ini`)
INI file with sections: `[intake]`, `[subdivision]`, `[capacity]`, `[context_warnings]`, `[scheduler]`, `[verdicts]`, `[search]`, `[pip]`.

- `[intake]` — research lives, tiebreaker, allowed research tools, `context_budget_ratio` (fraction of context window for research agent, default 0.60). `research_agent_tools` includes `web_search` — dispatches `WebSearchAgent` asynchronously (search + fetch + synthesize). `web_fetch` is intentionally absent; it is private to `WebSearchAgent`.
- `[subdivision]` — max_depth, max_retries_per_level, max_total_sub_ideas, subdivision_agent_tools, `context_budget_ratio` (default 0.60). Both `subdivision_agent_tools` and `subdivision_planning_tools` include `web_search` for domain research during decomposition.
- `[search]` — `provider` (duckduckgo | brave, default duckduckgo), `brave_api_key` (required only if provider=brave). Env overrides: `MAESTRO_SEARCH_PROVIDER`, `BRAVE_API_KEY`.
- `[pip]` — `resolution_max_turns` (default: 20, max turns for `PIPResolutionAgent` before it auto-stalls).

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
GET    /api/projects/{name}/arch-gen-jobs — pending/running arch gen jobs [{id, category, status, created_at, retry_count}]
GET    /api/tasks/{id}/pips               — full PIP list with verification history per PIP (for PIP detail modal)
```

## Safety rules (for the agent tools)

- **Never hard-delete.** Use `archive_file()` which moves to `.archive/YYYY-MM-DD_HH-MM-SS/`. No `rm`, `del`, `shutil.rmtree`.
- **Agent git work happens on `maestro/task-{id}` branches only.** `git_checkout` blocks anything that isn't `maestro/*`, `main`, or `master`.
- **Named shell tools replace grouped `run_shell_*` tools.** Each tool does exactly one operation with no hidden allowlist for agents to guess. Key tools: `run_pytest`, `run_mypy`, `run_ruff`, `run_black_check`, `run_unittest`, `run_npm_test`, `run_cargo_test`, `run_go_test` (testing); `run_make`, `run_cargo_build`, `run_go_build`, `run_npm_build`, `run_tsc` (build); `run_pip_install`, `run_npm_install`, `run_cargo_fetch` (deps); `run_bandit`, `run_pip_audit`, `run_semgrep`, `run_npm_audit` (security); `git_restore`, `git_add`, `git_unstage` (git helpers). Per-stage access is still controlled by `build_tool_schemas(allowed_names)` in `config.py`.
- After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
