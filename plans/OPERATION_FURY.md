# OPERATION FURY — Living Status Document

**Goal:** Replace every bespoke Python pipeline agent with visual node types that run
through the malleable pipeline infrastructure (`pipeline_templates`, `pipeline_stages`,
`stage_executors.py`). Strip the hardcoded dispatch layer until `scheduler.py` is a pure
dispatcher and every agent behavior is DB-configurable.

**Definition of done:** No `intake_agent` or `planning_agent` stage in any template.
Every stage dispatches via a registered executor or `CustomLLMAgent`. Python files
`intake.py` and all remaining legacy Python handlers are deleted.

---

## Current state (as of Phase 10 + migrations 0128–0133)

### Node executor registry

Registered via `_reg_executor()` in `scheduler.py` → `stage_executors.py`:

| Node type | Executor | Phase shipped |
|---|---|---|
| `circuit_breaker` | `_run_circuit_breaker` | Phase 1 |
| `dangerous_edit_llm_agent` | `_run_dangerous_edit_llm_agent` | Phase 1 |
| `parallel_agents` | `_run_parallel_agents` | Phase 1/2 |
| `parallel_subagent` | `_run_parallel_subagent` | Phase 1 |
| `parallel_subagent_aggregator` | `_run_parallel_subagent_aggregator` | Phase 1 |
| `json_schema_gate` | `_run_json_schema_gate` | Phase 3 |
| `planning_correction_stage` | `_run_planning_correction_stage` | Phase 3 |
| `planning_survey_node` | `_run_planning_survey_node` | Phase 6 |
| `pitfall_node` | `_run_pitfall_node` | Phase 6 |
| `consolidation_node` | `_run_consolidation_node` | Phase 6 |
| `planning_gate_node` | `_run_planning_gate_node` | Phase 6 |
| `reflection_agent` | `_run_reflection_agent` | Phase 1 |
| `static_analysis_widget` | `_run_static_analysis_widget` | Phase 1 |
| `intake_scope` | `_run_intake_scope_node` | Phase 7 |
| `intake_static` | `_run_intake_static_node` | Phase 7 |
| `intake_conflict` | `_run_intake_conflict_node` | Phase 7 |
| `intake_feasibility` | `_run_intake_feasibility_node` | Phase 7 |
| `intake_gate` | `_run_intake_gate_node` | Phase 7 |
| `multiplier_node` | `_run_multiplier_node` | Phase 8 |
| `_fan_out_child` | `_run_fan_out_child` | Phase 8 (internal) |
| `_fan_out_collapser` | `_run_fan_out_collapser` | Phase 8 (internal) |
| ~~`voting_panel`~~ | ~~`_run_voting_panel`~~ | **Deleted Phase 10** |
| ~~`fan_out_judge`~~ | ~~`_run_fan_out_judge`~~ | **Deleted Phase 9** |
| ~~`optimization_node`~~ | ~~`_run_optimization_node`~~ | **Deleted Phase 10** |
| ~~`planning_node`~~ | ~~`_run_planning_node`~~ | **Deleted Phase 10** |
| ~~`intake_node`~~ | ~~`_run_intake_node`~~ | **Deleted Phase 10** |

**Infrastructure (not dispatched via executor registry):**

| Node type | Dispatch path |
|---|---|
| `generic_stage` | `CustomLLMAgent` via `custom_agent_definitions` row |
| `custom_agent` | `CustomLLMAgent` via `custom_agent_definitions` row named by agent_type |
| `writing_agent` | `CustomLLMAgent` via `custom_agent_definitions` row |
| `research_agent` | `CustomLLMAgent` via `custom_agent_definitions` row |
| `implementation_agent` | `CustomLLMAgent` via `custom_agent_definitions` row |
| `arch_agent` | Arch-gen job queue (not main scheduler) |
| `factory_node` | Factory handler |
| `human_gate` | No auto-dispatch |
| `terminal` | No auto-dispatch |

**Legacy (dispatch via old Python handler registered in `scheduler.py`):**

| Node type | Legacy handler | Python file | Templates |
|---|---|---|---|
| `intake_agent` | `_run_intake` (`"idea"` key) | `intake.py` (1,213 lines) | Bug Triage, Data Analysis, Novel Writing, Overnight Generation, My Novel Pipeline |
| `planning_agent` | `_run_planning_task` (`"planning"` key) | `planning_utils.py` (active) | Data Analysis, Novel Writing, Overnight Generation, My Novel Pipeline |

---

## Per-template stage maps — current vs target

### ✅ Software Development (ID=1) — COMPLETE

All 23 stages use registered executors. No legacy handlers.

