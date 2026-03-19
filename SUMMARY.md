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

## File Structure

```
app/
├── main.py                  FastAPI app, all routes, intake/subdivision orchestration,
│                            completion rollup. Imports PIPELINE_COLUMN_ORDER and
│                            PIPELINE_DONE_STATUSES from config.
├── database.py              SQLAlchemy models (Task, LLM, Budget, TransitionVote,
│                            TransitionResult, BudgetEntry, SubdivisionRecord,
│                            PlanningResult, ComponentResult, OptimizationResult,
│                            SecurityReviewResult, FullReviewResult, MergeRecord)
│                            + all DB CRUD functions.
├── agent/
│   ├── config.py            Single config interface. Load order: env → maestro.ini →
│   │                        hardcoded defaults. Exports all tuneable constants.
│   ├── json_utils.py        Shared JSON extraction: extract_json_block(),
│   │                        parse_json_block(). Used by loop.py and research.py.
│   ├── tools.py             26 safe tools + OpenAI schemas + dispatch_tool().
│   │                        LISTING_EXCLUDED_DIRS sourced from config.
│   ├── system_prompt.py     MAESTRO_SYSTEM_PROMPT
│   ├── loop.py              MaestroLoop (the Wiggum engine)
│   ├── dag.py               DAGResolver — Kahn's sort, cycle detection.
│   │                        _DONE_STATUSES and _TYPE_ORDER from config.
│   ├── verdicts.py          Verdict enum (UPPERCASE values), Vote dataclass,
│   │                        TallyResult, tally_votes(), classify_confidence()
│   ├── static_analysis.py   Tree-sitter deterministic code parser
│   ├── intake.py            IntakePipeline (IDEA → PLANNING gate, 4-stage voting).
│   │                        VERDICT_* constants derived from Verdict enum.
│   ├── planning.py          PlanningPipeline (5 stages: survey, best-of-N design,
│   │                        review panel, pitfall detection, consolidation)
│   ├── planning_gate.py     PlanningGate (7 checks, #6 is LLM feasibility re-check).
│   │                        Uses PIPELINE_DONE_STATUSES from config.
│   ├── dev_orchestrator.py  DevOrchestrator (batch execution, parallel components)
│   ├── component_loop.py    ComponentLoop + ComponentToolDispatcher (file containment)
│   ├── conceptual_review.py ConceptualReviewPipeline (4 deterministic + 4 LLM reviewers)
│   ├── optimization.py      OptimizationPipeline (profile → propose → vote → implement
│   │                        → verify)
│   ├── security_review.py   SecurityPipeline (3 parallel agents, veto power, allowlisted
│   │                        shell)
│   ├── full_review.py       FullReviewPipeline (4 parallel reviewer agents: functional,
│   │                        quality, integration, ux)
│   ├── merge.py             Deterministic git merge (NO LLM): branch → checkout →
│   │                        merge --no-ff → test → push → tag
│   ├── merge_conflict_resolver.py  LLM-assisted resolver for parallel component collisions
│   ├── research.py          Research agent with lives system (NEEDS_RESEARCH / tie-breaker)
│   ├── subdivide.py         SubdivisionAgent — decomposes oversized ideas into sub-ideas.
│   │                        Uses LISTING_EXCLUDED_DIRS from tools.
│   ├── scheduler.py         Push-first eager task scheduler (auto-dispatches planning +
│   │                        indev only)
│   ├── llm_client.py        Centralized HTTP client with budget tracking
│   └── mock_llm.py          Dictionary-based mock LLM for testing
├── migrations/
│   ├── runner.py            Standalone sqlite3 migration engine
│   └── versions/
│       ├── 0001–0010        Initial schema through subdivision support
│       └── 0011–0016        big_idea_flag, planning_results, component_results,
│                            optimization_results, security/full_review/merge tables,
│                            demotion tracking
├── models/
│   └── dags.py              TaskDAG, TaskNode (state machine)
├── services/
│   └── repl.py              CheckpointManager + legacy MaestroREPL (pre-FastAPI, not
│                            used by main; .maestro/task_dag.json holds task-1 as ACCEPTED
│                            to prevent commit spam from the old loop)
├── tests/
│   ├── test_config.py
│   ├── test_integration.py
│   ├── test_repl.py
│   ├── test_subdivision.py
│   ├── test_planning_tools.py
│   ├── test_grouped_drag.py
│   ├── test_zoom_view.py
│   └── test_pipeline_routing.py
└── web/
    ├── index.html           Board UI shell (9 columns)
    ├── kanban.js            All frontend behaviour
    └── style.css            All styles
data/
└── kanban.db                SQLite database (16 migrations applied)
.maestro/
└── task_dag.json            Legacy REPL state (task-1 = ACCEPTED, silences old loop)
maestro.ini                  Master config (11 sections — see Configuration below)
pyproject.toml               Dependency management
migrate.bat                  Thin wrapper: migrate.bat [migrate|status|reset|rollback]
```

