# CLAUDE_PIPELINE.md — Malleable Pipeline System Reference

> Load this file when working on: pipeline templates, the Litegraph editor, agent
> registry, card factory, document store, workspace isolation, or autopilot/mission.
> For the schema tables specifically, see `CLAUDE_SCHEMA.md`.

---

## 1. Core Concepts

**Pipeline Template** — a named, reusable directed graph of stage nodes and transition
edges stored in `pipeline_templates`. Every project references exactly one template via
`projects.pipeline_template_id`. Built-in templates (`is_builtin=TRUE`) cannot be deleted
but can be cloned.

**Stage Node** — one row in `pipeline_stages`; maps to one kanban column. Key fields:
`stage_key` (routing id), `label` (display), `agent_type` (registry key), `position`
(column order), `group_id` (optional bracket), `config` (JSONB with gate, retries,
intent, system_prompt, tool_allowlist, required_input_keys, output_keys, verifier,
arch_category_keys, upstream_task_gate).

**Transition Edge** — one row in `pipeline_transitions`: `from_stage_id → to_stage_id`
with `condition IN ('pass','fail','reject','always','skip')` and `priority`. The
scheduler follows the highest-priority edge whose condition matches the agent's verdict.

**Stage Group** — `pipeline_stage_groups` rows; visual brackets around related stages
(e.g. the Optimization+Security parallel group in Software Development).

**task.type → stage_key migration** — `tasks.type` is kept in sync with `tasks.stage_key`
for backward compatibility. `pipeline_router.advance_stage()` writes both atomically.
The goal is to eventually deprecate `type` in favor of `stage_key`; ~15 exit paths in
`scheduler.py` still write `type` directly and are flagged for cleanup.

---

## 2. Built-in Templates

Seeded by migration 0073. All have `is_builtin=TRUE`.

| Template | Key Stages |
|---|---|
| Software Development | idea → planning → indev → conceptual_review → [optimization \|\| security] → final_review → human_review → completed |
| Novel Writing | idea → outline → chapter_draft → continuity_check → line_edit → human_review → published |
| Research Report | idea → topic_refinement → research → outline → draft → fact_check → formatting → human_review → published |
| Data Analysis | idea → question_refinement → [data_collection \|\| schema_design] → analysis → visualization → write_up → human_review → completed |
| Mathematics / Proof Exploration | idea → problem_statement → approach_factory → approach_planning → proof_attempt → peer_review → synthesis → human_review → accepted |
| Bug Triage | bug_report → reproduce → root_cause → fix → regression_test → human_review → resolved |
| Overnight Generation (Story Factory) | seed_prompt → story_bible → chapter_factory → chapter_outline → chapter_draft → continuity_check → chapter_archive |

---

## 3. Agent Registry

**File:** `app/agent/agent_registry.py`

```python
AGENT_REGISTRY: dict[str, AgentSpec]
```

Built-in keys: `planning_agent`, `implementation_agent`, `review_agent`,
`optimization_agent`, `security_agent`, `final_review_agent`, `human_gate`,
`intake_agent`, `terminal`, `arch_agent`, `custom_llm_agent`, `factory_node`.

Custom agent definitions from `custom_agent_definitions` are loaded at startup and on
create/update via `load_custom_agents_into_registry()` in `crud_malleable.py`.

**`AgentSpec` fields:** `cls` (agent class), `display_name`, `description`,
`default_tools`, `gate_type` (llm_judge | single_pass | test_suite | human | voting).

---

## 4. Pipeline Router

**File:** `app/agent/pipeline_router.py`

```python
get_next_stage(task_id, condition)     → str | None   # DB lookup: current stage → outgoing edges
advance_stage(task_id, condition, *, from_stage=None) → bool  # writes stage_key + type atomically
get_stage_config(task_id)              → StageConfig | None
dispatch_task(task_id, stage_key=None, **llm_config) → bool
```

`advance_stage()` accepts an optional `from_stage` kwarg for idempotent re-sends.
Returns `False` when no matching transition edge exists (task stays put).

**Fallback:** `_LEGACY_TRANSITIONS` dict in `pipeline_router.py` provides a hardcoded
Software-Development transition table for tasks without a `pipeline_template_id`.

---

## 5. Custom LLM Agent

**File:** `app/agent/custom_llm_agent.py`

`CustomLLMAgent` extends `AgentLoop`. It reads a `custom_agent_definitions` row by
`name`, injects the row's `system_prompt`, and restricts tools to `allowed_tools`
(always appends `submit_work`). On exit:
1. Computes `condition` from the agent's verdict (ACCEPTED→pass, REJECTED→fail).
2. If `condition == "pass"` and `stage_config.verifier != "none"`, calls
   `run_verifier(task_id, stage_config)` from `verifiers.py`. A failing verifier
   overrides condition to `"fail"`.
3. Calls `advance_stage(task_id, condition)`.

**Verifier gap (as of 2026-05-15):** The verifier call is the planned behavior;
confirm it is wired in `custom_llm_agent.py` before assuming it runs.

---

## 6. Verifiers

**File:** `app/agent/verifiers.py`

`run_verifier(task_id, stage_config) → bool`