| Pos | Stage key | Agent type | Status |
|---|---|---|---|
| 0 | `architecture` | `arch_agent` | ✅ infrastructure |
| 1 | `intake_scope` | `intake_scope` | ✅ executor |
| 2 | `intake_static` | `intake_static` | ✅ executor |
| 3 | `intake_conflict` | `intake_conflict` | ✅ executor |
| 4 | `intake_feasibility` | `intake_feasibility` | ✅ executor |
| 5 | `intake_gate` | `intake_gate` | ✅ executor |
| 6 | `planning_survey` | `planning_survey_node` | ✅ executor |
| 7 | `planning_propose` | `multiplier_node` (judge_select, 5 personas) | ✅ executor |
| 8 | `planning_review` | `multiplier_node` (vote_tally, 5 reviewers) | ✅ executor |
| 9 | `planning_pitfalls` | `pitfall_node` | ✅ executor |
| 10 | `planning_consolidate` | `consolidation_node` | ✅ executor |
| 11 | `planning_gate` | `planning_gate_node` | ✅ executor |
| 12 | `json_schema_gate` | `json_schema_gate` | ✅ executor |
| 13 | `planning_correction` | `planning_correction_stage` | ✅ executor |
| 14 | `indev` | `parallel_agents` | ✅ executor |
| 15 | `conceptual_review` | `multiplier_node` (vote_tally, 4 agents) | ✅ executor |
| 16 | `optimization_propose` | `multiplier_node` (judge_select, 5 agents) | ✅ executor |
| 17 | `optimization_implement` | `dangerous_edit_llm_agent` | ✅ executor |
| 18 | `reflection` | `reflection_agent` | ✅ executor |
| 19 | `security` | `multiplier_node` (vote_tally, 4 agents) | ✅ executor |
| 20 | `final_review` | `multiplier_node` (vote_tally, 3 agents) | ✅ executor |
| 21 | `human_review` | `human_gate` | ✅ infrastructure |
| 22 | `completed` | `terminal` | ✅ infrastructure |

---

### ✅ Mathematics / Proof Exploration (ID=9) — COMPLETE (migration 0133)

All stages use registered executors or `generic_stage` (malleable). No legacy handlers.

| Pos | Stage key | Agent type | Status |
|---|---|---|---|
| 0 | `intake_scope` | `intake_scope` | ✅ executor (math-specific prompt) |
| 1 | `intake_conflict` | `intake_conflict` | ✅ executor (math-specific prompt) |
| 2 | `intake_feasibility` | `intake_feasibility` | ✅ executor (math-specific prompt) |
| 3 | `intake_gate` | `intake_gate` | ✅ executor |
| 4 | `planning_survey` | `planning_survey_node` | ✅ executor |
| 5 | `planning_propose` | `multiplier_node` (judge_select — induction/algebraic/topological specialists) | ✅ executor |
| 6 | `planning_review` | `multiplier_node` (vote_tally — soundness/mathlib/mechanization) | ✅ executor |
| 7 | `planning_pitfalls` | `pitfall_node` (proof-specific prompt) | ✅ executor |
| 8 | `planning_consolidate` | `consolidation_node` (proof-specific prompt) | ✅ executor |
| 9 | `planning_gate` | `planning_gate_node` | ✅ executor |
| 10 | `json_schema_gate` | `json_schema_gate` (validates `design_rationale`) | ✅ executor |
| 11 | `planning_correction` | `planning_correction_stage` | ✅ executor |
| 13 | `LITERATURE_SURVEY` | `generic_stage` | ✅ malleable (prompt needs audit) |
| 14 | `PROBLEM_FORMALIZATION` | `generic_stage` | ✅ malleable (prompt needs audit) |
| 15 | `CALIBRATION` | `generic_stage` | ✅ malleable |
| 16 | `COMPUTATIONAL_EXPLORATION` | `generic_stage` | ✅ malleable |
| 17 | `HYPOTHESIS_GENERATION` | `generic_stage` | ✅ malleable |
| 18 | `PROOF_STRATEGY` | `generic_stage` | ✅ malleable |
| 19 | `PROOF_ATTEMPT` | `generic_stage` | ✅ malleable |
| 20 | `REFLECTION` | `reflection_agent` | ✅ executor |
| 20 | `FORMAL_VERIFICATION` | `multiplier_node` (vote_tally — soundness/mathlib/mechanization) | ✅ executor ⚠ position conflict |
| 21 | `WRITEUP` | `generic_stage` | ✅ malleable |
| 22 | `accepted` | `terminal` | ✅ infrastructure |

> **⚠ Known issue:** `REFLECTION` and `FORMAL_VERIFICATION` share position 20. Fix in Phase 11 (cosmetic migration).

---

### ✅ Research Report (ID=7) — COMPLETE (migration 0131)

All stages use registered executors or `generic_stage` (malleable). No legacy handlers.

