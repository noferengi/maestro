# Project Maestro — Summary

## What This Is

A Kanban board that doubles as the control surface for an agentic LLM orchestration system. The board is real and functional. The agent backend includes a deterministic intake pipeline that gates every column transition behind a multi-stage LLM voting system. The core engine is the "Wiggum Loop" — a persistent Do-While that drives a local LLM through Design -> Implement -> Test -> Verify cycles until every task in the DAG reaches ACCEPTED.

The LLM target is OmniCoder 9B (Qwen 3.5 base) running via llama.cpp on `localhost:8008`, OpenAI API compatible.

---

## Current File Structure

```
app/
├── main.py              FastAPI app, all routes, intake pipeline endpoint
├── database.py          SQLAlchemy models (Task, LLM, Budget, TransitionVote, TransitionResult), all DB functions
├── agent/
│   ├── config.py        LLM endpoint, safety constants, intake pipeline settings, verdict ranges
│   ├── tools.py         16 safe tools + OpenAI schemas + dispatch_tool()
│   ├── system_prompt.py MAESTRO_SYSTEM_PROMPT
│   ├── loop.py          MaestroLoop (the Wiggum engine)
│   ├── dag.py           DAGResolver (Kahn's sort, cycle detection)
│   ├── verdicts.py      Verdict enum, Vote dataclass, tally_votes(), classify_confidence()
│   ├── static_analysis.py  Tree-sitter code parser (classes, functions, imports, call graphs)
│   └── intake.py        IntakePipeline orchestrator (IDEA -> PLANNING gate)
├── migrations/
│   ├── runner.py        Standalone sqlite3 migration engine
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_add_prerequisites.py
│       ├── 0003_add_project_field.py
│       ├── 0004_add_llm_budget.py
│       ├── 0005_llm_budget_tables.py
│       └── 0006_add_idea_column_and_votes.py
└── web/
    ├── index.html       Board UI shell (6 columns)
    ├── kanban.js         All frontend behaviour
    └── style.css         All styles
data/
└── kanban.db            SQLite database
pyproject.toml           Dependency management (replaces requirements.txt)
migrate.bat              Thin wrapper: migrate.bat [migrate|status|reset|rollback]
PLAN_OF_ACTION.md        Full design doc for the intake pipeline system
```

---

## What's Working

### Kanban Board
- Six columns: ARCHITECTURE, IDEAS, PLANNING, DEVELOPMENT, REVIEW, COMPLETED
- IDEAS is the human entry point — users create ideas here
- Task creation locked to IDEAS and ARCHITECTURE only (backend rejects direct creation in other columns via API or curl)
- Per-project task isolation — switching projects fetches only that project's tasks
- Task creation modal with architecture-specific content fields
- Task editing, history tracking, proof-of-work timeline
- LLM endpoint and Budget assignment per task (required before advancement)

### Intake Pipeline (IDEA -> PLANNING gate)
- "Advance to Planning" button on IDEA cards triggers the intake pipeline
- **Stage 1: Scope Analysis** — LLM evaluates task size, decomposition, complexity
- **Stage 2a: Static Analysis** — Tree-sitter parses the codebase deterministically (classes, methods, imports, call graphs) — no LLM hallucination
- **Stage 2b: Feasibility Analysis** — LLM evaluates feasibility informed by Stage 2a ground-truth data
- **Stage 3: Conflict Detection** — LLM checks for conflicts with existing in-flight tasks
- Execution order: Stage 1 first, then 2a and 3 in parallel, then 2b, then tally
- **Voting system** with 5 verdicts: REJECTED [0-50%], NOT_SUITABLE (50-60%], NEEDS_RESEARCH (60-75%], POSSIBLE (75-92%), LIKELY [92-100%]
- Tally rules: any REJECTED = immediate fail, majority NOT_SUITABLE = fail, NEEDS_RESEARCH triggers research agents (3 lives), ties go to tie-breaker agent
- All votes, confidence scores, justifications, and token costs persisted in `transition_votes` and `transition_results` tables
- Budget tracking per LLM call (prompt/completion tokens, model, stage)
- Rejected cards get red outline visual; processing cards get yellow pulse animation
- Pipeline results available via `GET /api/tasks/{id}/transition-status`

### Drag-and-Drop
- HTML5 native drag events — no mouse tracking
- Ghost rectangle with 120ms open animation
- Cross-column drag support with advancement validation
- Positions authoritative from DB after every drop

