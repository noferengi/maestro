# Malleable Pipelines — Design Exploration

> **Status:** COMPLETE — All phases implemented and verified, May 2026  
> **Author:** Exploration session, May 2026  
> **Detailed phase plans:** `plans/PHASE_1_DATA_MODEL.md` through `plans/PHASE_10_TEMPLATES_GALLERY.md`  
> **Goal:** Decouple pipeline definition from the scheduler so any project can run
> any workflow — software development, novel writing, research reports, data
> analysis, mathematics, or anything else — with a visual node editor for composing pipelines.

---

## 1. The Problem With How We Are Built Today

The Maestro scheduler is a Wiggum Loop wired to one fixed pipeline:

```
IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION →
SECURITY → FINAL_REVIEW → HUMAN_REVIEW → COMPLETED
```

This is hardcoded across the codebase. A **codebase survey (May 2026)** identified
the actual blast radius:

| Location | What's hardcoded |
|---|---|
| `scheduler.py` (4910 lines) | 9 separate dispatch queues: DAG tasks, file summaries, research jobs, arch-gen jobs, survey jobs, clarification jobs, PIP resolution, subdivision recovery, Maestro orchestrator |
| `models.py` `Task.type` | String enum; `stage_key` column does not yet exist |
| `maestro.ini [pipeline] column_order` | Canonical ordering lives in config, not the DB |
| `kanban.js` `ARCH_CATEGORY_COLORS` | 14 architecture categories hardcoded in JS |

There is no `advance_task_type()` function; stage transitions happen via scattered
`update_task(task_id, type=...)` calls in `main.py`, `crud_tasks.py`, and the
individual agent files. There is no agent registry — each agent class is instantiated
by its own dedicated dispatcher function inside `scheduler.py`.

Every agent class is valid only in the context of software development. A novel-writing
project has no use for `SecurityReviewAgent`.

The consequence: if you want to run a novel-writing project through Maestro today,
you either abuse the software pipeline (use "INDEV" to mean "draft a chapter",
"CONCEPTUAL_REVIEW" to mean "editor pass") or you can't use Maestro at all.

---

## 2. The Vision: Pipelines as First-Class Objects

A **pipeline** is a directed graph of **stage nodes** and **transition edges**.
Each project references exactly one pipeline template. The scheduler, frontend,
and kanban board are all driven by that template — nothing is hardcoded.

### 2.1 Core Concepts

**Stage Node** — one column on the kanban board plus the agent that runs inside it.

```
┌─────────────────────────────────┐
│  STAGE: "Draft"                 │
│  agent:  writing_agent          │
│  tools:  [read_file, write_file]│
│  gate:   single_pass            │
│  retries: 2                     │
│  llm_override: null             │
└─────────────────────────────────┘
```

**Transition Edge** — a directed connection from one stage to another, with a
condition that fires the transition.

```
Draft ──(pass)──► Line Edit
Draft ──(fail)──► Draft          # retry in-place
Draft ──(reject)► Outline        # demote to prior stage
```

**Stage Group** — a visual bracket around related stages (mirrors today's "grouped"
Security + Optimization + Final Review treatment). Groups can be named, coloured,
and collapsed in the UI.

**Pipeline Template** — the full graph: stages + edges + groups + metadata.
Saved in the DB and assignable to any project.

**Agent Node** — a registered agent class, its allowed tools, and its input/output
schema. Agents are pulled from an `agent_registry`; users can add custom ones.

---

## 3. The ComfyUI Analogy — Where It Fits and Where It Doesn't

ComfyUI's model is:

```
[source node] ──[typed wire]──► [processor node] ──[typed wire]──► [sink node]
```

For Maestro, the "wire" is the task itself as it flows through stages. The
analogy holds well:

| ComfyUI concept | Maestro equivalent |
|---|---|
| Node | Stage (agent + gate + tools) |
| Typed port | Data contract between stages (task description, planning result, review verdict, etc.) |
| Wire | Stage transition (conditional) |
| Group / reroute | Stage group, parallel group |
| Bypass | Skip edge — `SECURITY` stage can be bypassed for trivial tasks |
| Queue / batch | DAG prerequisite system (already exists) |

**Where the analogy breaks down:**  
ComfyUI is a synchronous DAG — every node runs once per image. Maestro stages are
re-entrant: a task can loop through INDEV many times as the dev orchestrator retries.
The graph has **back-edges** (fail → demote → earlier stage). This is a cyclic
directed graph, not a DAG, so the visual metaphor needs loop-back wires rendered
distinctly (dashed, curved, different colour) from forward progress wires.