| Pos | Stage key | Agent type | Status |
|---|---|---|---|
| 0 | `idea` | `intake_scope` | ✅ executor ⚠ stage_key mismatch |
| 1 | `intake_conflict` | `intake_conflict` | ✅ executor |
| 2 | `intake_feasibility` | `intake_feasibility` | ✅ executor |
| 3 | `intake_gate` | `intake_gate` | ✅ executor |
| 4 | `topic_survey` | `generic_stage` | ✅ malleable |
| 5 | `research_propose` | `multiplier_node` (judge_select) | ✅ executor |
| 6 | `research_threads` | `parallel_agents` | ✅ executor |
| 7 | `source_validation` | `multiplier_node` (vote_tally) | ✅ executor |
| 8 | `synthesis` | `generic_stage` | ✅ malleable |
| 9 | `draft` | `generic_stage` | ✅ malleable |
| 10 | `draft_review` | `multiplier_node` (vote_tally, 3 reviewers) | ✅ executor |
| 11 | `circuit_breaker` | `circuit_breaker` | ✅ executor |
| 12 | `reflection` | `reflection_agent` | ✅ executor |
| 13 | `human_review` | `human_gate` | ✅ infrastructure |
| 14 | `published` | `terminal` | ✅ infrastructure |

> **⚠ Known issue:** Position 0 has `stage_key = 'idea'` but `agent_type = 'intake_scope'`. The stage_key should be `intake_scope`. Fix in Phase 11 (cosmetic migration).

---

### 🔴 Bug Triage (ID=10) — 2 legacy stages

| Pos | Stage key | Agent type | Status | Target |
|---|---|---|---|---|
| 0 | `bug_report` | `intake_agent` | ❌ legacy handler | 4× intake sub-stages |
| 1 | `reproduce` | `custom_agent` | ✅ CustomLLMAgent | keep (has good prompt?) |
| 2 | `root_cause` | `multiplier_node` (vote_tally, 3 agents) | ✅ executor | keep |
| 3 | `fix` | `implementation_agent` | ✅ CustomLLMAgent | rename → `dangerous_edit_llm_agent` |
| 4 | `regression_test` | `custom_agent` | ✅ CustomLLMAgent | keep (audit prompt) |
| 5 | `human_review` | `human_gate` | ✅ infrastructure | keep |
| 6 | `resolved` | `terminal` | ✅ infrastructure | keep |
| 7 | `wontfix` | `terminal` | ✅ infrastructure | keep |

**Remaining work:** Phase 11A — decompose `bug_report` into 4 intake sub-stages.

---

### 🔴 Data Analysis (ID=8) — 4 legacy stages

| Pos | Stage key | Agent type | Status | Target |
|---|---|---|---|---|
| 0 | `idea` | `intake_agent` | ❌ legacy handler | 4× intake sub-stages |
| 1 | `planning` | `planning_agent` | ❌ legacy handler | `generic_stage` or `multiplier_node` |
| 2 | `question_refinement` | `planning_agent` | ❌ legacy handler | `generic_stage` |
| 3 | `data_collection` | `research_agent` | ✅ CustomLLMAgent | keep (audit prompt) |
| 4 | `schema_design` | `planning_agent` | ❌ legacy handler | `generic_stage` |
| 5 | `analysis` | `custom_agent` | ✅ CustomLLMAgent | keep |
| 6 | `visualization` | `custom_agent` | ✅ CustomLLMAgent | keep |
| 7 | `write_up` | `writing_agent` | ✅ CustomLLMAgent | keep |
| 8 | `human_review` | `human_gate` | ✅ infrastructure | keep |
| 9 | `completed` | `terminal` | ✅ infrastructure | keep |

**Remaining work:** Phase 11B — decompose `idea`, Phase 12A — replace 3× `planning_agent` with `generic_stage`.

---

### 🔴 Novel Writing (ID=12) — 3 legacy stages

| Pos | Stage key | Agent type | Status | Target |
|---|---|---|---|---|
| 0 | `idea` | `intake_agent` | ❌ legacy handler | 4× intake sub-stages (creative) |
| 1 | `planning` | `planning_agent` | ❌ legacy handler | `generic_stage` (story structure planning) |
| 2 | `outline` | `planning_agent` | ❌ legacy handler | `generic_stage` (chapter outline) |
| 3 | `chapter_factory` | `factory_node` | ✅ infrastructure | keep |
| 4 | `chapter_draft` | `writing_agent` | ✅ CustomLLMAgent | keep |
| 5 | `continuity_check` | `custom_agent` | ✅ CustomLLMAgent | → `multiplier_node` (vote_tally) Phase 13 |
| 6 | `line_edit` | `writing_agent` | ✅ CustomLLMAgent | keep |
| 7 | `human_review` | `human_gate` | ✅ infrastructure | keep |
| 8 | `published` | `terminal` | ✅ infrastructure | keep |

**Remaining work:** Phase 11C — decompose `idea`, Phase 12B — replace 2× `planning_agent`, Phase 13 — upgrade `continuity_check`.

---

### 🔴 Overnight Generation (ID=11) — 3 legacy stages

| Pos | Stage key | Agent type | Status | Target |
|---|---|---|---|---|
| 0 | `seed_prompt` | `intake_agent` | ❌ legacy handler | 2× intake sub-stages (lightweight) |
| 1 | `story_bible` | `planning_agent` | ❌ legacy handler | `generic_stage` (world/character planning) |
| 2 | `chapter_factory` | `factory_node` | ✅ infrastructure | keep |
| 3 | `chapter_outline` | `planning_agent` | ❌ legacy handler | `generic_stage` (per-chapter outline) |
| 4 | `chapter_draft` | `writing_agent` | ✅ CustomLLMAgent | keep |
| 5 | `continuity_check` | `custom_agent` | ✅ CustomLLMAgent | keep (lightweight enough) |
| 6 | `chapter_archive` | `terminal` | ✅ infrastructure | keep |