---

## The 9-Stage Pipeline

```
IDEA → [intake] → PLANNING → [planning + gate] → INDEV → [dev_orchestrator]
     → CONCEPTUAL_REVIEW → [conceptual_review] → OPTIMIZATION → [optimization]
     → SECURITY → [security_review] → FULL_REVIEW → [full_review] → COMPLETED
```

### Advance Handlers (`ADVANCE_HANDLERS` in `main.py`)

| Column             | Handler                      | Trigger  |
|--------------------|------------------------------|----------|
| `idea`             | `_run_intake_pipeline`       | Manual   |
| `planning`         | `_run_planning_pipeline_bg`  | Auto     |
| `indev`            | `_run_dev_orchestrator_bg`   | Auto     |
| `conceptual_review`| `_advance_to_optimization`   | Manual   |
| `optimization`     | `_run_security_pipeline_bg`  | Manual   |
| `security`         | `_run_full_review_bg`        | Manual   |
| `full_review`      | `_execute_merge_bg`          | Manual   |

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

## Agent Inventory

| Agent | File | Tools | Max Turns | Terminal |
|-------|------|-------|-----------|----------|
| **MaestroLoop** | `loop.py` | All 26 | 150 | `ACCEPTED` / `REVERT_TO_DESIGN` |
| **IntakePipeline** | `intake.py` | LLM only (no file tools) | 1 call/stage | tally outcome |
| **ResearchAgent** | `research.py` | 13 read-only | 20/life × 3 lives | verdict JSON |
| **SubdivisionAgent** | `subdivide.py` | Read + planning tools | 25 | sub-ideas JSON |
| **Scheduler** | `scheduler.py` | None (dispatcher) | — | — |

---

## Tool System (26 tools)

### Access by agent role

| Category | Tools | MaestroLoop | Research | Subdivision |
|----------|-------|-------------|----------|-------------|
| File read | read_file, read_file_lines, count_lines, list_directory, search_files, find_files | ✓ | ✓ | ✓ |
| File write | write_file, append_file | ✓ | — | — |
| Soft delete | archive_file | ✓ | — | — |
| Shell | run_shell | ✓ | — | — |
| Shell (allowlisted) | run_shell_security, run_shell_review | ✓ | — | — |
| Git read | git_status, git_diff, git_log, git_blame, git_show | ✓ | ✓ | ✓ |
| Git write | git_create_branch, git_commit, git_checkout | ✓ | — | — |
| Task read | get_task, list_tasks | ✓ | ✓ | ✓ |
| Task write | update_task_status, append_task_history | ✓ | — | — |
| Planning | generate_architecture_doc, generate_interface_contract, generate_mermaid_diagram, spawn_research_agent | — | — | ✓ |

### Sandboxing model

- **Path containment** — `_assert_safe_path()` resolves symlinks then asserts `startswith(PROJECT_ROOT)`.
- **`.git` hard rejection** — `_assert_archivable()` blocks `.git` and everything inside it before any OS call.
- **No re-archiving** — `archive_file` rejects paths already inside `ARCHIVE_DIR` and instead returns undelete instructions with the archived copy's location.
- **Soft-delete only** — `archive_file` uses `shutil.move` to `.archive/<timestamp>/`. Never calls `os.remove`, `os.unlink`, or `shutil.rmtree`.
- **Shell blocklist** — 19 regex patterns block `rm -rf`, `del /s`, `shutil.rmtree`, `os.remove`, `os.unlink`, fork bombs, disk wipe commands, deep traversal, pipe-to-shell injections.
- **Git branch allowlist** — only `maestro/task-*` branches can be created or checked out (plus `main`/`master`).
- **Listing exclusions** — `LISTING_EXCLUDED_DIRS` (sourced from `maestro.ini [tools]`) hides system folders from all three listing tools. Root `.archive` and `.git` are always hidden by absolute path regardless.

---

## Configuration

All tuneable values live in `maestro.ini`. The load order is:

```
Environment variable (MAESTRO_* prefix) > maestro.ini > built-in default in config.py
```

### Sections

