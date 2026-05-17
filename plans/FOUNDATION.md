# FOUNDATION.md: TheMaestro Project Knowledge Base

## Project Identity

- **Name**: TheMaestro
- **Type**: Kanban board with an agentic LLM orchestration backend
- **Location**: `D:\workspace\TheMaestro`
- **Stack**: FastAPI + Uvicorn (backend), Vanilla JS + HTML (frontend), PostgreSQL (database), llama.cpp / OmniCoder 9B (agent LLM)

---

## Current File Tree

```
D:\workspace\TheMaestro\
├── app/
│   ├── agent/
│   │   ├── __init__.py
│   │   ├── config.py         # Constants: LLM endpoint, MAX_TURNS=100, limits, paths
│   │   ├── dag.py            # DAGResolver: Kahn's topological sort, ready-task finder, cycle detection
│   │   ├── loop.py           # MaestroLoop class; _ACTIVE_LOOPS/_LOOP_STATUS dicts
│   │   ├── system_prompt.py  # MAESTRO_SYSTEM_PROMPT
│   │   └── tools.py          # 16 tools with OpenAI JSON schemas + dispatch_tool()
│   ├── migrations/
│   │   ├── runner.py         # Migration engine (psycopg2, uses MAESTRO_ADMIN_DATABASE_URL)
│   │   └── versions/
│   │       ├── 0001_initial_schema.py
│   │       ├── 0002_add_prerequisites.py
│   │       └── 0003_add_project_field.py
│   ├── models/
│   │   └── dags.py
│   ├── scripts/
│   │   └── reset_database.py
│   ├── services/
│   │   └── repl.py
│   ├── tests/
│   │   ├── test_config.py
│   │   ├── test_integration.py
│   │   └── test_repl.py
│   ├── web/
│   │   ├── index.html        # Board shell: project tabs, five columns, create/edit modals
│   │   ├── kanban.js         # All board behaviour (taskData, rendering, drag-and-drop)
│   │   └── style.css         # All styles
│   ├── database.py           # SQLAlchemy Task model + all DB functions
│   └── main.py               # FastAPI app, all routes, mounts static files from app/web/
├── data/
│   └── test.db               # SQLite test database (auto-created by test suite)
├── venv/                     # Python virtual environment
├── AGENTS.md
├── ARCHITECTURE.md
├── CLAUDE.md
├── FOUNDATION.md
├── migrate.bat               # Migration helper: status | migrate | reset
├── requirements.txt
└── SUMMARY.md
```

---

## Task Data Structure

This is the canonical shape of a task — both in the PostgreSQL DB (via SQLAlchemy) and as returned by the API.

```javascript
{
    id:            string,          // e.g. "task-1714000000.123"
    title:         string,
    type:          string,          // status: 'architecture' | 'planning' | 'development' | 'review' | 'completed'
    description:   string | null,
    owner:         string,          // default: "user"
    tags:          string[],        // e.g. ["backend", "setup"]
    content:       object | null,   // architecture tasks only: {frontend, backend, database, style, ...}
    history:       [{status: string, timestamp: ISO8601}],
    prerequisites: string[],        // task IDs that must be completed before this task is READY
    position:      integer,         // sort order within column (0 = first)
    project:       string,          // project scope, default: "TheMaestro"
    created_at:    ISO8601,
    updated_at:    ISO8601
}
```

### Column Status Mapping

| Column | `type` value | Notes |
|--------|-------------|-------|
| ARCHITECTURE | `architecture` | Immutable; seeded once |
| PLANNING | `planning` | |
| DEVELOPMENT | `development` | |
| REVIEW | `review` | |
| COMPLETED | `completed` | |

---

## Agent Tool Safety Contract

These invariants are enforced at the tool level in `app/agent/tools.py` — any agent operating in this codebase must respect them:

1. **No hard deletes.** `archive_file()` moves files to `.archive/YYYY-MM-DD_HH-MM-SS/`. Never use `rm`, `del`, or `shutil.rmtree`.
2. **Branch enforcement.** `git_checkout` blocks any branch that is not `maestro/task-{id}`, `main`, or `master`.
3. **Shell blocklist.** `run_shell()` rejects patterns including `rm -rf`, `del /s`, fork bombs, and deep `../` traversal.
4. **Failure circuit-breaker.** After 3 consecutive tool failures, `MaestroLoop` emits `{"signal": "REVERT_TO_DESIGN"}` and halts.
5. **Turn cap.** `MAX_TURNS=100` in `app/agent/config.py` terminates runaway loops.

---

## The Maestro Philosophy

This Kanban board is the UI face of an **Agentic Orchestration System** with strict design/implementation separation.

### Dual-Artifact System

- **Blueprint (Design)**: Stored in `.md` files — `ARCHITECTURE.md`, `PRD.md`, `*/AGENTS.md`
- **Product (Implementation)**: The actual source code, tests, and assets

### The "Wiggum" Loop

A persistent Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until all DAG task nodes reach ACCEPTED.

### Failure Protocol

After 3 implementation failures, the system triggers a **REVERT_TO_DESIGN** signal, moving the task back to Phase A.

### Design Satisfaction

Tasks don't move to COMPLETED until both:
1. Tests pass (exit code 0)
2. LLM verification confirms implementation matches design

---

## Agent Specialization

| Agent Type | Capabilities | Permissions |
|------------|--------------|-------------|
| **Planning Agent** | Create/Edit Markdown, Manage DAG/Kanban | Read Source, Write Markdown |
| **Coding Agent** | Write/Edit Source Code | Read Markdown, Write Source |
| **Debugging Agent** | Execute Tests, Static Analysis | Read Source, Read Markdown, NO WRITE |
| **Research Agent** | Tool-based search, MCP documentation fetch | Read-Only |

---

## Design → Implementation Flow

1. **Phase A: Design Loop** — Solve the problem in Markdown (`AGENTS.md`)
2. **Phase B: Implementation Loop** — Realize the design in code
3. **Verification** — Tests pass → git commit on `maestro/task-{id}` → advance DAG
4. **Failure** — Tests fail → "Advice Context" → retry OR emit `REVERT_TO_DESIGN`
