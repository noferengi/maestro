# Project Maestro — Summary

## What This Is

A Kanban board that doubles as the control surface for an agentic LLM orchestration system. The board is real and functional. The agent backend includes a deterministic intake pipeline that gates every column transition behind a multi-stage LLM voting system. The core engine is the "Wiggum Loop" — a persistent Do-While that drives a local LLM through Design -> Implement -> Test -> Verify cycles until every task in the DAG reaches ACCEPTED.

The LLM target is OmniCoder 9B (Qwen 3.5 base) running via llama.cpp on `localhost:8008`, OpenAI API compatible.

---

## Current File Structure

```
app/
├── main.py              FastAPI app, all routes, intake pipeline, subdivision orchestration, completion rollup
├── database.py          SQLAlchemy models (Task, LLM, Budget, TransitionVote, TransitionResult, BudgetEntry, SubdivisionRecord), all DB functions
├── agent/
│   ├── config.py        LLM endpoint, safety constants, intake/subdivision settings, verdict ranges
│   ├── tools.py         16 safe tools + OpenAI schemas + dispatch_tool()
│   ├── system_prompt.py MAESTRO_SYSTEM_PROMPT
│   ├── loop.py          MaestroLoop (the Wiggum engine)
│   ├── dag.py           DAGResolver (Kahn's sort, cycle detection, cancelled/subdividing exclusions)
│   ├── verdicts.py      Verdict enum (6 verdicts incl. SUBDIVIDE_IDEA), Vote, tally_votes(), classify_confidence()
│   ├── static_analysis.py  Tree-sitter code parser (classes, functions, imports, call graphs)
│   ├── intake.py        IntakePipeline orchestrator (IDEA -> PLANNING gate, SUBDIVIDE_IDEA handling)
│   ├── research.py      Research agent with lives system (NEEDS_RESEARCH / tie-breaker)
│   ├── subdivide.py     SubdivisionAgent — decomposes oversized ideas into sub-ideas
│   ├── scheduler.py     Push-first eager task scheduler
│   ├── llm_client.py    Centralized HTTP client with budget tracking
│   └── mock_llm.py      Dictionary-based mock LLM for testing
├── migrations/
│   ├── runner.py        Standalone sqlite3 migration engine
│   └── versions/
│       ├── 0001–0009    (initial schema through budget_entries)
│       └── 0010_add_subdivision_support.py   parent_task_id, subdivision_generation, subdivision_records table
├── models/
│   └── dags.py          TaskDAG, TaskNode (state machine)
├── services/
│   └── repl.py          CheckpointManager (git-based persistence)
├── tests/
│   ├── test_config.py
│   ├── test_integration.py
│   ├── test_repl.py
│   └── test_subdivision.py   21 tests for subdivision verdicts, tally, DAG, parsing, config
└── web/
    ├── index.html       Board UI shell (6 columns)
    ├── kanban.js         All frontend behaviour (subdivision badges, parent links, children viewer)
    └── style.css         All styles (subdivision indicators, verdict colors)
data/
└── kanban.db            SQLite database (10 migrations applied)
maestro.ini              Master config ([llm], [loop], [intake], [subdivision], [scheduler], [verdicts], ...)
pyproject.toml           Dependency management
migrate.bat              Thin wrapper: migrate.bat [migrate|status|reset|rollback]
```

---

## How IDEAs Are Managed

### The IDEA Lifecycle

IDEAs are the only human entry point for new work. The lifecycle is:

```
Human creates IDEA
        |
        v
  [Advance to Planning] button
        |
        v
  Intake Pipeline (4 stages, LLM voting)
        |
        +---> outcome = "passed"     --> type = "planning"
        +---> outcome = "rejected"   --> stays as IDEA (human edits & retries)
        +---> outcome = "subdivide"  --> SUBDIVISION (automatic decomposition)
        +---> outcome = "needs_research" --> research agent investigates, re-tally
        +---> outcome = "tie"        --> tie-breaker agent casts deciding vote
```

### Intake Pipeline Stages

When a user clicks "Advance to Planning" on an IDEA card:

1. **Stage 1: Scope Analysis (LLM)** — Evaluates task scope, complexity, decomposition need, affected areas. Can vote `SUBDIVIDE_IDEA` if the task is sound but too large.

