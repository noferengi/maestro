# Gap 10 — Multi-model routing by task type

**Status:** Complete  
**Effort:** Small-Medium  
**Priority:** Medium — reduces cost and latency; enables model specialization

---

## Problem

The scheduler dispatches tasks to LLM endpoints based on capacity (free slots) and
project configuration (`project.llm_id`). There is no dispatch logic based on what kind
of task it is. A file summary job and a formal proof attempt both go to the same endpoint.
A simple formatting correction and a complex architectural decision consume the same model.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Capability declarations** | Manual tags on each LLM record via UI. Tags: `reasoning`, `code`, `math`, `fast`, `long_context`, `cheap`. Operator-declared, stored in `llms.capabilities` JSONB. |
| **Routing policy location** | Per-project routing table — `project_llm_routing` table: `(project_id, stage_key, llm_id)`. Overrides `project.llm_id` for matching stages. |
| **Fallback** | Hard routing: tasks wait for their assigned model. To prevent silent starvation, a max-wait timeout (configurable, default 30 minutes) marks the task `blocked_on_model` and surfaces it in project health for human review. |
| **Override order** | `project_llm_routing[stage_key]` > `project.llm_id` > any available endpoint. A human-pinned `task.llm_id` (set via UI) always overrides all routing — it is treated as an explicit override, not a default. |

---

## Implementation plan

### Phase 1 — `capabilities` field on `llms` table

**Migration** (`NNNN_llm_capabilities.py`):

```sql
ALTER TABLE llms ADD COLUMN capabilities JSONB NOT NULL DEFAULT '[]';
-- e.g. ["reasoning", "code", "long_context"]

ALTER TABLE llms ADD COLUMN supports_tools BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE llms ADD COLUMN supports_vision BOOLEAN NOT NULL DEFAULT false;
```

Valid capability tags (enforced in application logic, not DB):

| Tag | Meaning |
|---|---|
| `reasoning` | Strong multi-step logical reasoning and planning |
| `code` | Code generation, debugging, refactoring |
| `math` | Symbolic and formal mathematics |
| `fast` | Low latency, optimized for short tasks |
| `long_context` | Context window > 32K tokens |
| `cheap` | Low cost per token; prefer for bulk/mechanical tasks |

**UI** — LLM edit form gains a capability tag multi-select (checkboxes). Tags are saved
as a JSON array to `llms.capabilities`. `supports_tools` and `supports_vision` are booleans
in the same form.

---

### Phase 2 — Per-project routing table

**Migration** (`NNNN_project_llm_routing.py`):

```sql
CREATE TABLE project_llm_routing (
    id         SERIAL PRIMARY KEY,
    project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    stage_key  TEXT    NOT NULL,
    llm_id     INTEGER NOT NULL REFERENCES llms(id) ON DELETE CASCADE,
    UNIQUE (project_id, stage_key)
);
```

**`app/database/crud_projects.py`** — add:
- `get_routing_table(project_id, db) -> dict[str, int]` — returns `{stage_key: llm_id}`.
- `upsert_routing_entry(project_id, stage_key, llm_id, db)` — insert or replace.
- `delete_routing_entry(project_id, stage_key, db)` — remove a stage override.

**API routes** (`app/main.py`):
```
GET    /api/projects/{name}/routing          — full routing table
PUT    /api/projects/{name}/routing/{stage}  — upsert entry { "llm_id": N }
DELETE /api/projects/{name}/routing/{stage}  — remove entry
```

**UI** — Project settings panel gains a "Model Routing" section: a table showing all
pipeline stages in the project's active template, with an LLM picker per row. Empty
means "use project default." Cleared entries fall back to `project.llm_id`.

---

### Phase 3 — Routing resolution in the scheduler

**`app/agent/config.py`** — add:

```python
def resolve_llm_for_task(task, stage_key: str, db, settings) -> int:
    """
    Returns the llm_id to use for dispatching this task at this stage.
    Resolution order:
      1. task.llm_id if human-pinned (task.llm_pinned = True)
      2. project_llm_routing[stage_key]
      3. project.llm_id
      4. settings.default_llm_id (ini fallback)
    """
    if task.llm_pinned and task.llm_id:
        return task.llm_id

    routing = get_routing_table(task.project_id, db)
    if stage_key in routing:
        return routing[stage_key]

    project = get_project(task.project_id, db)
    if project.llm_id:
        return project.llm_id

    return settings.default_llm_id
```

**`app/database/models.py`** — add `llm_pinned: bool = False` to `Task`. When a human
sets `llm_id` via the task edit UI, `llm_pinned` is set to `True`. Auto-assigned LLM IDs
(from project default or routing) set `llm_pinned = False`.

**Migration** (`NNNN_task_llm_pinned.py`):
```sql
ALTER TABLE tasks ADD COLUMN llm_pinned BOOLEAN NOT NULL DEFAULT false;
```

---

### Phase 4 — Hard routing with blocked_on_model timeout

**`app/agent/scheduler.py`** — extend dispatch logic:

