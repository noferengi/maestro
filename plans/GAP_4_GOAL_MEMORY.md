# Gap 4 — Goal memory / persistent objectives

**Status:** Planning  
**Effort:** Medium  
**Priority:** Medium — required for multi-tick autonomous goal pursuit

## Problem

Each Maestro run starts fresh from DB task state. The document store and arch cards
provide some continuity, but there is no explicit persistent record of what Maestro is
trying to accomplish long-term, what evidence it has accumulated toward that goal, or
how far along it is. Progress is lost on server restart. Maestro cannot distinguish
"I have been working on this for 3 weeks and here is what I know" from "I have never
seen this project before."

## Rough phases

1. Storage design — new table vs. document store vs. special card type
2. Objective lifecycle — creation, progress update, completion, archival
3. Maestro decision prompt integration — how objectives enter each tick
4. Progress signals — what events count as evidence of advancement
5. Objective-to-card linkage

## Open questions

### Storage design
- New `maestro_objectives` table with typed columns, or keyed documents in the existing
  `project_documents` store (e.g., key = `"objective:twin_prime"`)?
- A dedicated table gives migrations, query support, and typed status fields. The
  document store gives zero schema work and immediate availability.
- If a new table: what columns are required at minimum? Candidates: `id`, `project_id`,
  `goal_statement`, `status` (active / paused / completed / abandoned), `evidence`
  (accumulated text), `success_criteria`, `created_by` (human / maestro), `priority`,
  `created_at`, `last_advanced_at`.
- Should sub-objectives be supported (parent_id FK)? Or is that scope creep for now?

### Objective lifecycle
- Who creates objectives? Human via UI, Maestro autonomously during survey mode,
  or both? If Maestro can create its own objectives, what prevents unbounded growth?
- How does Maestro mark an objective complete? Autonomous decision, human approval,
  or only when explicit success criteria are met?
- What is "abandonment"? After N ticks with no progress signal? After the project
  is deleted? Should abandoned objectives be hard-deleted or soft-archived?

### Decision prompt integration
- How do objectives enter each Maestro tick? Options: top-N objectives injected as
  a block in the system prompt, Maestro reads them via a tool call (`list_objectives`),
  or objectives are summarized into the project summary and arrive that way.
- Should Maestro be able to *update* objective evidence during a tick (write a tool
  call to `append_objective_evidence`), or does evidence accumulate only from
  post-tick processing based on DB state changes?
- How many active objectives should be in context at once before it becomes noise?

### Progress signals
- What events count as evidence that an objective is advancing? Options: task stage
  completions, document store writes, git commits to the project branch, completed
  cards tagged with the objective ID, or an explicit LLM assessment call.
- Should progress be a number (0–100%), a milestone list, a free-text evidence log,
  or a combination?
- How does Maestro detect it is stuck on an objective (creating the same type of
  card repeatedly without completion) vs. making genuine headway?

### Card linkage
- Should individual task cards carry a reference to which objective they serve
  (e.g., `objective_id` FK on `Task`)? This would let Maestro query "all cards for
  objective X" without scanning free-text.
- Or is the linkage implicit — Maestro infers objective progress from overall board
  state and accumulated documents rather than explicit FK relationships?
- If there is an explicit FK, what happens to the objective when all its linked cards
  are completed or deleted?
