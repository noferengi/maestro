# TheMaestro — Agentic Software Factory

> **An agentic LLM orchestration system for automated software project management.**
> A Kanban board whose cards are executed by LLMs, not tracked by humans.

[![FastAPI](https://img.shields.io/badge/FastAPI-005571?logo=fastapi)](https://fastapi.tiangolo.com/)
[![SQLite](https://img.shields.io/badge/SQLite-003B57?logo=sqlite)](https://www.sqlite.org/)
[![Python](https://img.shields.io/badge/Python-3.10+-3776AB?logo=python)](https://www.python.org/)

---

## What It Is

TheMaestro takes human intent — expressed as **IDEA cards on a Kanban board** — and drives a fleet of locally-hosted LLMs through a closed-loop pipeline:

```
IDEA → Intake Vote → Planning → Implementation → Review → Accept
```

Each stage is automated, gated, and auditable. A non-engineer with a clear idea and a good GPU can ship production-quality software.

### Core Principles

| Principle | What It Means |
|---|---|
| **Irreversibility prevention** | Every agent action is either reversible (archive, branch) or gated (review panel, intake vote) |
| **Markdown first** | Designs are blueprinted in `.md` files before any source code is generated |
| **Git isolation** | Each task executes in its own worktree — agents cannot touch main or each other |
| **Defense in depth** | Five independent safety layers; failure of one does not compromise the system |
| **Human oversight** | Human merge required; agents propose, humans decide |

---

## Quick Start

```bash
# 1. Activate virtual environment
venv/Scripts/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the server
python -m uvicorn app.main:app --port 8000

# 4. Open the Kanban board
#    http://localhost:8000/kanban.html
```

The server runs on **http://localhost:8000**. The Kanban board is at `/kanban.html`. API docs at `/docs`.

> **Note:** Use **forward slashes** for all paths. Backslashes are treated as escape characters in bash and silently dropped.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│                    HUMAN OPERATOR                         │
│          Kanban Board  ·  Stage Journal  ·  Diagnostics   │
└──────────────────────┬───────────────────────────────────┘
                       │  HTTP (FastAPI)
┌──────────────────────▼───────────────────────────────────┐
│                   TheMaestro Server                       │
│  REST API  ·  Scheduler  ·  MCP Server                    │
└──────────────────────┬───────────────────────────────────┘
                       │  agent dispatch
┌──────────────────────▼───────────────────────────────────┐
│                   Agent System                            │
│  MaestroLoop · Planning · Review · Security · Optimization│
└──────────────────────┬───────────────────────────────────┘
                       │  OpenAI-compatible HTTP
┌──────────────────────▼───────────────────────────────────┐
│              Compute Resource Layer                       │
│  Compute Nodes → LLM Endpoints (local or remote)          │
└───────────────────────────────────────────────────────────┘
                       │
┌──────────────────────▼───────────────────────────────────┐
│              SQLite Database (data/kanban.db)             │
└───────────────────────────────────────────────────────────┘
```

See **[ARCHITECTURE.md](ARCHITECTURE.md)** for the full system reference — topology diagrams, pipeline flowcharts, agent registry, capacity model, safety layers, and data schema.

---

## The Pipeline

Cards flow through nine stages. Each transition is gated by automated checks and LLM review panels.

| Stage | Agent | Gate | Demotes To |
|---|---|---|---|
| **IDEA** | IntakePipeline | 4-stage LLM vote panel | — |
| **PLANNING** | PlanningPipeline (5 stages) | 7 deterministic + 1 LLM check | IDEA (if scope too large) |
| **INDEV** | DevOrchestrator → MaestroLoop | All components pass + tests green | PLANNING |
| **CONCEPTUAL_REVIEW** | ConceptualReviewPipeline | LLM panel majority | INDEV |
| **OPTIMIZATION** | OptimizationPipeline | LLM pass | INDEV |
| **SECURITY** | SecurityPipeline | Bandit + pip-audit + LLM | INDEV |
| **FINAL_REVIEW** | FinalReviewPipeline | LIKELY/POSSIBLE majority | INDEV |
| **HUMAN_REVIEW** | — | Human accepts & merges | FINAL_REVIEW (if rejected) |
| **COMPLETED** | — | Git merge to main | — |

```
IDEA ──vote──→ PLANNING ──gate──→ INDEV ──review──→ CONCEPTUAL_REVIEW
                                                        │
                                              ┌─────────┴─────────┐
                                              ▼                   ▼
                                        OPTIMIZATION       SECURITY
                                              │               │
                                              ▼               ▼
                                         FINAL_REVIEW ──→ HUMAN_REVIEW ──→ human merge ──→ COMPLETED
```

Every demotion writes a **Performance Improvement Plan** (PIP) — hard requirements the agent must satisfy on retry. A card demoted twice addresses both failure reasons explicitly.

---

## Key Features

### Agent System
- **15 agent types** — planning, implementation, review, security, optimization, research, subdivision, PIP resolution, and autonomous project resurrection
- **Turn-based LLM loops** — each agent drives one LLM through structured turns with tool use, circuit breakers, and context budget tracking
- **Best-of-N design** — parallel LLM calls with distinct personas (correctness, security, clarity, performance, architecture)
- **Named shell tools** — 18 granular tools (`run_pytest`, `run_bandit`, `git_add`, etc.) replace grouped shell entries; per-stage access controlled

### Scheduler
- **DAG-aware dispatch** — topological sort with prerequisite resolution and cycle detection
- **Capacity model** — three nested caps (per-LLM, per-node model, per-node session) enforced at every 5-second tick
- **Auto-rescue** — orphaned jobs reset, hung sessions expired, orphaned worktrees pruned
- **Wiggum Loop** — Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED

### Multi-Project Support
- Each project has its own filesystem root, default LLM, and default budget
- Tasks are scoped per-project; switching projects fully rebuilds the board state
- Architecture cards are project-scoped and injected into agent context by category

### Safety
- **Git worktree isolation** — each task gets its own independent checkout on its own `maestro/task-{id}` branch
- **Tool constraints** — no raw shell execution for most stages, path safety enforcement, binary detection
- **Loop circuit breakers** — max turns, consecutive error limits, context saturation hard stops
- **No hard deletes** — archive instead of delete everywhere; soft-delete with BFS cascade
- **Self-protection** — Maestro's own source tree is blocked from agent writes

### Observability
- **Stage Journal** — tabbed diff view, fullscreen mode, transition run cards with verdict colors
- **Diagnostics viewer** — agent sessions, budget traces, task activity, scheduler state (split into 5 modular JS files)
- **Stats page** — board-wide budget and throughput charts
- **Column Map View** — full-screen 2D radial canvas showing task hierarchy and prerequisites
- **Inbox** — persistent user notifications for intake results, transition outcomes, and pipeline events

### Intelligence
- **Big Idea subdivision** — oversized ideas decomposed into child tasks via LLM-driven subdivision with audit trail
- **Survey orchestrator** — hierarchical project summarization (Files → Directories → Modules → Project)
- **File summaries** — LLM-generated file summaries cached by SHA1, prewarmed before agent dispatch
- **Search cache** — web search results cached by (query, provider) for research agents
- **Dreamer agent** — autonomous project resurrection and maintenance

---

## Directory Structure

```
TheMaestro/
├── app/
│   ├── main.py                 # FastAPI application (~5100 lines)
│   ├── database/               # SQLAlchemy models + 10 CRUD modules
│   │   ├── models.py           # 22 SQLAlchemy model classes
│   │   ├── crud_tasks.py       # Task CRUD + history + seed
│   │   ├── crud_projects.py    # Project CRUD
│   │   ├── crud_infra.py       # LLM + Budget + ComputeNode CRUD
│   │   ├── crud_costs.py       # BudgetEntry + Expense
│   │   ├── crud_pipeline.py    # Pipeline audit tables
│   │   ├── crud_jobs.py        # Research + FileSummary jobs
│   │   ├── crud_files.py       # FileSummary + SearchCache
│   │   ├── crud_inbox.py       # InboxMessage CRUD
│   │   └── __init__.py         # Re-exports all CRUD
│   ├── agent/                  # 38 files: agents, loops, tools, pipelines
│   │   ├── loop.py             # MaestroLoop — primary implementation agent
│   │   ├── scheduler.py        # Push-first eager task scheduler
│   │   ├── planning.py         # 5-stage planning pipeline
│   │   ├── tools.py            # Agent tools with OpenAI JSON schemas
│   │   ├── worktree.py         # Git worktree isolation
│   │   ├── research.py         # Research agent with lives system
│   │   ├── intake.py           # Intake pipeline orchestrator
│   │   ├── subdivide.py        # Subdivision agent
│   │   ├── pip_resolution.py   # PIP resolution agent
│   │   ├── verdicts.py         # Vote tallying
│   │   ├── survey_orchestrator.py  # Hierarchical summarization
│   │   ├── file_summary_agent.py   # File summarization
│   │   ├── project_snapshot.py     # Project structure snapshots
│   │   ├── path_filter.py        # Path exclusion authority
│   │   └── ...                 # review, security, optimization, etc.
│   ├── migrations/             # 54 standalone SQLite migrations
│   │   ├── runner.py           # Standalone migration engine
│   │   └── versions/           # NNNN_description.py migration files
│   ├── tests/                  # 44 test files (~700 tests)
│   └── web/                    # Kanban board, diagnostics, stats, story viewer
│       ├── index.html          # Kanban board shell
│       ├── diagnostics.html    # LLM conversation viewer
│       ├── style.css           # Board styles
│       ├── diagnostics.css     # Diagnostics styles
│       └── kanban.js           # All board behaviour
├── data/
│   └── kanban.db               # SQLite database (auto-created)
├── scripts/
│   └── create_migration.py     # Migration scaffolding tool
├── mcp_server.py               # MCP server for Claude Code integration
├── mcp_tools/                  # MCP tool implementations
├── venv/                       # Python virtual environment
├── maestro.ini                 # Master config (~763 lines, 20+ sections)
├── ARCHITECTURE.md             # Full system reference
├── PRD.md                      # Product roadmap and capability matrix
├── PLAN.md                     # Sprint tracking
└── requirements.txt
```

---

## Configuration

**`maestro.ini`** is the master config file with 20+ sections:

| Section | Controls |
|---|---|
| `[llm]` | Default LLM endpoint and model |
| `[loop]` | Wiggum Loop safety limits (max turns, consecutive errors) |
| `[intake]` | Pipeline voting, research lives, tiebreaker |
| `[scheduler]` | Tick interval, dispatchable types, file summary timeouts |
| `[planning]` | Best-of-N designs, judge tokens, session timeout |
| `[planning_gate]` | Feasibility re-check, context safety margin |
| `[capacity]` | Per-endpoint capacity bounds |
| `[conceptual_review]` | 10-voter panel settings |
| `[security_review]` | 3-agent veto gate |
| `[final_review]` | 4-agent final judgment |
| `[dreamer]` | Autonomous resurrection agent |
| `[pip]` | Performance Improvement Plan resolution |
| `[subdivision]` | Max depth, retries, total sub-ideas, subdivision tools |
| `[search]` | Search provider (duckduckgo/brave), API key |
| `[monitor]` | Monitoring window duration in seconds |
| `[context_warnings]` | Context budget warning thresholds |
| `[verdicts]` | Verdict classification thresholds |
| `[search]` | Search provider and API configuration |

All sections have sensible defaults. The system runs out-of-the-box with a local Ollama or llama.cpp instance.

---

## Database Migrations

Migrations use SQLite and live in `app/migrations/versions/`. Never edit existing migrations — always add a new one.

```bash
migrate.bat status      # see applied vs pending
migrate.bat migrate     # apply pending migrations
migrate.bat reset       # DESTRUCTIVE: drop everything, re-migrate, re-seed
```

Or directly: `venv/Scripts/python.exe app/migrations/runner.py <command>`

Scaffold a new migration: `venv/Scripts/python.exe scripts/create_migration.py "your migration name"`

**Full schema reference:** See **[CLAUDE_SCHEMA.md](CLAUDE_SCHEMA.md)** — every table, column, type, nullability, and default value.

---

## API

### Tasks

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/tasks` | Get all tasks |
| GET | `/api/tasks/{id}` | Get specific task |
| GET | `/api/tasks/by-type/{type}` | Get tasks by column type |
| POST | `/api/tasks` | Create new task (include project field) |
| PUT | `/api/tasks/{id}` | Update task |
| DELETE | `/api/tasks/{id}` | Soft-delete (sets is_active=False on task + descendants) |
| POST | `/api/tasks/{id}/reorder` | Reorder within column |
| PATCH | `/api/tasks/map-positions` | Bulk-save 2D positions |

### Task Details & Pipeline Status

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/tasks/{id}/advance` | Trigger intake pipeline (IDEA→PLANNING) |
| POST | `/api/tasks/{id}/demote` | Move one stage backward (records demotion) |
| POST | `/api/tasks/{id}/set-stage` | Force to any pipeline stage (no demotion record) |
| POST | `/api/tasks/{id}/clone` | Duplicate as new IDEA |
| POST | `/api/tasks/{id}/pin` | Set position=0 (top of column) |
| GET | `/api/tasks/{id}/transition-status` | Latest transition result + vote history |
| GET | `/api/tasks/{id}/stage-summary` | Rolled-up stage status |
| GET | `/api/tasks/{id}/planning-result` | Full planning result |
| GET | `/api/tasks/{id}/component-status` | Per-component dev results |
| GET | `/api/tasks/{id}/optimization-status` | Latest optimization outcome |
| GET | `/api/tasks/{id}/security-status` | Security reviewer verdicts |
| GET | `/api/tasks/{id}/final-review-status` | Final review verdicts |
| GET | `/api/tasks/{id}/merge-status` | Branch name + merge commit SHA |
| GET | `/api/tasks/{id}/diff` | Git diff for task branch |
| GET | `/api/tasks/{id}/children` | Direct child tasks |
| GET | `/api/tasks/{id}/subdivision-records` | Subdivision audit trail |
| GET | `/api/tasks/{id}/pips` | Full PIP list with verification history |
| GET | `/api/tasks/{id}/research-jobs` | Research jobs for this task |
| GET | `/api/tasks/{id}/agent-sessions` | All agent sessions |
| GET | `/api/tasks/{id}/history` | Task history |

### Pipeline Triggers

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/tasks/{id}/run-planning` | Trigger planning pipeline |
| POST | `/api/tasks/{id}/run-review` | Trigger conceptual review |
| POST | `/api/tasks/{id}/run-security` | Trigger optimization + security |
| POST | `/api/tasks/{id}/run-final-review` | Trigger final review |

### Agent Control

| Method | Endpoint | Description |
|---|---|---|
| POST | `/api/agent/run/{id}` | Start MaestroLoop (background) |
| GET | `/api/agent/status/{id}` | Loop status |
| POST | `/api/agent/stop/{id}` | Stop active agent session |
| GET | `/api/agent/tasks/ready` | DAG-ready tasks |

### Projects & Infrastructure

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/projects` | List projects |
| POST | `/api/projects` | Create project |
| PUT | `/api/projects/{name}` | Update project |
| DELETE | `/api/projects/{name}` | Delete project |
| GET | `/api/projects/{name}/tasks` | All tasks for a project |
| GET | `/api/projects/{name}/arch-gen-jobs` | Pending arch gen jobs |
| CRUD | `/api/llms` | LLM endpoint management |
| CRUD | `/api/budgets` | Budget management |
| CRUD | `/api/compute-nodes` | Compute node management |

### Budget & Observability

| Method | Endpoint | Description |
|---|---|---|
| GET | `/api/budget-entries` | Budget entry listing |
| GET | `/api/budget-entries/{id}/full` | Full entry with prompt/response |
| GET | `/api/budgets/{id}/summary` | Aggregated budget usage |
| GET | `/api/diagnostics/tasks` | Tasks with LLM activity |
| GET | `/api/scheduler/status` | Scheduler state |

Full interactive docs: **http://localhost:8000/docs**

---

## MCP Integration

The Maestro exposes **20+ MCP tools** for Claude Code (configured in `.mcp.json`):

### Diagnostic & Orientation

| Tool | Purpose |
|---|---|
| `get_project_health(project?)` | Stage counts, active sessions, spend, demotions, pending merges |
| `get_capacity_status()` | Per-node/LLM used/free/total table |
| `list_pending_merges(project?)` | Completed tasks with no merge_commit_sha |
| `diagnose_task(task_id)` | Complete picture of why a task is stuck |
| `get_scheduler_state()` | DB scheduler state |
| `get_scheduler_api_status()` | Live API scheduler state |
| `find_stuck_tasks(idle_minutes)` | Tasks with no recent LLM activity |

### Budget & Traces

| Tool | Purpose |
|---|---|
| `get_budget_trace(task_id, n)` | Raw LLM call history |
| `get_budget_entry_full(entry_id)` | Full prompt/response for one call |
| `get_gate_history(task_id)` | Planning gate failure history |
| `get_planning_result(task_id)` | Full plan content |

### Task Actions

| Tool | Purpose |
|---|---|
| `list_tasks(project, type)` | List tasks by project or type |
| `append_task_description(task_id, text)` | Add scope note to task |
| `set_task_type(task_id, stage)` | Force task to a pipeline stage |
| `demote_task(task_id, target?)` | Move task backward with demotion record |
| `trigger_planning_run(task_id)` | Trigger planning pipeline |
| `run_pipeline_stage(task_id, stage)` | Trigger review/security/final_review |
| `stop_agent(task_id)` | Stop a running MaestroLoop |
| `patch_planning_fields(result_id, fields)` | Fix interface_contracts / file_manifest |

### Monitoring

| Tool | Purpose |
|---|---|
| `monitor(duration_seconds)` | Blocks N seconds, returns activity diff + pattern flags |

### Utilities

| Tool | Purpose |
|---|---|
| `restart_server()` | Drain sessions, restart server |
| `run_inspect_cards(section, extra)` | Run inspect_cards.py diagnostics |

### Default Monitoring Workflow

```
/loop                    # Start loop
```

Each iteration calls `monitor()` with no arguments, blocks for the window defined in
`maestro.ini [monitor] duration_seconds` (default 5 minutes), then returns a structured
report with five pattern flags: `rapid_cycling`, `token_limited`, `zombie_sessions`, `stage_thrash`, `tool_call_storms`.

---

## Testing

```bash
# Run all tests (uses test database via MAESTRO_TEST_DB)
python -m pytest app/tests/ -v

# Run a specific test module
python -m pytest app/tests/test_tools.py -v

# Run a single test
python -m pytest app/tests/test_tools.py -k "test_safety" -v
```

**~700 tests** across 44 files covering pipeline, agents, scheduler, DAG resolution, tool safety, LLM client resilience, subdivision, survey orchestration, PIP resolution, and more.

Tests use a separate `data/test.db` via `MAESTRO_TEST_DB`. Database initialization tests use `tmp_path` + `importlib.reload` for full isolation. LLM calls are always mocked.

---

## Diagnostics

When cards are stuck, use these diagnostic tools:

### CLI Inspect Cards

```bash
venv/Scripts/python.exe scripts/inspect_cards.py                  # Overview: all cards, transitions, subdivision records
venv/Scripts/python.exe scripts/inspect_cards.py prereqs          # Prerequisite chain analysis
venv/Scripts/python.exe scripts/inspect_cards.py scheduler        # Simulated scheduler state
venv/Scripts/python.exe scripts/inspect_cards.py activity         # Recent LLM activity timeline
venv/Scripts/python.exe scripts/inspect_cards.py votes            # Transition vote detail
venv/Scripts/python.exe scripts/inspect_cards.py budget           # LLM capacity and budget summary
venv/Scripts/python.exe scripts/inspect_cards.py children         # Parent-child tree
venv/Scripts/python.exe scripts/inspect_cards.py all              # Run all sections
```

### MCP Diagnose

```bash
# In Claude Code:
mcp__maestro__diagnose_task(task-1746000000.123)
```

---

## Troubleshooting

### Port Already in Use
```bash
python -m uvicorn app.main:app --port 8002
```

### Server Won't Start — Import Errors
```bash
venv/Scripts/activate
pip install -r requirements.txt
```

### Stuck Cards — Use MCP Diagnose
```bash
# In Claude Code:
mcp__maestro__diagnose_task task-1746000000.123
```

### MCP Tool Calls Hang
MCP tools are synchronous and make direct SQLite calls. If the scheduler is holding a write lock, reads can queue behind it.
- **Quick fix:** Wait a few seconds and retry
- **Nuclear option:** `mcp__maestro__restart_server()` — drains sessions, clears lock contention
- **Fallback:** `venv/Scripts/python.exe scripts/inspect_cards.py <section>` bypasses the MCP layer

### Reset Database
```bash
del data\kanban.db
python -c "from app.database import init_db; init_db()"
```

---

## Documentation

| Document | Purpose |
|---|---|
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system reference — topology, pipeline, agents, safety, data |
| [PRD.md](PRD.md) | Product roadmap, shipped capabilities, forward themes |
| [PLAN.md](PLAN.md) | Sprint tracking and current work |
| [CLAUDE.md](CLAUDE.md) | Agent-facing reference — key files, patterns, conventions |
| [CLAUDE_SCHEMA.md](CLAUDE_SCHEMA.md) | Complete database schema reference |
| [app/agent/CLAUDE.md](app/agent/CLAUDE.md) | Agent system per-file descriptions |
| [app/database/CLAUDE.md](app/database/CLAUDE.md) | Database layer per-file descriptions |
| [app/web/CLAUDE.md](app/web/CLAUDE.md) | Frontend per-file descriptions |
| [app/tests/CLAUDE.md](app/tests/CLAUDE.md) | Test conventions and isolation patterns |

---

## License

Internal project. All rights reserved.

---

*TheMaestro — Humans describe. Machines build.*