| Section | Controls |
|---------|----------|
| `[llm]` | base_url, model, max_tokens_per_turn, temperature, timeout_seconds |
| `[loop]` | max_turns, max_consecutive_errors, max_task_retries |
| `[shell]` | timeout_seconds |
| `[git]` | branch_prefix, allowed_base_branches, git_timeout_seconds |
| `[paths]` | project_root, archive_dir |
| `[intake]` | research_agent_max_lives, tiebreaker_enabled, llm_temperature, tool lists |
| `[subdivision]` | max_depth, max_retries_per_level, max_total_sub_ideas, llm_temperature, context_budget_ratio, tool lists |
| `[capacity]` | min/max_parallel_sessions, min/max_context_size |
| `[context_warnings]` | enabled, thresholds at 50%/75%/90% with configurable messages |
| `[scheduler]` | tick_interval, enabled |
| `[verdicts]` | confidence ranges for REJECTED/NOT_SUITABLE/NEEDS_RESEARCH/POSSIBLE/LIKELY |
| `[pipeline]` | column_order, done_statuses — imported by dag.py, main.py, planning_gate.py |
| `[tools]` | max_search_results, max_git_log_entries, git_timeout_seconds, excluded_directories |
| `[planning]` | best_of_n, temperature_spread, judge_temperature, max_design_retries, survey_max_turns |
| `[planning_gate]` | feasibility_recheck_enabled, context_safety_margin |
| `[indev]` | component_max_turns, component_max_retries, llm_temperature, enforce_file_containment |
| `[conceptual_review]` | reviewer_max_turns, llm_temperature, high_severity_blocks_advance |
| `[optimization]` | proposal_count, judge_count, implementation_max_turns, temperatures, improvement thresholds |
| `[security_review]` | llm_temperature, veto_power, research_agent_max_lives |
| `[full_review]` | llm_temperature, auto_ux_review, frontend_patterns, research_agent_max_lives |
| `[merge]` | test_timeout, auto_push, tag_merged_branches, delete_merged_branches |

---

## Verdict System

Canonical enum in `verdicts.py` — all values are UPPERCASE strings:

| Verdict | Confidence Range | Meaning |
|---------|-----------------|---------|
| `REJECTED` | 0–50 | Fundamental blocker; halts pipeline immediately |
| `NOT_SUITABLE` | 51–60 | Poorly scoped; majority triggers rejection |
| `NEEDS_RESEARCH` | 61–75 | Insufficient info; spawns ResearchAgent |
| `POSSIBLE` | 76–91 | Feasible with some concerns |
| `LIKELY` | 92–100 | High-confidence pass |
| `SUBDIVIDE_IDEA` | 0–100 | Categorical signal; spawns SubdivisionAgent |
| `CONDITIONAL_PASS` | 76–100 | Passes with noted concerns |

`intake.py` derives its `VERDICT_*` string constants from `Verdict.XXX.value` — no independent
definitions. All agent files that construct a `Verdict` from LLM output use `Verdict(str.upper())`
against the UPPERCASE enum values.

---

## Test Suite

**163 tests, all passing.**

```bash
venv\Scripts\python.exe -m pytest app/tests/ -v
venv\Scripts\python.exe -m pytest app/tests/test_repl.py -v           # single file
venv\Scripts\python.exe -m pytest app/tests/test_repl.py -k test_name -v  # single test
```

---

## Running Locally

```bash
# Server
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# Database
migrate.bat status
migrate.bat migrate
migrate.bat reset      # destructive — drops everything, re-migrates, re-seeds

# Dependencies
venv\Scripts\pip.exe install -e .
```

Board: `http://localhost:8000`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B)

---

## Key Design Decisions

- **Single config interface** — `config.py` is the only import for tuneable values. No file
  other than `config.py` reads `maestro.ini` directly.
- **Verdict enum is canonical** — `intake.py` derives its string constants from
  `Verdict.XXX.value`. Renaming a verdict in `verdicts.py` propagates everywhere automatically.
- **Pipeline order and done-statuses in config** — `PIPELINE_COLUMN_ORDER` and
  `PIPELINE_DONE_STATUSES` are read from `maestro.ini [pipeline]` and imported by `dag.py`,
  `main.py`, and `planning_gate.py`. Adding a new stage requires editing only `maestro.ini`.
- **Shared JSON extraction** — `json_utils.py` provides `extract_json_block()` and
  `parse_json_block()`. All agents use this instead of inline regex.
- **Soft-delete everywhere** — `archive_file` is the only deletion primitive. It moves files to
  `.archive/<timestamp>/` and returns restore instructions. Hard deletion is impossible through
  any tool. `.git` is permanently protected with a hard rejection at the tool layer.
- **Directory exclusions configurable** — `LISTING_EXCLUDED_DIRS` is built from
  `maestro.ini [tools] excluded_directories`. Changing what agents can see in file listings
  requires no code change.
- **Agent branches isolated** — every MaestroLoop run creates a `maestro/task-{id}` branch.
  `git_checkout` enforces an allowlist; `git_create_branch` enforces the prefix. Agents cannot
  commit to `main`.
