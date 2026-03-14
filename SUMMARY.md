# Project Maestro — Summary

## What This Is

A Kanban board that doubles as the control surface for an agentic LLM orchestration system. The board is real and functional. The agent backend is scaffolded and ready to wire up. The vision is a "Wiggum Loop" — a persistent Do-While that drives a local LLM through Design → Implement → Test → Verify cycles until every task in the DAG reaches ACCEPTED.

The LLM target is OmniCoder 9B (Qwen 3.5 base) running via llama.cpp on `localhost:8008`, OpenAI API compatible.

---

## Current File Structure

```
app/
├── main.py              FastAPI app, all routes
├── database.py          SQLAlchemy Task model, all DB functions
├── agent/
│   ├── config.py        LLM endpoint, safety constants
│   ├── tools.py         16 safe tools + OpenAI schemas + dispatch_tool()
│   ├── system_prompt.py MAESTRO_SYSTEM_PROMPT
│   ├── loop.py          MaestroLoop (the Wiggum engine)
│   └── dag.py           DAGResolver (Kahn's sort, cycle detection)
├── migrations/
│   ├── runner.py        Standalone sqlite3 migration engine
│   └── versions/
│       ├── 0001_initial_schema.py
│       ├── 0002_add_prerequisites.py
│       └── 0003_add_project_field.py
└── web/
    ├── index.html       Board UI shell
    ├── kanban.js        All frontend behaviour
    └── style.css        All styles
data/
└── kanban.db            SQLite database
migrate.bat              Thin wrapper: migrate.bat [migrate|status|reset|rollback]
```

---

## What's Working

### Kanban Board
- Five columns: ARCHITECTURE, PLANNING, DEVELOPMENT, REVIEW, COMPLETED
- Per-project task isolation — switching projects fetches only that project's tasks
- Project Alpha and Beta start empty; TheMaestro is seeded with 10 sample tasks
- New projects created via modal (no more `window.prompt()`)
- Task creation modal with architecture-specific content fields
- Task editing, history tracking, proof-of-work timeline

### Drag-and-Drop (fully rewritten twice, now correct)
- HTML5 native drag events — no mouse tracking
- Ghost rectangle (dashed blue, card-shaped) inserts into DOM flow between cards, pushing neighbours apart with a 120ms open animation
- Ghost suppressed when hovering the card's own slot; card restores to full opacity to signal "no-op drop"
- Drop POSTs to `/api/tasks/{id}/reorder`, then re-fetches full project task list from server before re-rendering — positions are always authoritative from DB
- Immediate visual update on drop, no F5 required

### Database & Migrations
- SQLite via SQLAlchemy
- Custom migration runner (`migrate.bat`) — no Alembic dependency
- Migrations are immutable, ordered, forward-only in practice
- `reset` command: drops everything, re-runs all migrations, re-seeds TheMaestro tasks
- Task schema: `id, title, type, description, owner, tags, content, history, position, prerequisites, project, created_at, updated_at`

### Agent Backend (scaffolded, not yet wired to UI)
- `MaestroLoop` — async Do-While, talks to llama.cpp, dispatches tool calls, tracks consecutive errors, emits terminal JSON signals
- 16 tools: file read/write/append/search/glob, `archive_file` (soft delete — moves to `.archive/`, never hard deletes), `run_shell` (blocklisted destructive patterns, 30s timeout, project-root containment), git tools (branch enforced to `maestro/task-*`), Kanban task tools
- DAGResolver — topological sort, ready-task finder, cycle detection
- FastAPI endpoints: `POST /api/agent/run/{task_id}`, `GET /api/agent/status/{task_id}`, `POST /api/agent/stop/{task_id}`, `GET /api/agent/tasks/ready`
- Failure protocol: 3 consecutive tool errors → `{"signal": "REVERT_TO_DESIGN"}` and halt

---

## Known Issues / Next Up

| Item | Notes |
|------|-------|
| Wire agent loop to board UI | "Run with Maestro" button per task, live status polling |
| Task prerequisites UI | Column exists in DB, not yet editable in the board |
| Rename / delete projects | Tab UI exists, no rename/delete actions yet |
| Move tasks between columns | Currently manual via buttons; no drag-across-column |
| Agent REVERT_TO_DESIGN flow | Signal exists, board doesn't react to it yet |
| Context window handler | RAG-lite / sliding window for long agent runs |
| Design Validator | LLM gate before coding agents are unleashed |

---

## Running Locally

```bash
# Start server
venv\Scripts\python.exe -m uvicorn app.main:app --port 8000

# Database
migrate.bat status
migrate.bat migrate
migrate.bat reset      # destructive — drops and re-seeds
```

Board: `http://localhost:8000`
Agent LLM: `http://localhost:8008/v1` (llama.cpp, OmniCoder 9B)
