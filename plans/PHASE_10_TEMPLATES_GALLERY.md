# Phase 10 — Cross-Domain Templates & Gallery

> **Status:** Not started — requires Phases 1–5  
> **Depends on:** All prior phases (templates must be fully functional before shipping examples)  
> **Estimated effort:** 2 days  
> **Goal:** Ship built-in pipeline templates for major use cases; add a gallery page
> for browsing, cloning, and assigning templates to projects.

---

## Built-In Templates

These are seeded into `pipeline_templates` by a migration and are marked
`is_builtin = true` (add this column to `pipeline_templates`). Built-in templates
cannot be deleted, but can be cloned and modified.

### Software Development (existing behavior, unchanged)

```
IDEA → Planning → Implementation → Conceptual Review →
  [Optimization || Security] → Final Review → Human Review → COMPLETED
```

Arch categories: Platform, Design, Testing, Performance, API, Data, Tooling,
Security, DevOps, Documentation, Quality, Cost, Scalability, General

---

### Novel Writing

```
IDEA
 └─► Outline (planning_agent)
      │ pass
      ▼
 Chapter Factory (factory_node: manual_prompt, LLM-segmented)
      │ creates N chapter draft cards
      ▼
 Chapter Draft (writing_agent, per-chapter)
      │ pass
      ▼
 Continuity Check (custom_agent: reads all chapter drafts, flags inconsistencies)
      │ pass          │ fail
      ▼               └─► Chapter Draft (retry)
 Line Edit (writing_agent)
      │ pass
      ▼
 Human Review (human_gate)
      │ approve
      ▼
 PUBLISHED
```

Arch categories: Characters, Themes, Plot, World Building, Timeline, Voice/Style,
Research Notes, Continuity Log

Key stage configs:
- **Continuity Check**: `arch_category_keys: ["characters", "timeline"]` — agent
  receives character profiles and timeline events in its context
- **Chapter Draft**: `required_input_keys: ["outline"]` — blocked until the
  Outline stage has written `outline` to the task content blob

---

### Research Report

```
IDEA
 └─► Topic Refinement (planning_agent)
      │ pass
      ▼
 Research (research_agent, web_search tools)
      │ pass
      ▼
 Outline (planning_agent)
      │ pass
      ▼
 Draft (writing_agent)
      │ pass             │ fail
      ▼                  └─► Draft (retry)
 Fact Check (custom_agent: verifies claims against research docs)
      │ pass
      ▼
 Formatting (writing_agent)
      │ pass
      ▼
 Human Review (human_gate)
      │ approve
      ▼
 PUBLISHED
```

Arch categories: Sources, Key Claims, Methodology, Glossary, Open Questions

---

### Data Analysis

```
IDEA
 └─► Question Refinement (planning_agent)
      ├─► Data Collection (research_agent)
      └─► Schema Design (planning_agent)        ← parallel group
           │ both complete
           ▼
 Analysis (custom_agent: python_sympy verifier optional)
      │ pass
      ▼
 Visualization (custom_agent: plot generation tools)
      │ pass
      ▼
 Write-Up (writing_agent)
      │ pass
      ▼
 Human Review (human_gate)
      │ approve
      ▼
 COMPLETED
```

Arch categories: Datasets, Hypotheses, Statistical Methods, Findings, Caveats

---

### Mathematics / Proof Exploration

```
IDEA
 └─► Problem Statement (planning_agent)
      │ required_input_keys: []
      │ output_keys: ["problem_statement", "known_results"]
      ▼
 Approach Factory (factory_node: manual_prompt, LLM-segmented)
      │ creates N attack-angle cards
      ▼
 Approach Planning (planning_agent, per-angle)
      │ output_keys: ["approach_plan"]
      ▼
 Proof Attempt (custom_agent, verifier=python_sympy or none)
      │ pass                   │ fail
      ▼                        └─► Approach Planning (retry)
 Peer Review (custom_agent: reads all completed proof attempts from doc store)
      │ pass      │ reject
      ▼            └─► Proof Attempt (retry with peer feedback)
 Synthesis (custom_agent: combines partial results)
      │ pass
      ▼
 Human Review (human_gate)
      │ approve
      ▼
 ACCEPTED
```

Arch categories: Known Theorems, Definitions, Conjectures, Failed Approaches,
Partial Results, Open Sub-Problems