| Verifier key | What it does |
|---|---|
| `none` | Always returns True (no verification) |
| `python_sympy` | Runs `sympy_proof_code` from task content as a Python subprocess |
| `lean4` | Runs `lean_proof` from task content via the `lean` binary (stub if not installed) |
| `coq` | Runs Coq proof (stub) |
| `custom_script` | Runs `stage_config.verifier_cmd` with task content JSON as stdin |

---

## 7. Pipeline CRUD API

All pipeline management routes. Full detail in `app/main.py`.

```
# Templates
GET    /api/pipelines                          — list all templates
POST   /api/pipelines                          — create template
GET    /api/pipelines/{id}                     — full template (stages + transitions + groups + arch_categories)
PUT    /api/pipelines/{id}                     — update template metadata
DELETE /api/pipelines/{id}                     — delete (blocked if is_builtin or any project uses it)

# Stages
GET    /api/pipelines/{id}/stages
POST   /api/pipelines/{id}/stages
PUT    /api/pipelines/{id}/stages/{stage_id}
DELETE /api/pipelines/{id}/stages/{stage_id}   — blocked if tasks use this stage without redirect
POST   /api/pipelines/{id}/stages/{stage_id}/delete-with-redirect  body: {redirect_stage_key}

# Transitions
GET/POST/PUT/DELETE  /api/pipelines/{id}/transitions[/{t_id}]

# Stage groups
POST/PUT/DELETE  /api/pipelines/{id}/groups[/{g_id}]

# Arch categories
GET/POST/PUT/DELETE  /api/pipelines/{id}/arch-categories[/{c_id}]
GET  /api/projects/{name}/arch-categories      — categories for the project's active template

# Assignment
POST  /api/projects/{name}/pipeline            body: {template_id: int}
POST  /api/projects/{name}/use-template        body: {template_id: int}   (alias)

# Export / Import
GET   /api/pipelines/{id}/export               — JSON blob
POST  /api/pipelines/import                    body: JSON blob

# Agent types (for property panel dropdowns)
GET   /api/pipelines/agent-types               — keys + display_name + description from AGENT_REGISTRY

# ⚡ Field generation
POST  /api/pipelines/generate-field
      body: {field, node_state, graph_context, partial_value}
      — streams an LLM-generated value for the specified property panel field

# Factory trigger
POST  /api/pipelines/stages/{stage_id}/trigger-factory?project={name}

# Custom agent definitions
GET/POST/PUT/DELETE  /api/agent-definitions[/{id}]
```

Also: `/api/pipeline-templates` routes exist as backward-compat aliases for
`/api/pipelines`.

---

## 8. Pipeline Editor (Litegraph.js)

**Files:** `app/web/pipeline_editor.html`, `pipeline_editor.js`, `pipeline_editor.css`
**Vendor:** `app/web/vendor/litegraph.js` + `litegraph.css` (vendored, no CDN)

**Routes:**
- `GET /pipelines` — template gallery (`gallery.html`)
- `GET /pipelines/{id}/edit` — canvas editor
- `GET /pipelines/new` — create blank template, redirect to editor

**Node types:** Stage, Factory, Conditional, Judgment Gate, Fan-out, Human Gate.
All rectangles; differentiation by port count and user-assigned color.

**Port types:** `task` (blue), `condition` (amber), `data` (green).

**Back-edge rendering:** Edges where `to_stage.position < from_stage.position`
are drawn dashed in amber to visually distinguish loops from forward progress.

**Property panel (slide-in from right):** Each ⚡ button POSTs to
`/api/pipelines/generate-field` and streams the response into the field.
The Intent field (plain-English stage purpose) is the primary authoring surface;
the system_prompt field is what agents actually receive — it is not inferred at
runtime.

**Save/load translation:** `pipeline_editor.js` converts between Litegraph's
internal `graph.serialize()` JSON and the DB schema on every load/save. The
Litegraph internal format never leaks to the API.

**Simulation mode:** "Simulate" button steps a ghost token through pass-condition
edges, highlighting the forward path.

**Tidy Layout:** Kahn's algorithm (back-edges excluded) left-to-right topological
sort with even spacing.

---

## 9. Template Gallery

**File:** `app/web/gallery.html`

Browse, clone, assign, import/export pipeline templates. Built-in templates show a
warning banner in the editor but editing is allowed (`is_builtin` only blocks deletion).

**[Use] button** → calls `POST /api/projects/{name}/use-template`. Migrates all
existing tasks' `stage_key` values to match the new template's stages.

---

## 10. Document Store

**Files:** `app/agent/doc_store.py`, `app/database/crud_documents.py`

Per-project shared artifact store keyed by name. All agents in a project can read/write.

```python
store_document(project_id, key, content, tags, written_by_task_id)  # upsert; key lowercased
get_document(project_id, key) → str | None                          # exact lookup
fuzzy_get_document(project_id, key, threshold=0.3) → list[FuzzyResult]  # pg_trgm similarity
list_documents(project_id, tag=None) → list[DocumentMeta]
delete_document(project_id, key, deleted_by_task_id) → bool         # soft-delete
```