2. **Stage 2a: Static Analysis (deterministic)** — Tree-sitter parses the codebase. Extracts classes, functions, imports, call graphs. No LLM hallucination — pure ground truth.

3. **Stage 2b: Feasibility Analysis (LLM)** — Informed by 2a's structural data. Evaluates technical feasibility, ambiguities, external dependencies, risks.

4. **Stage 3: Conflict Detection (LLM)** — Checks for file-level, semantic, priority, and resource conflicts with existing active tasks.

Execution order: 1 -> {2a, 3} in parallel -> 2b -> Tally.

### The Six Verdicts

| Verdict | Confidence Range | Meaning |
|---------|-----------------|---------|
| `REJECTED` | 0–50 | Fundamentally unfeasible or harmful |
| `NOT_SUITABLE` | 51–60 | Poorly scoped or architecturally questionable |
| `NEEDS_RESEARCH` | 61–75 | Too vague to assess — needs investigation |
| `POSSIBLE` | 76–91 | Feasible with some ambiguity |
| `LIKELY` | 92–100 | Well-defined and clearly feasible |
| `SUBDIVIDE_IDEA` | 0–100 | Sound idea but too large for one context window |

### Tally Rules (evaluated in order)

- **Rule 0**: Any `SUBDIVIDE_IDEA` vote -> `outcome = "subdivide"` (highest priority)
- **Rule 1**: Any `REJECTED` vote -> `outcome = "rejected"` (immediate)
- **Rule 2**: Majority `NOT_SUITABLE` -> `outcome = "rejected"`
- **Rule 3**: Any `NEEDS_RESEARCH` -> `outcome = "needs_research"` (spawns research agent)
- **Rule 4**: Equal pass/fail split -> `outcome = "tie"` (spawns tie-breaker agent)
- **Rule 5**: Otherwise -> `outcome = "passed"`

---

## The Subdivision Mechanism

### When It Triggers

When the intake pipeline returns `outcome = "subdivide"` (any stage voted `SUBDIVIDE_IDEA`), the system automatically decomposes the idea into smaller pieces. This replaces the old behavior where oversized ideas would dead-end at `NOT_SUITABLE` or `REJECTED`.

### How It Works

```
IDEA (too big)
    |
    v
Intake Pipeline votes SUBDIVIDE_IDEA
    |
    v
Parent type -> "subdividing" (hidden from scheduler, shown with badge in UI)
    |
    v
SubdivisionAgent runs (read-only tools, structured JSON output)
    |
    v
Validates sub-idea DAG (cycle detection)
    |
    v
Creates 2–7 child tasks as type="idea" with:
  - parent_task_id = parent's ID
  - subdivision_generation = parent's generation + 1
  - owner = "system"
  - tags = ["subdivision", "gen-N"]
  - prerequisites resolved from sub-idea index references
    |
    v
Creates subdivision_record (audit trail)
    |
    v
Each child enters normal intake pipeline automatically
```

### The SubdivisionAgent (`app/agent/subdivide.py`)