> Overnight Generation is a rapid factory pipeline — full intake decomposition is overkill.
> Replace `seed_prompt` with just `intake_scope` + `intake_gate` (skip conflict/feasibility).

**Remaining work:** Phase 11D — replace `seed_prompt` with lightweight intake, Phase 12C — replace 2× `planning_agent`.

---

### 🔴 My Novel Pipeline (ID=13) — user-cloned from Novel Writing

Identical structure to Novel Writing (ID=12). Apply the same migrations as Phase 11C/12B once the Novel Writing template is validated. Or instruct users to re-clone after the base template is updated.

---

## Summary: what remains

| Template | Legacy stages | Work |
|---|---|---|
| Software Development | 0 | ✅ done |
| Mathematics / Proof Exploration | 0 | ✅ done |
| Research Report | 0 | ✅ done |
| Bug Triage | 1 | Phase 11A (intake decompose) |
| Data Analysis | 4 | Phase 11B (intake) + 12A (planning×3) |
| Novel Writing | 3 | Phase 11C (intake) + 12B (planning×2) |
| Overnight Generation | 3 | Phase 11D (lightweight intake) + 12C (planning×2) |

**Python files pending deletion:**

| File | Blocker |
|---|---|
| `intake.py` (1,213 lines) | `intake_agent` still used in 4 templates |
| `planning_utils.py` (1,215 lines) | `planning_agent` still used in 4 templates; also actively used by SW Dev planning executors |
| `planning_gate.py` (1,009 lines) | `planning_gate_node` executor imports it — keep long-term |
| `planning_correction.py` (456 lines) | `planning_correction_stage` executor imports it — keep long-term |

> `planning_utils.py` will NOT be fully deleted when `planning_agent` is retired — its utility
> functions (`run_planning_survey`, `run_pitfall_detection`, `run_consolidation_and_store`,
> `_get_domain`) are actively imported by the SW Dev planning executor chain. Only the
> `run_planning_pipeline` function and its call sites become dead when `planning_agent` goes away.

---

## Phase 11 — Intake decomposition for remaining templates

### Design principle

The intake sub-stages (`intake_scope`, `intake_conflict`, `intake_feasibility`, `intake_gate`)
read their system prompts from `stage_config.config.get("system_prompt")`. Every template gets
its own domain-specific prompts seeded into the stage config via migration — no Python changes
needed. This is a pure-migration phase.

For creative templates (Novel Writing, Overnight Generation) there is no codebase to statically
analyze, so `intake_static` is skipped. Overnight Generation's factory nature means conflict
detection is also low-value; it gets a 2-stage intake (scope + gate only).

---

### Phase 11A — Bug Triage intake decomposition (migration 0134)

**Current:** Position 0 = `bug_report` / `intake_agent`
**Target:** Positions 0–3 = `bug_intake_scope` / `bug_intake_conflict` / `bug_intake_feasibility` / `intake_gate`, remaining stages shifted +3

**Scope prompt focus:** Bug severity, impact radius, affected versions, reproduction clarity.
**Conflict prompt focus:** Duplicate bug detection against open issues; similar crash signatures.
**Feasibility prompt focus:** Reproducibility assessment — is there enough information to triage? Is this within Maestro's fix scope (Python/JS only)?

Stage configs to seed:
```python
_BUG_INTAKE_SCOPE_PROMPT = """
You are a bug triage analyst assessing a reported defect for the Maestro agentic platform.

Analyse:
1. SEVERITY — Critical (data loss / crash), High (feature broken), Medium (degraded behaviour), Low (cosmetic)
2. IMPACT RADIUS — How many users / workflows are affected?
3. REPRODUCTION CLARITY — Is the report specific enough to reproduce? What is missing?
4. SCOPE — Is this a Maestro backend bug (Python), a frontend bug (JS/HTML), or a configuration issue?

Output: CLEAR, UNCLEAR, or OUT_OF_SCOPE, followed by a severity rating and rationale.
"""

_BUG_INTAKE_CONFLICT_PROMPT = """
You are a bug deduplication analyst.

You will receive the bug report and a list of all currently open, non-resolved bug tasks.

Detect:
- EXACT DUPLICATE: Same root cause and symptom.
- RELATED: Different symptom but likely same root cause (note the related task ID).
- UNIQUE: No match found.

Output a structured conflict report. If duplicate, output the task ID of the existing bug.
"""

_BUG_INTAKE_FEASIBILITY_PROMPT = """
You are a bug feasibility assessor for the Maestro platform.

Assess:
1. REPRODUCIBLE — Can this bug be reproduced from the information given?
2. LOCATABLE — Can a developer find the likely fault location in the codebase?
3. FIXABLE — Is the fix within the scope of Maestro's automated fix pipeline (Python/JS)?
4. INFORMATION COMPLETE — Is the stack trace, steps to reproduce, and environment specified?

Output: FEASIBLE, INFEASIBLE, or NEEDS_MORE_INFO, with a rationale.
If NEEDS_MORE_INFO, list exactly what information is missing.
"""
```