Key design: the **document store** (Phase 8) is the coordination layer. Each
Proof Attempt card writes its result to `proofs/{approach_name}`. The Synthesis
card reads all `proofs/*` documents and assembles the final result.

---

### Bug Triage

```
BUG REPORT
 └─► Reproduce (custom_agent: writes repro script)
      │ pass            │ fail (cannot reproduce)
      ▼                  └─► WONTFIX (terminal)
 Root Cause (custom_agent: reads repro output)
      │ pass
      ▼
 Fix (implementation_agent)
      │ pass
      ▼
 Regression Test (custom_agent: run_pytest verifier)
      │ pass      │ fail
      ▼            └─► Fix (retry)
 Human Review (human_gate)
      │ approve
      ▼
 RESOLVED
```

---

### Overnight Generation (Story Factory)

A meta-template for long-running autonomous story generation:

```
SEED PROMPT
 └─► Story Bible (planning_agent: establishes characters, world, arc)
      │ pass
      ▼
 Chapter Factory (factory_node: cron, LLM-segmented, cron=nightly)
      │ creates N chapter outline cards per night
      ▼
 Chapter Outline (planning_agent)
      │ pass
      ▼
 Chapter Draft (writing_agent)
      │ pass
      ▼
 Continuity Check (custom_agent)
      │ pass      │ fail
      ▼            └─► Chapter Draft (retry)
 CHAPTER ARCHIVE (completed)
```

Intended to be run with autopilot + scheduled hours (23:00–07:00) + token budget
termination. The factory's cron trigger ensures new chapter work is queued each
night. The system runs until the token budget is exhausted or morning arrives.

---

## Gallery Page

Route: `GET /pipelines`

```
╔══════════════════════════════════════════════════════════════╗
║  Pipeline Templates                    [+ New Template]      ║
╠══════════════════════════════════════════════════════════════╣
║                                                              ║
║  ┌─────────────────────┐  ┌─────────────────────┐           ║
║  │ 📋 Software Dev     │  │ ✍ Novel Writing     │           ║
║  │ 9 stages            │  │ 7 stages            │           ║
║  │ Built-in            │  │ Built-in            │           ║
║  │ [Use] [Clone][Edit] │  │ [Use] [Clone][Edit] │           ║
║  └─────────────────────┘  └─────────────────────┘           ║
║                                                              ║
║  ┌─────────────────────┐  ┌─────────────────────┐           ║
║  │ 🔬 Research Report  │  │ 📊 Data Analysis    │           ║
║  │ 8 stages            │  │ 6 stages            │           ║
║  │ Built-in            │  │ Built-in            │           ║
║  │ [Use] [Clone][Edit] │  │ [Use] [Clone][Edit] │           ║
║  └─────────────────────┘  └─────────────────────┘           ║
║                                                              ║
║  [+ Create from scratch]   [Import JSON]                     ║
╚══════════════════════════════════════════════════════════════╝
```

**[Use]** — assign to the current project (or open a project picker if no project
is selected). Triggers the stage-key migration for any existing tasks.

**[Clone]** — creates a copy under a new name, opens in the editor.

**[Edit]** — opens the Litegraph canvas editor directly. Built-in templates show a
warning banner: "Editing a built-in template. Clone it first to make a private copy."
(Edit is still allowed — the `is_builtin` flag only blocks deletion.)

---

## `is_builtin` Column

```sql
ALTER TABLE pipeline_templates ADD COLUMN is_builtin BOOLEAN NOT NULL DEFAULT 0;
```

Built-in templates are seeded with `is_builtin=1`. The delete endpoint returns 400
if `is_builtin=1`. The editor shows a non-blocking warning banner.

---

## Test Criteria

- `GET /pipelines` returns all built-in templates plus any user-created ones
- Clone a built-in template → new template created with `is_builtin=0`, all stages
  and transitions copied
- Assign "Novel Writing" template to a project with existing tasks → all tasks get
  `stage_key` migrated, kanban columns update to novel writing stages
- Delete a built-in template → 400 error
- Export "Mathematics" template as JSON → import it under a new name → identical
  stage graph

---

## Risk Factors

**Built-in template drift** — if a bug is found in a built-in template's stage
config (e.g. wrong `required_input_keys`), the fix must be applied both to the
seeded data and to any projects already using that template. Add a
`check_builtin_templates()` startup function that compares the live DB state of
built-in templates against the expected seed data and logs a warning (not an
auto-fix) if they diverge.
