# Phase 3 — Pipeline CRUD API

> **Status:** COMPLETE — 2026-05-15  
> **Depends on:** Phase 1 (tables exist); Phase 2 can run in parallel  
> **Estimated effort:** 2 days  
> **Goal:** REST endpoints for managing pipeline templates, stages, transitions, and
> arch categories. Also the ⚡ field-generation endpoint. No frontend yet — this is
> the API layer the Phase 4 editor will call.

---

## Deliverables

1. Full CRUD for `pipeline_templates`
2. Full CRUD for `pipeline_stages` (within a template)
3. Full CRUD for `pipeline_transitions` (edges between stages)
4. Full CRUD for `pipeline_stage_groups`
5. Full CRUD for `pipeline_arch_categories`
6. Assign template to project endpoint
7. Template export and import as JSON
8. `POST /api/pipelines/generate-field` — ⚡ lightning bolt endpoint
9. `GET /api/pipelines/agent-types` — list of registered agent types (from registry)

---

## Route Definitions

```
# Templates
GET    /api/pipelines                          — list all templates (id, name, is_default, version)
POST   /api/pipelines                          — create template
GET    /api/pipelines/{id}                     — full template (stages + transitions + groups + arch_categories)
PUT    /api/pipelines/{id}                     — update template metadata
DELETE /api/pipelines/{id}                     — delete template (blocked if any project uses it)

# Stages within a template
GET    /api/pipelines/{id}/stages              — list stages ordered by position
POST   /api/pipelines/{id}/stages              — add a stage
PUT    /api/pipelines/{id}/stages/{stage_id}   — update stage (label, agent_type, position, config, color)
DELETE /api/pipelines/{id}/stages/{stage_id}   — delete stage (blocked unless redirect provided)

# Stage deletion with redirect
POST   /api/pipelines/{id}/stages/{stage_id}/delete-with-redirect
       body: { redirect_stage_key: str }
       — migrates all tasks in the deleted stage to redirect_stage_key, then deletes

# Transitions
GET    /api/pipelines/{id}/transitions         — list edges
POST   /api/pipelines/{id}/transitions         — add edge
PUT    /api/pipelines/{id}/transitions/{t_id}  — update condition or priority
DELETE /api/pipelines/{id}/transitions/{t_id}  — remove edge

# Stage groups
POST   /api/pipelines/{id}/groups              — create group
PUT    /api/pipelines/{id}/groups/{g_id}       — rename / recolor
DELETE /api/pipelines/{id}/groups/{g_id}       — dissolve group (stages become ungrouped)

# Arch categories
GET    /api/pipelines/{id}/arch-categories     — list categories
POST   /api/pipelines/{id}/arch-categories     — add category
PUT    /api/pipelines/{id}/arch-categories/{c_id}
DELETE /api/pipelines/{id}/arch-categories/{c_id}

# Assignment
POST   /api/projects/{name}/pipeline           body: { template_id: int }
       — assign template to project; migrates all task stage_keys (see Phase 1 / Q5 decision)

# Export / import
GET    /api/pipelines/{id}/export              — returns full template as JSON blob
POST   /api/pipelines/import                   body: JSON blob — creates new template from export

# Agent type list (for property panel dropdowns)
GET    /api/pipelines/agent-types              — returns list of {key, display_name, description,
                                                 default_tools, gate_type} from AGENT_REGISTRY

# ⚡ Field generation
POST   /api/pipelines/generate-field
       body: {
         field: str,            # "system_prompt" | "label" | "intent" | "gate_type" | "tool_allowlist"
         node_state: {          # current values of all other fields in the property panel
           stage_key, label, agent_type, intent, gate_type, tool_allowlist, system_prompt, ...
         },
         graph_context: {       # topology around this node
           pipeline_name, pipeline_description,
           predecessor_labels: [str],
           successor_labels: [str],
           in_conditions: [str],    # edge conditions arriving at this node
           out_conditions: [str]    # edge conditions leaving this node
         },
         partial_value: str     # whatever the user has typed in the field so far (may be "")
       }
       returns: { generated_value: str }
```

---

## Stage Deletion Safety Rule

Deleting a stage that has tasks currently assigned to it is blocked unless the
caller provides a `redirect_stage_key`. The `delete-with-redirect` endpoint:

1. Validates `redirect_stage_key` exists in the same template.
2. `UPDATE tasks SET stage_key = redirect_stage_key, type = redirect_stage_key
   WHERE stage_key = deleted_stage_key AND project_id IN (projects using this template)`.
3. Deletes incoming and outgoing transitions for the stage.
4. Deletes the stage row.
5. Returns count of migrated tasks.