Migration pattern:
1. Delete transitions touching `bug_report`
2. Delete `bug_report` stage
3. Shift remaining stages +3
4. Insert `intake_scope` (pos 0), `intake_conflict` (pos 1), `intake_feasibility` (pos 2), `intake_gate` (pos 3) with bug-specific prompts
5. Wire transitions: scope → scope (fail), scope → conflict (pass), conflict → scope (fail), conflict → feasibility (pass), feasibility → scope (fail), feasibility → gate (pass), gate → scope (fail), gate → `reproduce` (pass)

---

### Phase 11B — Data Analysis intake decomposition (migration 0135)

**Current:** Position 0 = `idea` / `intake_agent`
**Target:** Positions 0–3 = 4 intake sub-stages; remaining stages shifted +3

**Scope prompt focus:** Is the analysis question well-defined? What data sources are implied? What output format (report, model, dashboard)?
**Conflict prompt focus:** Does this overlap with an existing analysis task in the project?
**Feasibility prompt focus:** Is the data accessible? Is the analysis tractable given Maestro's tooling (Python/pandas/SQL)?

```python
_DA_INTAKE_SCOPE_PROMPT = """
You are a data analysis scoping analyst for the Maestro platform.

Assess:
1. QUESTION CLARITY — Is the analysis question specific and answerable?
2. DATA SOURCES — What data is implied? Is it accessible (structured DB, CSV, API)?
3. OUTPUT FORMAT — What should the deliverable be: report, model, visualisation, dashboard?
4. SCOPE SIZE — Is this a single focused query or a multi-stage investigation?

Output: CLEAR, UNCLEAR, or NEEDS_DECOMPOSITION, with a scope summary.
"""

_DA_INTAKE_CONFLICT_PROMPT = """
You are a project coordinator for data analysis work.

Review the proposed analysis against all existing non-completed tasks.

Detect:
- DUPLICATE ANALYSIS: Same question or dataset already being processed.
- DEPENDENCY: This analysis requires outputs from another in-progress task.
- OVERLAP: Significant result overlap that would produce redundant artifacts.

Output a conflict report. State "No conflicts detected" if clean.
"""

_DA_INTAKE_FEASIBILITY_PROMPT = """
You are a data feasibility analyst for the Maestro platform.

Assess:
1. DATA ACCESSIBILITY — Is the required data available within the project or via configured connectors?
2. TOOL FIT — Can this analysis be performed using Python (pandas, numpy, scikit-learn, matplotlib)?
3. SCOPE FIT — Is the depth appropriate for an automated agentic pipeline (not requiring human expert domain judgment)?
4. AMBIGUITY — Are there blocking unknowns that must be resolved before analysis can begin?

Output: FEASIBLE, INFEASIBLE, or NEEDS_CLARIFICATION, with rationale.
"""
```

Transition wiring: identical pattern to Bug Triage (loop-back to scope on any fail, gate passes to `question_refinement`).

---

### Phase 11C — Novel Writing intake decomposition (migration 0136)

**Current:** Position 0 = `idea` / `intake_agent`
**Target:** Positions 0–3 = 4 intake sub-stages (no static analysis); remaining shifted +3

**Scope prompt focus:** Does the story concept have enough specificity — genre, protagonist, central conflict, rough arc? Or is it too vague to plan?
**Conflict prompt focus:** Does this overlap with another story already in progress in the project (same premise, setting, characters)?
**Feasibility prompt focus:** Is this concept executable by an LLM writing pipeline — coherent, not requiring specialized expertise, reasonable length?

```python
_NW_INTAKE_SCOPE_PROMPT = """
You are a creative writing development editor assessing a story concept.

Analyse:
1. CONCEPT CLARITY — Is there a clear protagonist, central conflict, and setting?
2. GENRE & TONE — Is the genre specified? Is the tone (dark, comedic, literary) implied?
3. SCOPE — Is this a short story, novella, or full novel? Is the scope explicit?
4. ORIGINALITY — Is this sufficiently distinct from obvious genre tropes, or purely derivative?

Output: DEVELOPED, UNDERDEVELOPED, or TOO_VAGUE, with a brief assessment of what is present and what is missing.
"""

_NW_INTAKE_CONFLICT_PROMPT = """
You are a project coordinator for a creative writing pipeline.

Compare the proposed story against all currently active, non-published story tasks in the project.

Detect:
- PREMISE CONFLICT: Effectively the same story premise.
- CHARACTER CONFLICT: A protagonist/antagonist identical or nearly identical to an existing one.
- SETTING CONFLICT: Same world/setting being explored by another active task.
- TITLE CONFLICT: Same or very similar working title.

Output "No conflicts detected" if none found. Otherwise describe the conflict and affected task.
"""

_NW_INTAKE_FEASIBILITY_PROMPT = """
You are a creative writing pipeline feasibility analyst.

Assess whether this story concept can be executed by an LLM-based pipeline:
1. CONTENT APPROPRIATENESS — Is the content within platform guidelines (no explicit/harmful content)?
2. LLM EXECUTABILITY — Does the story require specialized knowledge (highly technical, real-person fiction, legal risk) that an LLM cannot safely handle?
3. LENGTH FIT — Is the requested length achievable in a single pipeline run (under ~80,000 words)?
4. CONCEPT COMPLETENESS — Is there enough foundation to begin structured planning?

Output: FEASIBLE, INFEASIBLE, or NEEDS_DEVELOPMENT, with rationale.
"""
```