### Database & Migrations
- SQLite via SQLAlchemy, 6 migrations applied
- Models: Task, LLM, Budget, TransitionVote, TransitionResult
- Custom migration runner (`migrate.bat`) — no Alembic dependency
- Task schema: `id, title, type, description, owner, tags, content, llm_id, budget_id, history, position, prerequisites, project, created_at, updated_at`

### Agent Backend
- `MaestroLoop` — async Do-While, talks to llama.cpp, dispatches tool calls, tracks consecutive errors, emits terminal JSON signals
- 16 tools: file read/write/append/search/glob, `archive_file` (soft delete), `run_shell` (blocklisted), git tools (branch enforced to `maestro/task-*`), Kanban task tools
- DAGResolver — topological sort, ready-task finder, cycle detection
- FastAPI endpoints: `POST /api/agent/run/{task_id}`, `GET /api/agent/status/{task_id}`, `POST /api/agent/stop/{task_id}`, `GET /api/agent/tasks/ready`
- Failure protocol: 3 consecutive tool errors -> `{"signal": "REVERT_TO_DESIGN"}` and halt

### Static Analysis Engine
- Tree-sitter Python parser extracts classes (methods, bases, line ranges), functions (params, async flag), imports, global variables
- Project-wide analysis builds import graph and reverse import graph
- Deterministic vote generator checks file existence, parse errors, circular imports
- Used as Stage 2a in the intake pipeline — feeds ground-truth to LLM stages

---

## What Needs Doing Next

### Immediate (pipeline completion)
| Item | Notes |
|------|-------|
| Research agent implementation | NEEDS_RESEARCH verdict should spawn a research sub-agent with 3 lives; currently the verdict is recorded but no research agent runs |
| Tie-breaker agent | When votes split 2-2, a tie-breaker agent should investigate with all voter context; not yet implemented |
| Pipeline status polling in UI | Frontend should poll `/api/tasks/{id}/transition-status` while pipeline runs and update card state |
| Rejection detail view | When a card is rejected, clicking it should show the full vote breakdown and rejection reasons |
| IDEA card re-submission | After rejection, human edits the idea and re-submits; need UI flow for this |

### Other column transitions (design TBD)
| Item | Notes |
|------|-------|
| PLANNING -> DEVELOPMENT gate | Stages TBD — should validate design docs exist and are sufficient |
| DEVELOPMENT -> REVIEW gate | Stages TBD — should verify tests pass and code matches design |
| REVIEW -> COMPLETED gate | Stages TBD — final acceptance criteria check |

### Board improvements
| Item | Notes |
|------|-------|
| Wire Wiggum Loop to board UI | "Run with Maestro" button per DEVELOPMENT task, live status panel |
| Task prerequisites UI | Column exists in DB, not yet editable in the board |
| Rename / delete projects | Tab UI exists, no rename/delete actions yet |
| Agent REVERT_TO_DESIGN flow | Signal exists, board doesn't react to it yet |
| DAG visualization | Directed graph view of task dependencies |

### Safety hardening
| Item | Notes |
|------|-------|
| Shell allowlist (replace blocklist) | Only permit pytest, pylint, git read-only, pip list, cat, head, wc |
| Eliminate shell=True | Use subprocess.run(shlex.split(command)) |
| Write journaling | Snapshot file content before every write_file() |
| Git worktree isolation | One worktree per agent run |
| Pre-run snapshot tags | Lightweight git tag before any agent loop starts |

---

## Key API Routes

```
GET  /api/projects/{project_name}/tasks   — all tasks for a project
POST /api/tasks                           — create task (IDEA and ARCHITECTURE only)
PUT  /api/tasks/{id}                      — update task
POST /api/tasks/{id}/reorder              — {position, type} — reorder/move between columns
POST /api/tasks/{id}/advance              — trigger intake pipeline (IDEA -> PLANNING)
GET  /api/tasks/{id}/transition-status    — latest pipeline result + vote breakdown
POST /api/agent/run/{task_id}             — start MaestroLoop (background)
GET  /api/agent/status/{task_id}          — loop status
GET  /api/agent/tasks/ready               — DAG-ready tasks
GET  /api/llms                            — list LLM endpoints
POST /api/llms                            — add LLM endpoint
GET  /api/budgets                         — list budgets
POST /api/budgets                         — add budget
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

# Install dependencies
venv\Scripts\pip.exe install -e .
```

Board: `http://localhost:8000`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B)