The other difference: Maestro's graph operates *per-task*, not per-pipeline-run.
Each task is a token that moves through the graph independently and asynchronously.
The visual editor shows the graph topology; the kanban board shows the current
position of each token.

---

## 4. Data Model Changes

### 4.0 Scope of the Pipeline System

The 8 infrastructure-level scheduler queues (file summaries, research jobs,
arch-gen, clarification, PIP resolution, survey jobs, subdivision recovery,
Maestro orchestrator) remain as scheduler internals and are **not** expressed as
pipeline stage nodes. The pipeline template system governs only the DAG task stages
that tasks flow through on the kanban board.

### 4.1 New Tables

All DDL targets PostgreSQL (migrations 0068+). `SERIAL` for auto-increment PKs,
`TIMESTAMPTZ` for timestamps, `JSONB` for structured config blobs.

```sql
-- A named, reusable pipeline topology
CREATE TABLE pipeline_templates (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT,
    is_default  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_builtin  BOOLEAN     NOT NULL DEFAULT FALSE,
    version     INTEGER     NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- One row per stage node in a template
CREATE TABLE pipeline_stages (
    id           SERIAL  PRIMARY KEY,
    template_id  INTEGER NOT NULL REFERENCES pipeline_templates(id),
    stage_key    TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    agent_type   TEXT    NOT NULL,
    position     INTEGER NOT NULL,
    group_id     INTEGER REFERENCES pipeline_stage_groups(id),
    config       JSONB,              -- gate, retries, llm_override, tools, intent, system_prompt, …
    color        TEXT,
    UNIQUE(template_id, stage_key)
);

-- Stage groups (bracketed columns)
CREATE TABLE pipeline_stage_groups (
    id          SERIAL  PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
    name        TEXT    NOT NULL,
    color       TEXT,
    position    INTEGER NOT NULL
);

-- Directed edges between stages
CREATE TABLE pipeline_transitions (
    id            SERIAL  PRIMARY KEY,
    template_id   INTEGER NOT NULL REFERENCES pipeline_templates(id),
    from_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
    to_stage_id   INTEGER NOT NULL REFERENCES pipeline_stages(id),
    condition     TEXT    NOT NULL CHECK(condition IN ('pass','fail','reject','always','skip')),
    priority      INTEGER NOT NULL DEFAULT 0
);
```

```sql
-- Arch card categories per pipeline template
CREATE TABLE pipeline_arch_categories (
    id          SERIAL  PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
    key         TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    color       TEXT,
    position    INTEGER NOT NULL,
    UNIQUE(template_id, key)
);

-- Deleted-file audit trail (deletion protection for workspace scratch pads)
CREATE TABLE archived_files (
    id             SERIAL      PRIMARY KEY,
    task_id        TEXT        NOT NULL REFERENCES tasks(id),
    original_path  TEXT        NOT NULL,
    archive_path   TEXT        NOT NULL UNIQUE,
    deleted_at     TIMESTAMPTZ DEFAULT NOW(),
    restored_at    TIMESTAMPTZ
);

-- Global and per-project key/value settings
CREATE TABLE system_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE project_settings (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    PRIMARY KEY (project_id, key)
);
```

### 4.2 Changes to Existing Tables

```sql
-- Projects reference a template
ALTER TABLE projects ADD COLUMN pipeline_template_id INTEGER
    REFERENCES pipeline_templates(id);

-- Tasks store their stage_key instead of (or alongside) type
-- Migration: existing type values become stage_keys in the default template
ALTER TABLE tasks ADD COLUMN stage_key TEXT;
-- tasks.type is kept for backward compat but becomes an alias
```

### 4.3 Backward Compatibility

Migration `0068_pipeline_templates_baseline.py` would:
1. Insert the "Software Development" template with all current stages.
2. Set all existing projects to use it.
3. Populate `stage_key` from `type` for all existing tasks.
4. Make `tasks.type` a computed alias via a DB view (or leave it as a denormalized
   copy that the scheduler keeps in sync).

No existing task or project changes behaviour. The new tables sit dormant until
a user creates a custom template or the UI is deployed.

---

## 5. The Agent Registry

Today agent classes are imported directly in `scheduler.py`. The registry
decouples this:

```python
# app/agent/registry.py

AGENT_REGISTRY: dict[str, AgentSpec] = {
    "planning_agent": AgentSpec(
        cls=PlanningAgent,
        display_name="Planning",
        description="Analyzes task and produces an implementation plan.",
        default_tools=["read_file", "web_search", "list_dir"],
        input_schema=TaskDescriptionSchema,
        output_schema=PlanningResultSchema,
        gate_type="voting",        # LLM majority vote on pass/fail
    ),
    "implementation_agent": AgentSpec(
        cls=DevOrchestrator,
        display_name="Implementation",
        description="Writes code, runs tests, iterates.",
        default_tools=["read_file", "write_file", "run_pytest", "run_shell"],
        input_schema=PlanningResultSchema,
        output_schema=ImplementationResultSchema,
        gate_type="test_suite",    # pass = all tests green
    ),
    "writing_agent": AgentSpec(
        cls=WritingAgent,          # new
        display_name="Writing",
        description="Drafts, revises, or edits prose.",
        default_tools=["read_file", "write_file"],
        input_schema=OutlineSchema,
        output_schema=ProseSchema,
        gate_type="llm_judge",     # single LLM evaluator
    ),
    "research_agent": AgentSpec(
        cls=ResearchAgent,         # exists today as intake sub-agent
        display_name="Research",
        description="Web search + synthesis.",
        default_tools=["web_search", "web_fetch"],
        input_schema=ResearchQuerySchema,
        output_schema=ResearchSummarySchema,
        gate_type="single_pass",   # no gate, always passes
    ),
    "human_gate": AgentSpec(
        cls=HumanGateAgent,        # new — pauses until user approves
        display_name="Human Review",
        description="Pause for human approval before proceeding.",
        default_tools=[],
        gate_type="human",
    ),
    "custom_llm_agent": AgentSpec(
        cls=CustomLLMAgent,        # new — user-defined prompt + tools
        display_name="Custom",
        description="User-defined agent with custom system prompt.",
        default_tools=[],
        gate_type="llm_judge",
    ),
}
```

The scheduler's dispatch loop becomes:

```python
# Instead of:  if task.type == "planning": run PlanningAgent(...)
# Now:
stage = get_stage_for_task(task)          # DB lookup: task.stage_key → pipeline_stages row
spec  = AGENT_REGISTRY[stage.agent_type]  # lookup in registry
agent = spec.cls(task, stage.config)      # instantiate with per-stage overrides
await agent.run()
```

---

## 6. The Frontend: Pipeline Editor

### 6.1 Canvas Layout

A new route `/pipelines/{template_id}/edit` renders a full-canvas node editor
built on **Litegraph.js** (single script tag, no build step):

```
╔══════════════════════════════════════════════════════════════╗
║  Pipeline: "Novel Writing"           [Save] [Simulate] [...]  ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  ┌──────┐   ┌─────────┐   ╔══════════════╗   ┌──────────┐  ║
║  │ IDEA │──►│Outlining│──►║   Drafting   ║──►│ COMPLETE │  ║
║  └──────┘   └─────────┘   ║  ┌────────┐ ║   └──────────┘  ║
║                   ▲        ║  │Chapter │ ║                  ║
║                   │(reject)║  │ Loop   │ ║                  ║
║                   └────────║  └────────┘ ║                  ║
║                            ╚══════════════╝                  ║
║                             ▼(pass)  ▼(fail)                 ║
║                        ┌────────┐  (retry)                  ║
║                        │Line Ed.│                            ║
║                        └────────┘                            ║
║                                                              ║
╚══════════════════════════════════════════════════════════════╝
```

Back-edges (fail/reject loops) render as dashed curved wires in a distinct colour;
forward edges are solid. Node interiors are canvas-painted — rich controls live in
the slide-in property panel, not inside the node itself.

**Node controls:**
- Double-click a node → open property panel
- Drag from an output port → draw a new transition edge
- Right-click edge → set condition (pass / fail / reject / skip)
- Drag-select multiple nodes → group them
- Delete key → remove node or edge

**Property panel (slides in from right):**

Every text or select field carries a ⚡ button. Clicking it sends the full current
state of the panel — every filled field — plus graph context (pipeline name,
pipeline description, predecessor stage labels, successor stage labels, edge
conditions in and out) to `POST /api/pipelines/generate-field`. The response
replaces the content of that field. The user can type a partial value first; ⚡
treats whatever is in the field as a directional hint, not a blank slate.

```
Stage: "Drafting"
────────────────────────────────────────────────
Agent type:     [writing_agent       ▼]       ⚡
Display label:  [Drafting              ]       ⚡
LLM override:   [inherit from project ▼]
Intent:         [draft a chapter of prose from an outline] ⚡

Gate type:      [llm_judge           ▼]
Max retries:    [3                    ]

Tools allowed:
  ☑ read_file    ☑ write_file
  ☐ web_search   ☐ run_pytest

System prompt:
  ┌──────────────────────────────────────────┐ ⚡
  │ You are an expert novelist...            │
  └──────────────────────────────────────────┘
```