---

### Phase 11D — Overnight Generation lightweight intake (migration 0137)

**Current:** Position 0 = `seed_prompt` / `intake_agent`
**Target:** Positions 0–1 = `intake_scope` + `intake_gate` only (no conflict, no feasibility — factory pipeline, speed matters)

**Scope prompt focus:** Is the seed prompt sufficient to generate a story bible? Genre, protagonist, core premise?

```python
_OG_INTAKE_SCOPE_PROMPT = """
You are a story concept validator for an overnight batch writing pipeline.

The pipeline will run unattended. Assess the seed prompt:
1. GENRE — Clear?
2. PROTAGONIST — Named or clearly implied?
3. CONFLICT — Central dramatic question stated?
4. TONE — Dark, light, comedic, thriller?

If all four are present: output READY.
If one or two are missing but inferable: output READY with a note on what was inferred.
If the prompt is too vague to proceed: output NOT_READY with a list of what must be specified.
"""
```

Transition wiring: scope → scope (fail), scope → gate (pass), gate → scope (fail), gate → `story_bible` (pass).

---

## Phase 12 — Replace `planning_agent` with `generic_stage`

### Design principle

`planning_agent` in non-SW-Dev templates dispatches through the `planning_pipeline` behavior
type, which runs `run_planning_pipeline()` from `planning_utils.py`. This is a 1,200-line
survey-propose-review-pitfalls-consolidate-gate monolith written for code planning. It is
the wrong tool for story outlines, data analysis question refinement, and schema design.

The replacement is `generic_stage`: a single-turn LLM call driven by `stage.config.system_prompt`.
These stages do not need fan-out, peer review, or a planning gate — they are simple structured
output stages. Each gets a well-crafted system prompt and the appropriate tools.

---

### Phase 12A — Data Analysis planning stages (migration 0138)

Three `planning_agent` stages replaced:

**`planning` (pos 1) → `generic_stage`**

```python
_DA_PLANNING_PROMPT = """
You are a data analysis project planner. Based on the refined analysis question,
produce a structured analysis plan with:
- analysis_objective: one clear sentence
- data_sources: list of required datasets/tables/APIs
- methodology: list of analysis steps (load → clean → explore → model → visualise)
- output_artifacts: list of expected deliverables (CSV, chart, report section)
- success_criteria: how to know the analysis is complete and correct

Call submit_work with the plan as a JSON object.
"""
```

**`question_refinement` (pos 2) → `generic_stage`**

```python
_DA_QUESTION_PROMPT = """
You are a data science research assistant. The raw analysis question needs sharpening.

Produce:
- refined_question: a precise, measurable, answerable version of the question
- key_metrics: the specific quantities to compute
- assumptions: any assumptions made to make the question answerable
- out_of_scope: what this analysis explicitly does NOT cover

Call submit_work with the refined question definition as JSON.
"""
```

**`schema_design` (pos 4) → `generic_stage`**

```python
_DA_SCHEMA_PROMPT = """
You are a data schema designer. Based on the analysis plan and collected data,
design the working schema for this analysis:
- input_schema: describe each input dataset (columns, types, expected size)
- derived_tables: intermediate tables/dataframes to compute
- output_schema: final output columns and format

Call submit_work with the schema design as JSON.
"""
```

---

### Phase 12B — Novel Writing planning stages (migration 0139)

Two `planning_agent` stages replaced:

**`planning` (pos 1 after Phase 11C shift → pos 4) → `generic_stage`**

```python
_NW_PLANNING_PROMPT = """
You are a story structure architect. Produce a complete story development plan:
- title: working title
- logline: one-sentence premise
- genre_and_tone: genre tags + tonal direction
- protagonist: name, goal, flaw, arc
- antagonist_or_conflict: source of opposition
- three_act_structure: setup / confrontation / resolution beats
- chapter_count: estimated number of chapters
- pov: narrative point of view (first-person, third-limited, omniscient)
- themes: 2–3 thematic concerns

Call submit_work with the story plan as JSON.
"""
```

**`outline` (pos 2 after Phase 11C shift → pos 5) → `generic_stage`**