```python
def _try_dispatch_task(task, db, settings):
    required_llm_id = resolve_llm_for_task(task, task.type, db, settings)
    endpoint = _get_free_endpoint(required_llm_id)

    if endpoint is None:
        # Hard routing: do not fall back to another model.
        _check_model_block_timeout(task, required_llm_id, db, settings)
        return False  # defer to next tick

    _dispatch(task, endpoint, db, settings)
    return True

def _check_model_block_timeout(task, required_llm_id, db, settings):
    """
    If a task has been waiting for a specific model longer than the timeout,
    mark it blocked_on_model and surface it in project health.
    """
    if task.dispatch_waiting_since is None:
        mark_dispatch_waiting(task.id, db)
        return

    wait_minutes = (datetime.utcnow() - task.dispatch_waiting_since).seconds / 60
    if wait_minutes >= settings.model_block_timeout_minutes:
        set_task_blocked_on_model(task.id, required_llm_id, db)
        # Surface in project health — human must intervene (fix the endpoint or
        # remove the routing entry to allow fallback).
```

**Schema additions** (`NNNN_task_dispatch_waiting.py`):
```sql
ALTER TABLE tasks ADD COLUMN dispatch_waiting_since TIMESTAMPTZ NULL;
ALTER TABLE tasks ADD COLUMN blocked_on_model_id    INTEGER NULL REFERENCES llms(id);
```

**`maestro.ini`** — add under `[scheduler]`:
```ini
model_block_timeout_minutes = 30
```

**Project health** (`get_project_health` MCP tool) — includes a `blocked_on_model` section
listing tasks waiting longer than 5 minutes for a specific endpoint. Gives the operator
clear visibility before the hard timeout fires.

---

### Phase 5 — UI: routing table in project settings

The project settings panel's "Model Routing" table:

```
Stage               Assigned Model          (default: Qwen 35B)
──────────────────────────────────────────────────────────────
PLANNING            [Claude Sonnet ▾]       ← reasoning
INDEV               [Qwen 35B ▾]            ← code
SECURITY            [Claude Opus ▾]         ← reasoning + code
FINAL_REVIEW        [Qwen 35B ▾]
HUMAN_REVIEW        (no model needed)       ← greyed out
```

- Each row shows the stage key, an LLM picker (same component as the project LLM picker),
  and a "clear" button to revert to project default.
- Stages of type `human_review` and `verifier` show "(no model)" and are non-editable.
- The table is populated from the active pipeline template's stages.

---

### Phase 6 — Cost accounting by model

**`budget_entries`** already records `llm_id` per entry. No schema change needed.

New API route: `GET /api/projects/{name}/cost-by-model`:
```json
{
  "by_model": [
    {"llm_id": 1, "model_name": "qwen-35b", "total_tokens": 1234567, "total_cost_usd": 0.12},
    {"llm_id": 3, "model_name": "claude-sonnet", "total_tokens": 89012, "total_cost_usd": 4.56}
  ],
  "by_stage": [
    {"stage_key": "PLANNING", "llm_id": 3, "total_cost_usd": 2.34},
    {"stage_key": "INDEV",    "llm_id": 1, "total_cost_usd": 0.08}
  ]
}
```

Displayed as a cost breakdown section in the project settings panel.

---

### Phase 7 — Tests

1. **Unit** — `resolve_llm_for_task`: human-pinned task returns `task.llm_id` regardless of routing table; routing table entry wins over project default; project default wins over nothing; ini fallback last.
2. **Unit** — `_check_model_block_timeout`: task waiting under threshold → no action; task waiting over threshold → `blocked_on_model` set.
3. **Unit** — routing table CRUD: upsert, get, delete via API; `UNIQUE (project_id, stage_key)` enforced.
4. **Unit** — `llm_pinned` flag: setting llm_id via task edit UI sets `llm_pinned = True`; routing assignment sets `llm_pinned = False`.
5. **Integration** — full dispatch with routing: task at `PLANNING` stage dispatched to routing-assigned LLM, not project default.
6. **Integration** — hard routing stall: preferred LLM at capacity → task deferred → `dispatch_waiting_since` set → appears in project health.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_llm_capabilities.py` | `capabilities` JSONB + `supports_tools` + `supports_vision` on `llms` |
| `app/migrations/versions/NNNN_project_llm_routing.py` | `project_llm_routing` table |
| `app/migrations/versions/NNNN_task_llm_pinned.py` | `llm_pinned` + `dispatch_waiting_since` + `blocked_on_model_id` on `tasks` |
| `app/database/crud_projects.py` | Routing table CRUD |
| `app/database/models.py` | `ProjectLlmRouting` model; `llm_pinned`, `dispatch_waiting_since`, `blocked_on_model_id` on `Task` |
| `app/agent/config.py` | `resolve_llm_for_task`; `model_block_timeout_minutes` from ini |
| `app/agent/scheduler.py` | Hard routing dispatch; `_check_model_block_timeout` |
| `app/main.py` | Routing CRUD routes; `/cost-by-model` route |
| `app/web/` | LLM capability tags in LLM edit form; routing table in project settings; cost breakdown panel |
| `app/tests/test_model_routing.py` | **New file** — all tests for this gap |

---

## Acceptance criteria

- [x] LLM records can be tagged with capability strings via the UI; tags are stored in `llms.capabilities`.
- [x] A per-project routing table maps stage keys to specific LLM IDs via the project settings UI.
- [x] `resolve_llm_for_task` correctly implements the resolution order: human-pin > routing table > project default > ini fallback.
- [x] Tasks wait for their routed model without falling back to alternatives.
- [x] Tasks waiting longer than `model_block_timeout_minutes` are marked `blocked_on_model` and surface in project health.
- [x] Cost-by-model breakdown is accessible per project via API and UI.
- [x] All new code passes existing test suite with no regressions (11/11 new tests pass; 89 pre-existing failures unchanged).