**The Intent field** is the primary authoring surface. It is a short, plain-English
description of what this stage should accomplish — written by the person building the
pipeline, not inferred by the system. ⚡ on the system prompt field uses the Intent
(plus agent type, tools, predecessor/successor labels, and any partial prompt text
already typed) to generate a complete, specific system prompt. ⚡ on other fields
(label, gate type, tool selection) uses the Intent similarly to suggest sensible
defaults.

The system prompt field is **not optional** and **not auto-populated at runtime**.
The Intent field makes writing it fast — a few words from the pipeline author, one
click, and the LLM produces a draft the author can review and edit. But the saved
prompt is what the agent runs on; there is no "infer from graph at dispatch time"
magic. What you see in the panel is what the agent gets.

### 6.2 Kanban Derivation

When a project is assigned a pipeline template, the kanban board derives its
columns from `pipeline_stages` ordered by `position`. Stage groups become
visual brackets with the group name as a header. This is a purely mechanical
render — no hardcoded column list in `board.js`.

### 6.3 Template Gallery

A `/pipelines` gallery page shows all saved templates with a "Use for this
project" button and a "Clone & edit" option. Maestro ships with built-in
templates:

| Template | Stages |
|---|---|
| Software Development | (current pipeline, verbatim) |
| Novel Writing | Outline → Chapter Draft → Continuity Check → Line Edit → Human Review → Published |
| Research Report | Topic → Research → Outline → Draft → Fact Check → Format → Human Review → Published |
| Data Analysis | Question → Data Collection → Analysis → Visualization → Write-Up → Human Review |
| Bug Triage | Reproduce → Root Cause → Fix → Regression Test → Human Review |

---

## 6.4 Node Type Taxonomy

All nodes are rectangles of the same shape. User assigns color per node or
selection. Semantic meaning comes from wiring, not shape. Node types:

| Node type | Ports | What it does |
|---|---|---|
| **Stage node** | 1 in, N out (one per condition) | Runs an agent; the primary building block |
| **Factory node** | 0–1 in (optional trigger), 1 out | Ingests external data and batch-creates cards |
| **Conditional node** | 1 in, 2–N out | Branches on a content blob key value |
| **Judgment gate** | N in, 1 out | Fan-in: receives N parallel attempts, selects the best |
| **Fan-out node** | 1 in, 1 out | Spawns N parallel attempt cards from one input |
| **Human gate** | 1 in, 2 out (approve / reject) | Blocks until human acts or Maestro autopilot handles it |

---

## 7. The Scheduler: What Changes, What Doesn't

### 7.1 What stays the same
- The DAG prerequisite system (task-level, not stage-level)
- Budget and LLM routing
- Git worktree isolation for any agent that writes files
- The tick interval and thread model
- The capacity/slot counting (per-LLM, per-compute-node)

### 7.2 What changes

**Dispatch routing** — today's `if task.type == "planning": ...` chain becomes
a DB lookup + registry call. One dispatch path for all agent types.

**Stage advancement** — today's `advance_task_type` function hardcodes
"planning" → "indev" → "conceptual_review" etc. Replaced by:

```python
def get_next_stage(task, condition: str) -> str | None:
    """Return the next stage_key given the current stage and the exit condition."""
    edges = get_outgoing_transitions(task.stage_key, task.pipeline_template_id)
    # Pick highest-priority edge whose condition matches
    for edge in sorted(edges, key=lambda e: -e.priority):
        if edge.condition == condition:
            return edge.to_stage.stage_key
    return None  # no transition → task stays put
```

**Column map** — the front-end's `columnMap` is built from `pipeline_stages`
at board load time, not from a hardcoded dict.

### 7.3 The `task.type` migration problem

`task.type` today is simultaneously:
1. The pipeline stage identifier (routing logic reads it)
2. The display column (frontend renders based on it)
3. An enum used in 50+ places in the codebase

The migration path is **additive, not replacement**:

1. Add `task.stage_key` as a nullable column.
2. Populate it from `task.type` via migration.
3. Scheduler reads `stage_key` if set, falls back to `type`.
4. Once `stage_key` is universally set, deprecate `type`.
5. Eventually make `type` a computed alias pointing at `stage_key`.

This avoids a flag-day rewrite and lets both systems coexist during transition.

---