This enforces the Q5 decision: no card ever ends up in a null stage.

---

## ⚡ Field Generation (`/api/pipelines/generate-field`)

### System prompt for the generation call

```
You are a pipeline designer assistant for Maestro, an AI orchestration system.

Pipeline: "{pipeline_name}"
Pipeline description: "{pipeline_description}"

The node being designed:
  Stage key: {stage_key}
  Agent type: {agent_type}
  Display label: {label}
  Intent: {intent}
  Gate type: {gate_type}
  Tools allowed: {tool_allowlist}
  Predecessor stages: {predecessor_labels}  (these complete before this stage runs)
  Successor stages: {successor_labels}      (this stage must produce output for these)
  Incoming conditions: {in_conditions}
  Outgoing conditions: {out_conditions}

The user has started typing the following for the "{field}" field:
  "{partial_value}"

Generate a {field} for this stage. Use the intent, label, agent type, tool list,
and graph position as your primary signal. Return only the generated value —
no explanation, no markdown fencing.
```

The LLM call uses the project's default LLM (or a lightweight fast model if
configured). Response is streamed to the client so the field populates progressively.

### Fields and their generation behavior

| Field | What the generation focuses on |
|---|---|
| `system_prompt` | Full agent instruction based on intent, tools, predecessor/successor |
| `label` | Short display name (2-4 words) inferred from intent and agent type |
| `intent` | One-sentence purpose statement inferred from label + graph position |
| `gate_type` | Suggests appropriate gate (llm_judge, single_pass, test_suite, human, voting) |
| `tool_allowlist` | Suggests tool list appropriate for the intent and agent type |

---

## Template Export Format

```json
{
  "schema_version": 1,
  "name": "Novel Writing",
  "description": "...",
  "arch_categories": [
    { "key": "characters", "label": "Characters", "color": "#7c3aed", "position": 0 }
  ],
  "groups": [
    { "name": "Drafting", "color": "#1e40af", "position": 0 }
  ],
  "stages": [
    {
      "stage_key": "outline",
      "label": "Outline",
      "agent_type": "planning_agent",
      "position": 0,
      "group": null,
      "color": null,
      "config": {
        "gate_type": "llm_judge",
        "retries": 2,
        "intent": "Produce a chapter-by-chapter outline from the idea description.",
        "system_prompt": "...",
        "tool_allowlist": ["read_file", "write_file"],
        "required_input_keys": [],
        "output_keys": ["outline"],
        "verifier": "none",
        "arch_category_keys": ["characters", "themes"]
      }
    }
  ],
  "transitions": [
    { "from": "outline", "to": "draft", "condition": "pass", "priority": 0 },
    { "from": "draft",   "to": "draft", "condition": "fail", "priority": 0 }
  ]
}
```

Import creates a new template (never overwrites an existing one). Stage positions
and group assignments are preserved.

---

## Test Criteria

- Create a template, add 3 stages, add edges → `GET /api/pipelines/{id}` returns
  the full graph correctly
- Delete a stage with tasks assigned → 400 without redirect, succeeds with redirect,
  tasks migrated in DB
- Export → import round-trips with identical graph shape
- `generate-field` with `field="system_prompt"` and a populated `node_state` returns
  a non-empty string
- Assigning a template to a project that already has tasks migrates `stage_key`
  correctly for all tasks

---

## Risk Factors

**Concurrent template edits** — if the Phase 4 editor sends rapid partial saves
(e.g., drag a node, node position saves immediately), each save is an independent
PUT. Debounce position saves in the frontend (~300ms) to avoid unnecessary
round-trips; PostgreSQL handles concurrent writes without lock starvation, but
the network overhead of dozens of rapid saves per drag still adds up.

**`generate-field` LLM cost** — each ⚡ click fires an LLM call. Use the project's
smallest configured LLM for this, not the compute-heavy reasoning model. Add a
`[pipeline_editor] llm_id` override to `maestro.ini` so the operator can pin a
cheap fast model for generation calls.

---

## Implementation Audit (2026-05-15)

### What was delivered

All planned routes are implemented and functional. Full CRUD for templates, stages,
transitions, groups, and arch categories; `delete-with-redirect` for stages;
export/import as JSON (`schema_version=1`); `GET /api/pipelines/agent-types`;
streaming `POST /api/pipelines/generate-field`.

A parallel route set (`/api/pipeline-templates`) exists alongside `/api/pipelines` for
backward compatibility with older frontend code; both return the same data.

### No deviations from plan. Status: COMPLETE ✅