```python
_NW_OUTLINE_PROMPT = """
You are a chapter outline architect. Based on the story plan, produce a complete
chapter-by-chapter outline. For each chapter:
- chapter_number: integer
- title: chapter title or working label
- pov_character: whose viewpoint
- setting: location and time
- events: 3–5 key plot events that occur
- emotional_arc: character emotional state start → end
- ends_on: how the chapter closes (cliffhanger, revelation, quiet beat)

Call submit_work with chapters as a JSON array.
"""
```

---

### Phase 12C — Overnight Generation planning stages (migration 0140)

Two `planning_agent` stages replaced:

**`story_bible` (pos 1 after Phase 11D shift → pos 2) → `generic_stage`**

```python
_OG_BIBLE_PROMPT = """
You are building a story bible for an unattended batch writing pipeline.
The story bible must be self-contained — all agents will read it without seeing
the original seed prompt.

Produce:
- title, genre, tone
- world: setting, rules, atmosphere
- characters: name, role, voice, goal for each
- central_conflict: the driving tension
- chapter_count: how many chapters to generate
- style_guide: sentence length, POV, vocabulary register

Call submit_work with the story bible as JSON.
"""
```

**`chapter_outline` (pos 3 after Phase 11D shift → pos 4) → `generic_stage`**

```python
_OG_OUTLINE_PROMPT = """
You are generating a chapter outline based on the story bible.

For each chapter produce:
- chapter_number
- title
- events: 3 key events
- emotional_beat: the dominant emotion of the chapter
- ends_on: final line direction (leave reader curious, resolved, shocked)

Call submit_work with chapters as a JSON array.
"""
```

---

## Phase 13 — Writing domain improvements

### Phase 13A — Novel Writing `continuity_check` upgrade

**Current:** `continuity_check` / `custom_agent` — single-reviewer LLM pass.
**Target:** `multiplier_node` (vote_tally) — 3 specialist reviewers.

```json
{
  "n": 3,
  "collapser_mode": "vote_tally",
  "tally_strategy": "majority",
  "on_tie": "reject",
  "output_key": "continuity_check_result",
  "agents": [
    {
      "name": "timeline_checker",
      "system_prompt": "You are a continuity editor checking temporal logic. Verify that the chapter's events are consistent with the established timeline. Vote ACCEPTED if consistent, REJECTED if you find a contradiction. Call submit_work with your verdict."
    },
    {
      "name": "character_voice",
      "system_prompt": "You are a character voice editor. Verify that each character's dialogue and actions are consistent with their established personality and arc from the story plan. Call submit_work with ACCEPTED or REJECTED."
    },
    {
      "name": "world_consistency",
      "system_prompt": "You are a world-building consistency checker. Verify that settings, rules, and established facts are not contradicted. Call submit_work with ACCEPTED or REJECTED."
    }
  ]
}
```

---

## Phase 14 — Dead code deletion

### Precondition checklist

Before running any deletion:
```sql
-- Must return 0 rows before deleting intake_agent dispatch path
SELECT stage_key, pt.name FROM pipeline_stages ps
JOIN pipeline_templates pt ON pt.id = ps.template_id
WHERE ps.agent_type = 'intake_agent';

-- Must return 0 rows before deleting planning_agent dispatch path
SELECT stage_key, pt.name FROM pipeline_stages ps
JOIN pipeline_templates pt ON pt.id = ps.template_id
WHERE ps.agent_type = 'planning_agent';
```

### Phase 14A — Delete intake.py and intake dispatch path

After Phases 11A–11D are applied and validated:

1. Remove `register_handler("idea", _run_intake)` from `scheduler.py`
2. Delete `_run_intake` function from `scheduler.py` (~800 lines)
3. Remove `"intake_pipeline"` from `_BEHAVIOR_TYPE_TO_STAGE_HANDLER` in `pipeline_router.py`
4. Remove `"intake_agent"` from `agent_registry.py`
5. Delete `app/agent/intake.py` (1,213 lines)

### Phase 14B — Delete planning_agent dispatch path

After Phases 12A–12C are applied and validated:

1. Remove `register_handler("planning", _run_planning_task)` from `scheduler.py`
2. Delete `_run_planning_task` function from `scheduler.py` (~200 lines estimate)
3. Remove `"planning_pipeline"` from `_BEHAVIOR_TYPE_TO_STAGE_HANDLER` in `pipeline_router.py`
4. Remove `"planning_agent"` from `agent_registry.py`
5. Delete `run_planning_pipeline` from `planning_utils.py` (or leave file — still needed by SW Dev executors)

### Phase 14C — Clean up `custom_agent_definitions` stale rows

The `custom_agent_definitions` table has dead rows for retired agent types. After the above:

```sql
-- Delete definitions that are no longer used by any template stage
DELETE FROM custom_agent_definitions
WHERE name IN ('intake_agent', 'planning_agent', 'fan_out_judge', 'voting_panel', 'optimization_agent');
```

---

## Phases shipped