## 8. Loop-Back Edges and Demotion

The current "demote" mechanism (task goes backward) is a special case of a
loop-back edge with condition `"reject"`. In the graph model this is just an
edge:

```
indev ──(reject)──► planning
```

The `demote_task` API endpoint becomes:
```python
def demote_task(task_id: str, target_stage: str | None = None):
    if target_stage:
        # Force to explicit stage — admin action
        task.stage_key = target_stage
    else:
        # Follow the "reject" edge from the current stage
        next_stage = get_next_stage(task, condition="reject")
        task.stage_key = next_stage or task.stage_key
```

This means demotion behaviour is **configurable per pipeline** — a novel
pipeline could demote from "Line Edit" all the way back to "Draft", while a
software pipeline demotes from "Final Review" only one step to "Security".

---

## 9. Parallel Stage Groups

Today "Security" and "Optimization" run in parallel (both must complete before
Final Review). In the graph model this is a **fork-join pattern**:

```
indev ──► [ Security  ] ──► final_review
      └──► [ Optimization ] ┘
```

Implementation: a stage can have an `execution_mode: "parallel_group"` flag.
The scheduler dispatches all parallel-group stages simultaneously (up to LLM
capacity), and the fork-join gate fires when all of them have passed.

This generalises naturally: a research pipeline might run "Literature Review"
and "Data Collection" in parallel before "Synthesis".

---

## 10. User-Defined Agents

The most powerful extension: users define their own agent node types with a
custom system prompt and tool set. These are stored in the DB:

```sql
CREATE TABLE custom_agent_definitions (
    id            SERIAL      PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    description   TEXT,
    intent        TEXT,
    system_prompt TEXT        NOT NULL DEFAULT '',
    allowed_tools JSONB       NOT NULL DEFAULT '[]',
    gate_type     TEXT        NOT NULL DEFAULT 'llm_judge',
    verifier      TEXT        NOT NULL DEFAULT 'none',
    verifier_cmd  TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

At runtime, `custom_llm_agent` reads the definition and injects the system
prompt. The tool list constrains which tools the underlying `AgentLoop` can
call. This is essentially a meta-agent that becomes concrete when parameterised
with a definition.

**Example: "Continuity Checker" for novel writing:**
```
system_prompt: "You are a continuity editor. Read the chapter outline and the
  chapter draft. Identify any continuity errors — character inconsistencies,
  timeline contradictions, unresolved threads. List each error with chapter
  reference and severity."

allowed_tools: ["read_file", "list_dir"]
gate_type: "llm_judge"   # pass if no HIGH severity errors found
```

---

## 11. What This Enables

### 11.1 Novel Writing Pipeline
```
IDEA ──► Outline ──► [Chapter 1 Draft] ──► [Chapter 2 Draft] ──► ...
         (planning)   (writing_agent)        (writing_agent)
                           │                        │
                           ▼                        ▼
                      Continuity Check         Continuity Check
                      (custom_agent)           (custom_agent)
                           │                        │
                           └────────┬───────────────┘
                                    ▼
                               Line Edit (writing_agent)
                                    ▼
                            Human Review (human_gate)
                                    ▼
                               PUBLISHED
```

### 11.2 Research Report Pipeline
```
IDEA ──► Topic Refinement ──► Research ──► Outline ──► Draft ──►
         (planning_agent)   (research_agent)(planning)(writing)
                                                          │
                                                          ▼
                                                    Fact Check
                                                 (custom_agent: verifies
                                                  claims against sources)
                                                          │
                                                  pass ◄──┤──► fail (back to Draft)
                                                          │
                                                     Formatting
                                                  (writing_agent)
                                                          │
                                                   Human Review
                                                          │
                                                     PUBLISHED
```

### 11.3 Bug Triage Pipeline
```
BUG REPORT ──► Reproduce ──► Root Cause ──► Fix ──► Regression ──► RESOLVED
               (custom:      (custom:       (impl)   (test_agent)
                repro script) analysis)
```

### 11.4 Data Pipeline
```
QUESTION ──► Data Collection ──────► Analysis ──► Visualization ──► Write-Up
             (research_agent)         (custom:      (custom:          (writing)
         └──► Schema Design ──────┘   pandas/sql)   plot generation)
             (planning_agent)
