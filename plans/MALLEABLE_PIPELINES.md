# Malleable Pipelines — Design Exploration

> **Status:** Brainstorm / Pre-RFC  
> **Author:** Exploration session, May 2026  
> **Goal:** Decouple pipeline definition from the scheduler so any project can run
> any workflow — software development, novel writing, research reports, data
> analysis, or anything else — with a visual node editor for composing pipelines.

---

## 1. The Problem With How We Are Built Today

The Maestro scheduler is a Wiggum Loop wired to one fixed pipeline:

```
IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION →
SECURITY → FINAL_REVIEW → HUMAN_REVIEW → COMPLETED
```

This is hardcoded in at least four places:

| Location | What's hardcoded |
|---|---|
| `scheduler.py` | Stage routing logic, agent dispatch per stage |
| `agent_loop.py` / `maestro.py` | Loop entry/exit conditions per stage type |
| `models.py` `Task.type` | String enum implicitly assumes software stages |
| `app/web/board.js` | Column ordering, column-to-stage mapping |

Every agent class (`PlanningAgent`, `DevOrchestrator`, `OptimizationAgent`, etc.)
is valid only in the context of software development. A novel-writing project has
no use for `SecurityReviewAgent`.

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

### 4.1 New Tables

```sql
-- A named, reusable pipeline topology
CREATE TABLE pipeline_templates (
    id          INTEGER PRIMARY KEY,
    name        TEXT    NOT NULL UNIQUE,
    description TEXT,
    is_default  BOOLEAN NOT NULL DEFAULT 0,
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  DATETIME,
    updated_at  DATETIME
);

-- One row per stage node in a template
CREATE TABLE pipeline_stages (
    id           INTEGER PRIMARY KEY,
    template_id  INTEGER NOT NULL REFERENCES pipeline_templates(id),
    stage_key    TEXT    NOT NULL,   -- machine identifier, e.g. "draft", "line_edit"
    label        TEXT    NOT NULL,   -- display name, e.g. "Draft"
    agent_type   TEXT    NOT NULL,   -- key into agent_registry
    position     INTEGER NOT NULL,   -- display order (left to right)
    group_id     INTEGER REFERENCES pipeline_stage_groups(id),
    config       JSON,               -- agent-specific overrides (tool list, gate type, retries, llm_id)
    UNIQUE(template_id, stage_key)
);

-- Stage groups (bracketed columns)
CREATE TABLE pipeline_stage_groups (
    id          INTEGER PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
    name        TEXT    NOT NULL,
    color       TEXT,               -- CSS colour token
    position    INTEGER NOT NULL    -- display order of the group itself
);

-- Directed edges between stages
CREATE TABLE pipeline_transitions (
    id            INTEGER PRIMARY KEY,
    template_id   INTEGER NOT NULL REFERENCES pipeline_templates(id),
    from_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
    to_stage_id   INTEGER NOT NULL REFERENCES pipeline_stages(id),
    condition     TEXT    NOT NULL,  -- "pass" | "fail" | "reject" | "always" | "skip"
    priority      INTEGER NOT NULL DEFAULT 0  -- tie-break when multiple edges match
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

A new route `/pipelines/{template_id}/edit` renders a full-canvas node editor:

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

**Node controls:**
- Double-click a node → open property panel (agent type, tools, gate config, LLM override)
- Drag from an output port → draw a new transition edge
- Right-click edge → set condition (pass / fail / reject / skip)
- Drag-select multiple nodes → group them
- Delete key → remove node or edge

**Property panel (slides in from right):**
```
Stage: "Drafting"
─────────────────────
Agent type:     [writing_agent       ▼]
Display label:  [Drafting              ]
LLM override:   [inherit from project ▼]

Gate type:      [llm_judge           ▼]
Max retries:    [3                    ]

Tools allowed:
  ☑ read_file    ☑ write_file
  ☐ web_search   ☐ run_pytest

Custom system prompt:
  ┌──────────────────────────────────┐
  │ You are an expert novelist...    │
  └──────────────────────────────────┘