Follows the `ResearchAgent` pattern:
- **Tools**: Read-only (same set as ResearchAgent, configurable via `maestro.ini`)
- **Output**: Structured JSON with `sub_ideas[]`, each having title, description, prerequisites, estimated_scope, rationale
- **Turn limit**: 25 turns (configurable)
- **LLM temperature**: 0.3 (more creative than intake's 0.1 to explore decomposition strategies)
- **Retry awareness**: On retry, receives `rejection_context` with the previous decomposition, which sub-ideas failed and why, and which passed — allowing it to try a different split strategy

### Self-Healing Loop

When a system-generated sub-idea (one with `parent_task_id IS NOT NULL`) fails intake, the system does NOT surface it to the human. Instead:

1. Check retry budget: `attempt_number < max_retries_per_level` (default 2)?
2. **If retries remain**:
   - Cancel all sibling sub-ideas (`type = "cancelled"`)
   - Mark old `subdivision_record` as `"superseded"`
   - Re-run `SubdivisionAgent` with rejection context (previous decomposition, which failed and why, which passed)
   - Agent tries a different split strategy, may preserve sub-ideas that already passed
   - Create new child tasks and new `subdivision_record`
3. **If retries exhausted**:
   - Mark `subdivision_record` as `"failed"`
   - Revert parent to `type = "idea"` — human sees it on the board with failure history

Human-created ideas that fail intake follow the normal path: stay as IDEA, human sees rejection details, edits, and retries.

### Recursive Subdivision

If a sub-idea's own intake returns `SUBDIVIDE_IDEA` (the sub-idea is still too big):
- Check `subdivision_generation < max_depth` (default 3)
- Check total sub-ideas across all levels < `max_total_sub_ideas` (default 15)
- If within limits: recurse (the sub-idea becomes a parent with its own children)
- If at limit: downgrade to `NOT_SUITABLE` with note explaining the depth/count limit was hit

### Three Independent Recursion Guards

1. **`max_depth = 3`** — Maximum subdivision generations (human -> gen 1 -> gen 2 -> gen 3, no deeper)
2. **`max_total_sub_ideas = 15`** — Total descendants across all levels (prevents combinatorial explosion)
3. **`max_retries_per_level = 2`** — Re-subdivision attempts when sub-ideas fail intake

### Completion Rollup

When all leaf children of a subdivided parent reach `"completed"`:
- Parent automatically transitions to `"completed"`
- History entry records the subdivision chain
- Rollup recurses upward (if the parent itself has a parent)

### Data Model

**Tasks table** (new columns):
- `parent_task_id TEXT REFERENCES tasks(id)` — NULL for human-created, set for sub-ideas
- `subdivision_generation INTEGER DEFAULT 0` — 0=human, 1=first split, 2=sub-split, etc.

**Subdivision records table** (audit trail):
```
subdivision_records:
  id, parent_task_id, attempt_number, generation,
  child_task_ids (JSON), rejection_context (JSON), agent_vote (JSON),
  prompt_tokens, completion_tokens, status (active|superseded|failed),
  created_at
```

### DAG Integration

- `"cancelled"` and `"subdividing"` tasks are excluded from `DAGResolver.get_ready_tasks()` — they won't be dispatched by the scheduler
- Cancelled sub-ideas stay in the database for audit but are filtered from the board UI
- Sub-idea prerequisites are resolved from the SubdivisionAgent's output (`"sub-0"`, `"sub-1"`, etc.) to real task IDs

### Configuration (`maestro.ini` [subdivision] section)

```ini
[subdivision]
max_depth = 3                    ; max recursion levels
max_retries_per_level = 2        ; re-attempts when sub-ideas fail
max_total_sub_ideas = 15         ; hard cap across all levels
llm_temperature = 0.3            ; agent creativity
subdivision_agent_tools = read_file, read_file_lines, count_lines,
    search_files, find_files, list_directory,
    git_status, git_diff, git_log, git_blame, git_show,
    get_task, list_tasks
```

---

## What's Working

### Kanban Board
- Six columns: ARCHITECTURE, IDEAS, PLANNING, DEVELOPMENT, REVIEW, COMPLETED
- IDEAS is the human entry point — users create ideas here
- Task creation locked to IDEAS and ARCHITECTURE only
- Per-project task isolation
- LLM endpoint and Budget assignment per task (required before advancement)
- Sub-idea cards show generation badge (purple "Gen N") and clickable parent link
- Subdividing tasks show animated "Subdividing" badge
- "View Children" button opens subdivision detail modal with child tasks and attempt history
- Cancelled sub-ideas hidden from board view

### Intake Pipeline (IDEA -> PLANNING gate)
- 4-stage voting with 6 verdicts (including `SUBDIVIDE_IDEA`)
- Research agent with 3 lives for `NEEDS_RESEARCH` verdicts
- Tie-breaker agent for split votes
- `SUBDIVIDE_IDEA` triggers automatic decomposition (Rule 0, highest priority)
- Self-healing retry loop for system-generated sub-ideas
- Recursive subdivision with 3 independent depth/count/retry guards
- Completion rollup when all children finish
- All votes, subdivision records, and token costs persisted

### Drag-and-Drop
- HTML5 native drag events
- Ghost rectangle with 120ms open animation
- Cross-column drag with advancement validation
- Positions authoritative from DB after every drop

### Database & Migrations
- SQLite via SQLAlchemy, 10 migrations applied
- Models: Task, LLM, Budget, TransitionVote, TransitionResult, BudgetEntry, SubdivisionRecord
- Custom migration runner (`migrate.bat`) — no Alembic dependency
- Task schema includes `parent_task_id` and `subdivision_generation`

### Agent Backend
- `MaestroLoop` — async Do-While, talks to llama.cpp, dispatches tool calls
- `SubdivisionAgent` — read-only tools, structured decomposition output, retry-aware
- `ResearchAgent` — lives system for investigating unknowns
- 16 tools: file I/O, search, git, shell (blocklisted), task queries
- DAGResolver — topological sort, ready-task finder, cycle detection (excludes cancelled/subdividing)
- Push-first eager scheduler with per-endpoint capacity limits

### Tests
- 94 total tests (73 existing + 21 subdivision-specific)
- Subdivision tests cover: verdict enum, tally Rule 0, DAG exclusions, result parsing, config loading, _build_tally integration

---

## What Needs Doing Next

### Immediate
| Item | Notes |
|------|-------|
| Auto-advance sub-ideas | After subdivision creates child IDEA tasks, automatically trigger their intake pipelines |
| Budget check before subdivision | Verify remaining budget before launching SubdivisionAgent |
| Subdivision agent low-confidence rejection | When agent returns confidence < 50 and recommends rejection, reject parent instead of subdividing |
| Task detail modal — Children tab | Full tree view of subdivision hierarchy in the task edit modal |

### Other column transitions (design TBD)
| Item | Notes |
|------|-------|
| PLANNING -> DEVELOPMENT gate | Validate design docs exist and are sufficient |
| DEVELOPMENT -> REVIEW gate | Verify tests pass and code matches design |
| REVIEW -> COMPLETED gate | Final acceptance criteria check |

### Board improvements
| Item | Notes |
|------|-------|
| Wire Wiggum Loop to board UI | "Run with Maestro" button per DEVELOPMENT task, live status panel |
| Task prerequisites UI | Column exists in DB, not yet editable in the board |
| DAG visualization | Directed graph view of task dependencies, showing subdivision tree |
| Subdivision tree view | Collapsible tree showing parent -> children -> grandchildren hierarchy |

### Safety hardening
| Item | Notes |
|------|-------|
| Shell allowlist (replace blocklist) | Only permit pytest, pylint, git read-only, pip list, cat, head, wc |
| Write journaling | Snapshot file content before every write_file() |
| Git worktree isolation | One worktree per agent run |

---

## Key API Routes

```
GET  /api/projects/{project_name}/tasks   — all tasks for a project
POST /api/tasks                           — create task (IDEA and ARCHITECTURE only)
PUT  /api/tasks/{id}                      — update task (triggers completion rollup if moved to completed)
POST /api/tasks/{id}/reorder              — {position, type} — reorder/move between columns
POST /api/tasks/{id}/advance              — trigger intake pipeline (IDEA -> PLANNING)
GET  /api/tasks/{id}/transition-status    — latest pipeline result + vote breakdown
GET  /api/tasks/{id}/children             — direct child tasks of a subdivided task
GET  /api/tasks/{id}/subdivision-records  — audit trail of subdivision attempts
POST /api/agent/run/{task_id}             — start MaestroLoop (background)
GET  /api/agent/status/{task_id}          — loop status
POST /api/agent/stop/{task_id}            — request graceful stop
GET  /api/agent/tasks/ready               — DAG-ready tasks (excludes cancelled/subdividing)
GET  /api/agent/tools                     — tool schemas + agent access tree (includes SubdivisionAgent)
GET  /api/scheduler/status                — scheduler state
CRUD /api/llms, /api/llms/{id}            — LLM endpoint management
CRUD /api/budgets, /api/budgets/{id}      — budget management
GET  /api/budget-entries                  — budget entry listing
GET  /api/budget-entries/{id}/full        — single entry with full prompt/response
GET  /api/budgets/{id}/summary            — aggregated budget usage
```

---

## Running Locally

```bash
# Start server
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# Database
migrate.bat status
migrate.bat migrate
migrate.bat reset      # destructive — drops and re-seeds

# Tests
venv\Scripts\python.exe -m pytest app/tests/ -v
venv\Scripts\python.exe -m pytest app/tests/test_subdivision.py -v   # subdivision only

# Install dependencies
venv\Scripts\pip.exe install -e .
```

Board: `http://localhost:8000`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B)