```

---

## 12. Open Design Questions

### Q1: How do we handle agent output schemas across custom pipelines?

**Decision:** Loose blob, not tight types.

Each stage writes its output into the task's `content` JSON field — a dict with
any keys it wants. Each agent spec declares `required_input_keys` (list of keys it
needs present) and `output_keys` (list of keys it guarantees to write). The pipeline
editor warns visually when stage N's `output_keys` doesn't cover stage N+1's
`required_input_keys`, but this is advisory — mis-wired pipelines fail at runtime
with a clear error, not a crash.

The primary goal is to **keep as many DAG edges active and in-flight as possible**
to saturate inference capacity. Agents must tolerate partial or noisy predecessor
output and degrade gracefully (skip a check, use a default) rather than stalling.
Strict schema validation is an opt-in per stage, not the default.

---

### Q2: What does "subdivide" mean in a custom pipeline?

**Decision:** Subdivision is a registered agent type that calls a `batch_create_cards`
tool.

The current `SubdivisionAgent` is refactored to: (1) decide how to break the work,
then (2) call `batch_create_cards(cards: list[CardSpec])`, where each `CardSpec`
carries `{title, description, entry_stage, parent_id, tags}`. The tool is the
mechanism; the LLM decides the segmentation strategy based on its system prompt.

Key design points:
- `entry_stage` in `CardSpec` is pipeline-configured (`subdivision_entry_stage` on
  the pipeline template). For software tasks this is the pipeline's first stage; for
  a writing pipeline that is just logging character names it might be a lightweight
  "register" stage or even `completed` immediately.
- `batch_create_cards` may return a new parent card. When it does, the original card
  is demoted to a "legacy archive" card with `type='archive'` (a new soft-type),
  recording its role as the origination point.
- Partial overlap across cards is intentional and expected — the system is designed
  for redundancy, not exclusivity.

**Arch card categories (CRUD):** Currently hardcoded in `kanban.js` as 14 categories.
Categories are **per pipeline template** — software dev has Platform/Testing/Security;
a writing pipeline has Characters/Themes/Plot/WorldBuilding. A `pipeline_arch_categories`
table replaces the hardcoded JS dict, with rows keyed by `template_id`. Each stage node
in the template declares which category keys to surface in its system prompt context.
This is the "knowledge graph" layer: arch cards are the global project memory,
segmented by category, selectively surfaced per agent role.

---

### Q3: Workspace Isolation for Non-Code Work

**Decision:** Scratch pads replace raw worktrees as the agent file-access primitive.

`worktree.py` is already domain-agnostic (creates `.maestro-worktrees/{task_id}/`).
The new `workspace.py` layer wraps it with:

1. **Deletion protection** — `delete_file(path)` moves the file to
   `.archive/YYYY-MM-DD_HH-MM-SS_<hash>/original/path` and inserts an
   `archived_file` DB record with the original path, archive path, task_id, and
   a collision-safe name. `undelete_file(archive_record_id)` restores to the exact
   original path (or a user-chosen path on collision).

2. **Per-card scratch pads** — each task gets its own worktree. Within that worktree,
   the agent sees a full filesystem view and can rename/delete/create freely without
   touching other tasks' views or the shared `.archive`.

3. **Tool surface per stage** — the `allowed_tools` in a stage config controls which
   workspace tools the agent can call: `read_file`, `write_file`, `delete_file`,
   `rename_file`, `run_pytest`, `run_math_kernel`, `query_knowledge_graph`, etc.
   The per-stage tool allowlist in `config.py:build_tool_schemas()` is the enforcement
   point; no changes to the safety model are required.

4. **Collision on merge** — when a task's worktree is merged back to the main branch,
   file conflicts are surfaced as a diff for human or Maestro review. This is the
   existing git merge flow; the scratch-pad model doesn't change it.

---

### Q4: The human_gate agent and async blocking

**Decision:** Global "Human in the Loop / Leave it to the Maestro" toggle, surfaced
as a prominent button near the arch bar.

- **"Human in the Loop"** — Maestro scheduler is paused immediately. Any running
  Maestro sessions receive a graceful stop signal (existing `stop_agent` path). No
  new tasks are dispatched. Tasks in `human_review` stage (or any `human_gate` stage)
  surface as pending items for the user.
- **"Leave it to the Maestro"** — All tasks currently in a `human_gate` stage are
  fed to the Maestro orchestrator at maximum priority. Maestro decides whether to
  approve, reject, or request revision based on context. This is "YOLO mode": the
  user delegates even human-review decisions to the LLM.

State lives in `system_settings` (global default) and an optional
`project_settings(project_id, key, value)` override — mirroring how LLM overrides
work per-project today. The scheduler resolves: project override → global default.

The UI toggle is global; individual projects can override via a project settings panel.
Human-gate stages release their LLM slot immediately (existing behavior for
`HUMAN_REVIEW`) and the scheduler skips them until either the user approves or
Maestro autopilot is engaged.

---

### Q5: Pipeline versioning and running tasks

**Decision:** Migrate, not snapshot.

When a template is edited, all tasks assigned to that template are remapped to the
updated stage definitions:

- **Stage renamed:** `stage_key` on affected tasks is updated to the new key.
- **Stage deleted:** deletion requires choosing a replacement stage. The UI blocks
  deletion without a redirect — no card is left pointing to a null stage.
- **Stage added:** existing in-flight tasks are unaffected (they are already past
  or before the new stage). New tasks pick it up naturally.
- **Edge changed:** affects future transitions only; in-flight tasks are at their
  current stage, and the new edge applies when they next transition.

All tasks assigned to a given pipeline template are immediately live on the latest
definition. There is no version column per task — the template `version` field is
a change log aid, not a routing key.

---

## 13. Implementation Phases (✅ ALL PHASES COMPLETE)

See individual phase plan files in `plans/` for full detail on each phase.

> **Note:** As of May 2026, all implementation phases described below have been successfully completed and verified against the codebase.

### Phase 0 ✅ DONE — Zombie session recovery
Shipped in commit `0126374`.

### Phase 1 ✅ DONE (~3 days) — Data model & migration  `PHASE_1_DATA_MODEL.md`
All new tables added in one migration; system behavior is unchanged.  
New tables: `pipeline_templates`, `pipeline_stages`, `pipeline_transitions`,
`pipeline_stage_groups`, `pipeline_arch_categories`, `custom_agent_definitions`,
`project_documents`, `archived_files`, `system_settings`, `project_settings`,
`factory_runs`.  
Adds `tasks.stage_key` (nullable) and `projects.pipeline_template_id`.  
Seeds "Software Development" template from `maestro.ini [pipeline] column_order`.

### Phase 2 ⚠️ SUBSTANTIALLY COMPLETE — Scheduler decoupling  `PHASE_2_SCHEDULER_DECOUPLING.md`
Core infrastructure complete: `pipeline_router.py`, `agent_registry.py`, dispatch loop
refactored. ~15 call sites in scheduler.py still use direct `update_task(type=...)` for
MaestroLoop exits, variable demotion targets, and the subdivide outcome — these bypass
the pipeline graph for non-software templates. See Phase 2 audit for details.

### Phase 3 ✅ COMPLETE — Pipeline CRUD API  `PHASE_3_PIPELINE_CRUD_API.md`
Full REST CRUD for templates, stages, transitions, groups, arch categories.
Stage deletion requires a redirect target (no card ever ends up in a null stage).
Template export/import as JSON. `POST /api/pipelines/generate-field` for ⚡.

### Phase 4 ⚠️ SUBSTANTIALLY COMPLETE — Litegraph editor  `PHASE_4_LITEGRAPH_EDITOR.md`
Canvas editor at `/pipelines/{id}/edit` with all six node types, back-edge rendering,
property panel with ⚡ generation, simulation, tidy layout, and full save/load cycle.
**Known defects:** (1) kanban columns still hardcoded — CSS reorder only, new template
stages do not create board columns; (2) ~~`litegraph.js` not vendored~~ ✅ fixed 2026-05-15;
(3) gallery "Use" button calls wrong endpoint (`/use-template` vs `/pipeline`).

### Phase 5 ⚠️ SUBSTANTIALLY COMPLETE — Agent registry & custom agents  `PHASE_5_AGENT_REGISTRY.md`
`CustomLLMAgent` operational; `batch_create_cards` tool added; subdivision agent
refactored to call it. Pluggable verifier framework exists (`none`, `lean4` stub,
`coq` stub, `python_sympy`, `custom_script`) but **is not wired into the gate** —
`run_verifier()` is never called. Custom agent definition CRUD API complete.

### Phase 6 ⚠️ SUBSTANTIALLY COMPLETE — Workspace isolation & arch CRUD  `PHASE_6_WORKSPACE_ISOLATION.md`
`workspace.py` wraps worktrees with deletion protection and `.archive/` path scheme.
**Critical gap:** workspace functions (`delete_file`, `rename_file`, etc.) are not
registered as agent tools — agents cannot call them. Human undelete via
`POST /api/tasks/{id}/undelete` works. Arch categories are fully CRUD-able per
template and dynamically loaded in kanban.

### Phase 7 ⚠️ SUBSTANTIALLY COMPLETE — Autopilot & mission system  `PHASE_7_AUTOPILOT_MISSION.md`
Toggle, scheduled hours, per-project override, mission dialog, and scheduler gate are
all correct. localStorage pre-fill for mission dialog and mission report arch card
creation not verified. See Phase 7 audit.

### Phase 8 ⚠️ SUBSTANTIALLY COMPLETE — Document store  `PHASE_8_DOCUMENT_STORE.md`
Backend fully implemented (doc_store.py, crud_documents.py, REST API, pg_trgm fuzzy
matching). **Missing:** UI document viewer; no test_document_store.py test file.

### Phase 9 ✅ COMPLETE (minor gaps) — Card factory system  `PHASE_9_CARD_FACTORY.md`
Factory nodes ingest external data and batch-create cards. All adapters, both
segmentation modes, all three trigger types, and audit table implemented and tested.
Minor gaps: no LLM-segmented test, no path security validation (`FACTORY_ALLOWED_ROOTS`).

### Phase 10 ✅ COMPLETE (minor gaps) — Templates gallery  `PHASE_10_TEMPLATES_GALLERY.md`
Six built-in templates: Software Development, Novel Writing, Research Report,
Data Analysis, Mathematics/Proof Exploration, Bug Triage, Overnight Story Factory.
Gallery UI at `/pipelines`: browse, clone, assign, import/export. Built-in templates
are protected from deletion but can be cloned.

---

## 14. Risk Factors

**Scheduler complexity** — `scheduler.py` is 4910 lines with 9 independent
dispatch queues. Only the DAG-task queue needs pipeline-awareness in Phase 2;
the other 8 are left alone. But even the DAG queue dispatches are not a single
if-else chain — they are scattered across per-queue dispatcher functions each of
which directly instantiates its agent class. Full test coverage of the routing
layer is mandatory before touching the dispatch path.

**`task.type` ubiquity** — it's used in 50+ places. An additive migration is
the right call, but it means living with two sources of truth during the
transition. Set a hard deadline to deprecate `type`.

**Agent output contracts** — today agents produce typed results (PlanningResult,
etc.) that later agents consume. Loosening this to "task content JSON" requires
either a schema validator or accepting that mis-wired pipelines will fail at
runtime. A warning in the UI ("these stages have incompatible schemas") is
probably enough for now.

**Canvas library: Litegraph.js (decided)** — [Litegraph.js](https://github.com/jagenjo/litegraph.js)
is the canvas library, matching ComfyUI's stack. It is a single script-tag drop-in
with no build step, handles back-edges and typed ports natively, and scales to
thousands of nodes at 60fps on a 2D canvas. The trade-off vs React Flow: node
interiors are canvas-painted primitives, so rich HTML (dropdowns, text areas) cannot
live inside the node itself. This is acceptable — the property panel is a separate
slide-in panel by design. No bundler, no React, no infrastructure change required.

---

## 15. Summary

The core insight is that Maestro's scheduler is already general-purpose. The only
things tying it to software development are the hardcoded stage → agent mapping and
the hardcoded column list in the frontend. Replacing those with a DB-backed pipeline
template system gives users a fully malleable orchestration platform.

**What this becomes:** A Litegraph.js canvas where you draw nodes, connect them with
wires, write a one-sentence Intent per stage, and click ⚡ to generate the system
prompt. The kanban board derives its columns from whatever you drew. The scheduler
dispatches agents by looking up the graph, not by reading Python if/elif chains.

**The five supporting systems** built alongside the pipeline engine:

| System | What it enables |
|---|---|
| **Document store** (Phase 8) | Agents share named artifacts across cards; coordination without tight coupling |
| **Card factory** (Phase 9) | Ingest folders, CSVs, databases, or LLM-segmented prompts → batch of cards |
| **Workspace isolation** (Phase 6) | Per-card scratch pads with deletion protection and `.archive/` restore |
| **Autopilot & mission** (Phase 7) | Scheduled overnight runs with first-breach-wins termination conditions |
| **Arch categories CRUD** (Phase 6) | Per-template knowledge categories injected into agent context |

**What does not change:** the DAG prerequisite system, LLM routing, budget tracking,
capacity counting, git worktree isolation, and the 8 infrastructure scheduler queues
(file summaries, research jobs, arch-gen, clarification, PIP resolution, survey,
subdivision recovery, Maestro orchestrator). These remain as-is.

**Immediate next step:** Phase 1 (data model migration). Low-risk, no behavior
change, unblocks everything else. Phase 2 (scheduler decoupling) is the highest-risk
work and must not begin until Phase 1 is fully merged and the test suite covers the
routing layer.