**Agent tools** (add to stage `tool_allowlist`):
`store_document`, `get_document`, `search_documents`, `list_documents`

**REST API:**
```
GET/PUT/DELETE  /api/projects/{name}/documents/{key}
GET             /api/projects/{name}/documents
GET             /api/tasks/{task_id}/documents
```

**UI:** A "📄 Docs" button in the kanban project header opens a modal document browser.

**Key design decision:** keys are lowercased at write and read time; last-write-wins per
`(project_id, key)`; no RAG/embeddings — strictly named artifact retrieval.

---

## 11. Card Factory System

**Files:** `app/agent/card_factory.py`, `app/agent/factory_sources.py`,
`app/database/crud_factory.py`

Factory nodes ingest external data and batch-create cards. Two segmentation modes:
- **Mechanical** — 1:1 mapping, one card per data item via template interpolation
- **LLM-segmented** — `CardFactoryAgent` (a `CustomLLMAgent` variant) reads the
  source and calls `batch_create_cards` to decide how to split

**Data source adapters** (`factory_sources.py`):
`FolderAdapter`, `FileListAdapter`, `CSVAdapter`, `JSONArrayAdapter`,
`SQLiteQueryAdapter` (reads an external SQLite data file, not the app DB),
`ManualPromptAdapter`, `MaestroCardsAdapter`

**Trigger mechanisms:**
- `manual` — "Run Now" button in editor or canvas
- `predecessor_complete` — fires after a card completes the preceding stage
- `cron` — runs on a cron schedule (`croniter` or built-in minimal parser)

**Audit table:** `factory_runs` — tracks `(factory_stage_id, project_id, trigger_type,
trigger_card_id, started_at, completed_at, cards_created, status)`.

**Note:** Path security validation (`FACTORY_ALLOWED_ROOTS`) is not implemented;
`FolderAdapter` and `SQLiteQueryAdapter` accept arbitrary paths. Acceptable for
single-user local deployment; add validation before multi-user deployment.

---

## 12. Workspace Isolation & Deletion Protection

**File:** `app/agent/workspace.py`

Wraps `worktree.py` with deletion protection:

```python
delete_file(task_id, path, effective_root, project_root) → ArchivedFileRecord
    # moves to {project_root}/.archive/{timestamp}/{task_id}/{path}; inserts archived_files row
undelete_file(archive_id, restore_path=None) → str
rename_file(src, dst, effective_root)   # fails if dst exists
write_file(task_id, path, content, effective_root)
read_file(task_id, path, effective_root) → str
list_dir(task_id, path, effective_root) → list[str]
```

**Archive path scheme:** paths stored relative to project root (not absolute) so the
archive survives project moves. `.archive/` and `.maestro-worktrees/` are both in
`.gitignore`.

**Agent tool registration gap (as of 2026-05-15):** `workspace_delete_file` and
`workspace_rename_file` are planned as agent-callable tools in `tools.py`; confirm
they are registered in `TOOL_SCHEMAS` and `TOOL_REGISTRY` before assuming agents can
call them.

**Human undelete:** `POST /api/tasks/{task_id}/undelete` with `{archive_id}` works
regardless of the agent tool registration status.

---

## 13. Autopilot & Mission System

**Settings storage:** `system_settings` table (keys: `maestro_autopilot`,
`autopilot_start_hour`, `autopilot_stop_hour`). Per-project override in
`project_settings(project_id, key='autopilot_override', value='inherit'|'force_on'|'force_off')`.

**Scheduler gate:** `_should_autopilot_dispatch()` in `scheduler.py` checks the
three settings each tick, handles overnight hour-range wraparound correctly.

**Mission system:** `MissionConfig` and `MissionState` dataclasses in `scheduler.py`.
Mission lives in memory only — a server restart with `maestro_autopilot='on'` resets
to `'off'` and logs a warning. Termination conditions: `time_limit`, `token_budget`,
`card_count`, `goal_card`. First condition to fire ends the mission and creates a
Mission Report arch card via `_create_mission_report()`.

**API:**
```
GET/POST  /api/settings/autopilot         body: {autopilot, start_hour?, stop_hour?, mission?}
GET/POST  /api/projects/{name}/settings   body: {autopilot_override: inherit|force_on|force_off}
```

**UI toggle:** arch bar area in `index.html`. Off→On opens mission dialog; On→Off
pauses immediately. localStorage caches last-used mission dialog values.

---

## 14. CRUD Helper Library

**File:** `app/database/crud_malleable.py`

50+ functions for pipeline templates, stages, transitions, groups, arch categories,
custom agent definitions, system_settings, and project_settings. Key functions:

```python
# Templates
get_all_templates() → list[PipelineTemplate]
get_template(id) → PipelineTemplate | None
clone_template(id, new_name) → PipelineTemplate
export_template(id) → dict            # JSON-serializable
import_template(data: dict) → PipelineTemplate

# Registry integration
load_custom_agents_into_registry()    # called at startup and on definition create/update

# Settings
get_system_setting(key) → str | None
set_system_setting(key, value)
get_project_setting(project_id, key) → str | None
set_project_setting(project_id, key, value)
```