### Phase 1 — `voting_panel` + `parallel_agents(dangerous_edit)` (commit `c695f7b`)
- Added `voting_panel` and `fan_out_judge` executors
- Added `dangerous_edit_llm_agent` executor (MaestroLoop with worktree isolation)
- Added `parallel_agents` executor with `dynamic_agents_from_key` and `dangerous_edit` subagent type

### Phase 2 — `dynamic_agents_from_key`, `indev → parallel_agents` (commit `df12399`)
- SW Dev `indev` stage switched from `dangerous_edit_llm_agent` to `parallel_agents` (migration 0119)
- `dev_orchestrator.py` + `component_loop.py` deleted

### Phase 3 — `optimization_node`, `json_schema_gate`, `planning_correction_stage` (commit `1644ccb`)
- `optimization_node`, `json_schema_gate`, `planning_correction_stage` executors added
- `optimization.py` deleted (989 lines)

### Phase 4 — `planning_node`, test gate (commit `ad79630`)
- `planning_node` executor added (thin wrapper around `run_planning_pipeline`)
- `require_passing_tests` gate restored in `MaestroLoop`

### Phase 6 — Decompose `planning_node` into 6 sequential stages (commit `e48c3c0`)
- 4 new executors: `planning_survey_node`, `pitfall_node`, `consolidation_node`, `planning_gate_node`
- Migration 0124: SW Dev template gains survey → propose → review → pitfalls → consolidate → gate
- `conceptual_review.py`, `security_review.py`, `final_review.py` deleted (~1,793 lines)

### Phase 7 — Intake decomposition for SW Dev (commit `8c752a1`)
- 5 new executors: `intake_scope`, `intake_static`, `intake_conflict`, `intake_feasibility`, `intake_gate`
- Migration 0125: SW Dev template gains 5-stage intake chain

### Phase 8 — `multiplier_node` + crash-survivable fan-out (commits `20fa9de`, `8a5e199`)
- `multiplier_node` with `vote_tally` and `judge_select` collapser modes
- Migration 0126: SW Dev `conceptual_review` + `final_review` converted from `voting_panel`

### Phase 9 — Retire `fan_out_judge` (commit `623b575`)
- `planning_propose` converted from `fan_out_judge` to `multiplier_node` (judge_select, 5 personas)
- `_run_fan_out_judge` deleted (~200 lines)

### Phase 10 — Retire `voting_panel`; Research Report + Math redesign (migrations 0128–0133)
- Migration 0128: All remaining `voting_panel` stages → `multiplier_node`
- `_run_voting_panel` deleted; `voting_panel` entry removed from registry
- Migration 0129: `optimization_propose` → `multiplier_node` (judge_select)
- `_run_optimization_node` deleted
- Migration 0130: Generalize intake/planning prompts
- Migration 0131: Research Report full redesign (15 stages, fully malleable)
- Migration 0132: Add `config JSONB` to `pipeline_templates` (kanban column bands)
- Migration 0133: Math/Proof intake + planning decomposition (4 intake + 8 planning stages)
- `_run_planning_node`, `_run_intake_node` deleted

---

## LOC Delta Summary

| Phase | Added | Deleted | Net |
|---|---|---|---|
| Phase 1–4 | ~800 | ~2,400 | **-1,600** |
| Phase 6 | ~430 | ~2,396 | **-1,966** |
| Phase 7 | ~450 | ~850 | **-400** |
| Phase 8–9 | ~600 | ~500 | **+100** |
| Phase 10 | ~300 | ~2,000 | **-1,700** |
| **Fury shipped total** | **~2,580** | **~8,146** | **~-5,566** |
| Phase 11–14 (planned) | ~400 | ~2,800 | **~-2,400** |
| **Fury total (complete)** | **~2,980** | **~10,946** | **~-7,966** |

---

## Verification checklist (per phase)

### Phase 11 (intake decompose — each template)
1. Run status: `SELECT stage_key, agent_type FROM pipeline_stages WHERE template_id = X ORDER BY position`
2. Create a test task in the template; trigger advance from first stage
3. Confirm 4 `_fan_out_child`-style DB records are NOT created (these are sequential, not fan-out)
4. Confirm intake sub-stages fire in order: scope → conflict → feasibility → gate
5. Confirm fail-loop-back works (stage returns to `intake_scope` on any failure)
6. Run: `venv/Scripts/python.exe -m pytest app/tests/ -q` — zero new failures

### Phase 12 (planning_agent → generic_stage)
1. Confirm stage `agent_type = 'generic_stage'` in DB
2. Confirm `stage.config.system_prompt` is non-empty
3. Dispatch task through the stage manually; confirm `submit_work` is called
4. Confirm `task.content` receives the JSON output from the stage

### Phase 14 (deletion)
```bash
# Precondition: no remaining intake_agent or planning_agent stages
venv/Scripts/python.exe scripts/psql.py "SELECT count(*) FROM pipeline_stages WHERE agent_type IN ('intake_agent', 'planning_agent')"
# → (0,)

# After deletion: confirm no broken imports
venv/Scripts/python.exe -c "from app.agent import scheduler; print('ok')"
venv/Scripts/python.exe -m pytest app/tests/ -q
```