```

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
    id            INTEGER PRIMARY KEY,
    name          TEXT NOT NULL UNIQUE,   -- used as agent_type key
    display_name  TEXT NOT NULL,
    description   TEXT,
    system_prompt TEXT NOT NULL,
    allowed_tools JSON NOT NULL,          -- list of tool keys
    gate_type     TEXT NOT NULL DEFAULT 'llm_judge',
    created_at    DATETIME
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

Today the flow is tightly typed: planning produces `PlanningResult`, which
`DevOrchestrator` consumes. In a custom pipeline, the "output" of stage N is
just the task's `content` JSON blob. We need a lightweight schema system — maybe
just a JSON Schema doc attached to each agent definition — so the editor can
warn when you wire incompatible stages together.

### Q2: What does "subdivide" mean in a custom pipeline?

Subdivision today creates child tasks of the same project. In a novel pipeline,
a "subdivide" on "Chapter 1 Draft" would create sub-tasks for each section.
The subdivision agent needs to know what stage the children start in (the
beginning of the pipeline, or the same stage as the parent?). This is a
per-pipeline config: `subdivision_entry_stage`.

### Q3: Git worktrees for non-code work

`DevOrchestrator` uses git branches for isolation. A writing agent working on
chapter files also benefits from isolation. The `worktree.py` module should be
generalised to work for any file-writing agent, not just software tasks.

### Q4: The human_gate agent and async blocking

`HumanGateAgent` needs to pause the task until the user approves via the UI.
This is fundamentally different from LLM-driven gates. The task sits in the
`human_review` column, the LLM slot is released, and the scheduler skips it
until a UI action fires. We already handle this today for `HUMAN_REVIEW` — it
just needs to be generalisable to any stage.

### Q5: Pipeline versioning and running tasks

If a project has 10 tasks in INDEV and you edit the pipeline template, what
happens to those tasks? Options:
- **Freeze**: tasks already dispatched continue on the template version at
  dispatch time. New tasks use the updated template.
- **Migrate**: all tasks are re-mapped to the new template (risky if stages
  were renamed or removed).
- **Snapshot**: templates are immutable once a task has been dispatched against
  them; edits create a new version.

The snapshot model (similar to Docker image layers) is safest: template edits
bump the `version` field and new tasks get the new version. In-flight tasks
stay on their version.

---

## 13. Implementation Phases

### Phase 0: Fix the scheduler's zombie problem (immediate, ~1 day)
The prerequisite bug — LLM awaits with no timeout, zombie detector blind to
alive-but-hung loops — needs to be fixed before we add more complexity.
See companion investigation notes.

### Phase 1: Data model + migration (~3 days)
- Add `pipeline_templates`, `pipeline_stages`, `pipeline_transitions`, `pipeline_stage_groups` tables.
- Migration that seeds the "Software Development" template from the current hardcoded pipeline.
- Add `stage_key` to tasks and project `pipeline_template_id` FK.
- Populate existing rows. System continues to work with zero visible change.

### Phase 2: Scheduler decoupling (~4 days)
- Extract stage-routing logic from `scheduler.py` into `pipeline_router.py`.
- Replace `if task.type == "planning":` dispatch chain with registry lookup.
- Replace `advance_task_type()` with `get_next_stage()` edge traversal.
- Existing tests pass on the Software Development template.

### Phase 3: Pipeline CRUD API (~2 days)
- REST endpoints for template CRUD.
- Endpoints to add/remove/update stages and edges within a template.
- Assign template to project endpoint.
- Template export/import as JSON.

### Phase 4: Frontend pipeline editor (~1 week)
- Canvas route `/pipelines/{id}/edit`.
- Draggable stage nodes with port-based edge wiring.
- Property panel per node.
- Kanban board derives columns from template (replaces hardcoded `columnMap`).

### Phase 5: Agent registry + custom agents (~3 days)
- `custom_agent_definitions` table.
- `CustomLLMAgent` class.
- Registry CRUD in UI.
- `WritingAgent`, `FactCheckerAgent` implementations.

### Phase 6: Cross-domain templates + gallery (~2 days)
- Ship the built-in templates (Novel Writing, Research Report, etc.).
- Template gallery UI page.

---

## 14. Risk Factors

**Scheduler complexity** — the dispatch loop is already 900+ lines. Decoupling
stage routing requires careful refactoring. Full test coverage of the routing
layer before touching the dispatch path.

**`task.type` ubiquity** — it's used in 50+ places. An additive migration is
the right call, but it means living with two sources of truth during the
transition. Set a hard deadline to deprecate `type`.

**Agent output contracts** — today agents produce typed results (PlanningResult,
etc.) that later agents consume. Loosening this to "task content JSON" requires
either a schema validator or accepting that mis-wired pipelines will fail at
runtime. A warning in the UI ("these stages have incompatible schemas") is
probably enough for now.

**ComfyUI canvas library** — we'd be building this from scratch or adopting a
library like [React Flow](https://reactflow.dev/) or
[Litegraph.js](https://github.com/jagenjo/litegraph.js) (what ComfyUI uses).
React Flow is MIT-licensed, well-maintained, and has first-class TypeScript
support. We'd need to introduce a JS build step (currently the frontend is
vanilla JS with no bundler). That's a small but real infrastructure step.

---

## 15. Summary

The core insight is that Maestro's scheduler is already general-purpose: it
dispatches agents to tasks, tracks capacity, manages budgets, and handles the
DAG. The only thing tying it to software development is the hardcoded stage →
agent mapping and the hardcoded column list in the frontend.

Replacing those two hardcoded things with a DB-backed pipeline template system
— and exposing that system through a visual ComfyUI-style editor — gives users
a fully malleable orchestration platform. The scheduler, DAG, budget system, git
worktree isolation, and LLM routing all work unchanged. The agents themselves
(planning, implementation, review) become entries in a registry that pipeline
templates reference by key.

The immediate next step is Phase 0: fixing the zombie session bugs that are
blocking the current pipeline. Phases 1 and 2 follow immediately after as they
are low-risk schema additions that lay the foundation without visible behaviour
change.
