# app/database — Database Layer

PostgreSQL-backed persistence for Maestro (SQLite only in tests via `MAESTRO_TEST_DB`).
All SQLAlchemy models and CRUD functions live here.

## Import contract

Every public name is re-exported from `__init__.py`.  All callers use:

```python
from app.database import Task, get_task, init_db, ...
import app.database as db_mod   # monkeypatching in tests
from database import Task, ...  # path-relative (app/ on sys.path)
```

**Never import directly from a submodule** (e.g. `from app.database.crud_tasks
import get_task`).  Always go through `app.database`.  This ensures test
monkeypatching on `app.database.X` works correctly.

## File map

| File | Responsibility | ~Lines |
|---|---|---|
| `session.py` | `DATABASE_PATH`, `engine`, `Base`, `SessionLocal`, `get_db`, `init_db_tables` | 60 |
| `models.py` | All 22 SQLAlchemy model classes | 365 |
| `crud_tasks.py` | Task CRUD + history + reorder + seed + subdivision traversal + `init_db` | 380 |
| `crud_projects.py` | Project CRUD + `get_project_path` | 100 |
| `crud_infra.py` | LLM + Budget + ComputeNode CRUD | 230 |
| `crud_costs.py` | BudgetEntry + Expense + budget math helpers | 130 |
| `crud_pipeline.py` | All pipeline audit tables (votes, planning, component, optimization, security, full review, merge, subdivision records) | 340 |
| `crud_jobs.py` | ResearchJob + FileSummaryJob + OptimizationBenchmark | 220 |
| `crud_files.py` | FileSummary + SearchCache | 100 |
| `crud_inbox.py` | InboxMessage CRUD (create, list, get, mark read, mark all read, delete, count unread) | 90 |
| `crud_malleable.py` | Pipeline templates, stages, transitions, groups, arch categories, custom agent defs, system_settings, project_settings — 50+ functions including clone/export/import and `load_custom_agents_into_registry()` | 600+ |
| `crud_documents.py` | `project_documents` table: upsert, exact get, pg_trgm fuzzy get, list, soft-delete; both project-name and project-ID wrappers | 316 |
| `crud_factory.py` | `factory_runs` audit table CRUD + helpers for checking predecessor triggers and cron scheduling | 150 |
| `__init__.py` | Re-exports everything from the above modules | 160 |

## Dependency graph (no cycles)

```
session.py
    ↑
models.py          (imports Base from session)
    ↑
crud_*.py          (import SessionLocal from session, models from models)
    ↑
crud_costs.py      (also imports get_budget from crud_infra — local import inside function)
    ↑
__init__.py        (imports from all of the above)
```

## Key design rules

- **`init_db()` lives in `crud_tasks.py`**, not `session.py`.  It queries the
  `Task` model to check if the DB is fresh; putting it in `session.py` would
  create a circular import (`session → models → session`).

- **`DATABASE_PATH` uses `../..`** relative to `session.py` because
  `session.py` is one level deeper than the original `database.py` was.
  The resolved path is always `<repo_root>/data/kanban.db`.

- **`update_planning_result`, `update_optimization_result`,
  `update_security_review_result`, `update_final_review_result`,
  `update_merge_record`** accept an explicit `db` session as their first
  argument.  These are called from within long-running pipeline transactions
  where the session is already open.  All other CRUD functions manage their
  own sessions internally.

- **`upsert_project` uses Ellipsis (`...`) as a sentinel** for `llm_id` and
  `budget_id`: pass `...` (the default) to leave the existing value unchanged,
  pass `None` to explicitly clear it, pass an int to set it.

- **`create_file_summary` uses INSERT-then-catch** for race-safe concurrent
  inserts.  Two agents summarising the same file simultaneously will not crash;
  the second one just reads back the row the first one wrote.

- **`get_budget_remaining_microcents`** calls `get_budget()` from
  `crud_infra.py` via a local import inside the function body to avoid a
  module-level cross-import.

## Models at a glance

**Infrastructure / config**
- `ComputeNode` — physical/virtual host that one or more LLM endpoints run on (`name`, `description`, `max_parallel_sessions`); enforces a node-level concurrency cap in the scheduler
- `LLM` — endpoint config (address, port, model, cost rates, optional `compute_node_id` FK)
- `Budget` — spending limit config (`dollar_amount == -1` = infinite)
- `Project` — name → filesystem path registry

**Core Kanban data**
- `Task` — the task card (type, history, prerequisites, map_x/y, ...)

**Cost tracking**
- `BudgetEntry` — one row per LLM call; stores full prompt + response JSON
- `Expense` — one row per LLM call; stores µ¢ cost breakdown

**Pipeline audit (write-once)**
- `TransitionVote` / `TransitionResult` — intake pipeline voting
- `SubdivisionRecord` — subdivision attempt audit trail
- `PlanningResult` — planning pipeline output
- `ComponentResult` — per-component dev agent result
- `OptimizationResult` — optimization pipeline output
- `SecurityReviewResult` — security review findings
- `FinalReviewResult` — final review findings
- `MergeRecord` — merge-to-main operations

**Background jobs**
- `ResearchJob` — scheduler-dispatched research (priority 0.0)
- `FileSummaryJob` — scheduler-dispatched file summaries (priority -1.0, blocks caller)
- `OptimizationBenchmark` — before/after profiling metrics

**Caches**
- `FileSummary` — LLM-generated file summaries keyed by (sha1, size)
- `SearchCache` — web search result cache keyed by (query, provider)

**Inbox / notifications**
- `InboxMessage` — persistent user notification (id=UUID, subject, source_type, task_id, task_title, outcome, data_json, read, created_at). No FK on task_id — the task may be deleted but the message should survive. `data_json` stores the full transition-status payload snapshot so the message can be re-rendered in the transition modal without re-fetching.

**Malleable pipeline (added Phase 1–10)**
- `PipelineTemplate` — named reusable workflow graph (`name`, `description`, `is_default`, `is_builtin`, `version`)
- `PipelineStage` — one stage node: `stage_key`, `label`, `agent_type`, `position`, `group_id`, `config` (JSONB: gate, retries, intent, system_prompt, tool_allowlist, required_input_keys, output_keys, verifier, arch_category_keys)
- `PipelineTransition` — directed edge: `from_stage_id → to_stage_id`, `condition`, `priority`
- `PipelineStageGroup` — visual bracket around related stages
- `PipelineArchCategory` — per-template arch card category (replaces hardcoded JS dict)
- `CustomAgentDefinition` — user-defined agent: `name`, `system_prompt`, `allowed_tools` (JSONB), `gate_type`, `verifier`, `verifier_cmd`
- `ProjectDocument` — shared document store: `(project_id, key)` unique; `content`, `tags` (JSONB), `written_by_task_id`, soft-delete via `deleted_at`
- `ArchivedFile` — deletion-protection audit: `task_id`, `original_path`, `archive_path` (relative to project root), `deleted_at`, `restored_at`
- `SystemSetting` — global key/value settings (`maestro_autopilot`, `autopilot_start_hour`, `autopilot_stop_hour`)
- `ProjectSetting` — per-project key/value overrides (e.g. `autopilot_override`)
- `FactoryRun` — factory trigger audit: `factory_stage_id`, `project_id`, `trigger_type`, `trigger_card_id`, `cards_created`, `status`
