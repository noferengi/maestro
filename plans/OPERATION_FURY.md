# OPERATION FURY — Living Status Document

**Goal:** Replace every bespoke Python pipeline agent with visual node types that run
through the malleable pipeline infrastructure (`pipeline_templates`, `pipeline_stages`,
`stage_executors.py`). Strip the hardcoded SW Dev pipeline layer by layer until `scheduler.py`
is a pure dispatcher and every agent behavior is DB-configurable.

---

## Current state (as of Phase 4 — commit `ad79630`)

### Node executor registry

Registered via `_reg_executor()` in `scheduler.py:5655–5669`:

| Node type | Executor | File | Status |
|---|---|---|---|
| `voting_panel` | `_run_voting_panel` | stage_executors.py | ✅ shipped |
| `fan_out_judge` | `_run_fan_out_judge` | stage_executors.py | ✅ shipped |
| `dangerous_edit_llm_agent` | `_run_dangerous_edit_llm_agent` | stage_executors.py | ✅ shipped |
| `parallel_agents` | `_run_parallel_agents` | stage_executors.py | ✅ shipped (dynamic subagents) |
| `parallel_subagent` | `_run_parallel_subagent` | stage_executors.py | ✅ shipped |
| `parallel_subagent_aggregator` | `_run_parallel_subagent_aggregator` | stage_executors.py | ✅ shipped |
| `optimization_node` | `_run_optimization_node` | stage_executors.py | ✅ shipped (Phase 3) |
| `json_schema_gate` | `_run_json_schema_gate` | stage_executors.py | ✅ shipped (Phase 3) |
| `planning_correction_stage` | `_run_planning_correction_stage` | stage_executors.py | ✅ shipped (Phase 3) |
| `planning_node` | `_run_planning_node` | stage_executors.py | ✅ shipped (Phase 4) |
| `reflection_agent` | `_run_reflection_agent` | stage_executors.py | ✅ shipped |
| `static_analysis_widget` | `_run_static_analysis_widget` | stage_executors.py | ✅ shipped |
| `circuit_breaker` | `_run_circuit_breaker` | stage_executors.py | ✅ shipped |

### SW Dev template stage map (current)

| Stage key | Agent type | Notes |
|---|---|---|
| idea | `idea` (legacy handler) | ← **next target** |
| planning | `planning_node` | ✅ Phase 4 |
| json_schema_gate | `json_schema_gate` | ✅ Phase 3 |
| planning_correction | `planning_correction_stage` | ✅ Phase 3 |
| indev | `parallel_agents` | ✅ Phase 2 — dynamic per impl step |
| conceptual_review | `voting_panel` | ✅ Phase 1 |
| optimization_propose | `optimization_node` | ✅ Phase 3 |
| optimization_implement | `dangerous_edit_llm_agent` | ✅ Phase 3 |
| security | `voting_panel` | ✅ Phase 1 |
| final_review | `voting_panel` | ✅ Phase 1 |
| human_review | `human_gate` | manual — unchanged |
| completed | `terminal` | unchanged |

### Legacy stage handlers still registered (`_register_stage_handler`)

These are the remaining Python-heavy fallback handlers. They execute when there is no
matching `agent_type` executor in the DB stage config.

| Stage key | Handler function | Scheduler lines | External Python file |
|---|---|---|---|
| `idea` | `_run_intake` | ~809 lines (4292–5103) | `intake.py` (1,067 lines) |
| `conceptual_review` | `_run_conceptual_review_task` | ~129 lines (5104–5232) | `conceptual_review.py` (696 lines) |
| `security` | `_run_security_task` | ~101 lines (5233–5333) | `security_review.py` (595 lines) |
| `final_review` | `_run_final_review_task` | ~117 lines (5334–5450) | `final_review.py` (502 lines) |

> **Note:** `conceptual_review`, `security`, and `final_review` are already wired to
> `voting_panel` in the SW Dev template (migration 0117). Their legacy handlers are never
> reached for SW Dev tasks. They are dead code for that template only — other templates
> without a stage config still fall through to them.

### Python files pending deletion

| File | Lines | Blocker |
|---|---|---|
| `app/agent/planning.py` | 2,011 | Needs production validation of `planning_node` |
| `app/agent/planning_gate.py` | 1,001 | Imported by `planning_correction_stage`; needs refactor |
| `app/agent/planning_correction.py` | 456 | Imported by `planning_correction_stage` |
| `app/agent/conceptual_review.py` | 696 | `_run_conceptual_review_task` still registered (other templates) |
| `app/agent/security_review.py` | 595 | `_run_security_task` still registered (other templates) |
| `app/agent/final_review.py` | 502 | `_run_final_review_task` still registered (other templates) |
| `app/agent/intake.py` | 1,067 | `_run_intake` still registered; intake pipeline not yet a node |

---

## Phases shipped

### Phase 1 — `voting_panel` + `parallel_agents(dangerous_edit)` (commit `c695f7b`)
- Added `voting_panel` and `fan_out_judge` executors
- Added `dangerous_edit_llm_agent` executor (MaestroLoop with worktree isolation)
- Added `parallel_agents` executor with `dynamic_agents_from_key` and `dangerous_edit` subagent type
- Fixed dispatch hierarchy: `pipeline_router.dispatch_task()` checks stage config first; legacy handlers are fallback-only

### Phase 2 — `dynamic_agents_from_key`, `indev → parallel_agents` (commit `df12399`)
- SW Dev `indev` stage switched from `dangerous_edit_llm_agent` to `parallel_agents` (migration 0119)
- Reads `planning_result.implementation_steps`; spawns one `_psubagent_dangerous` per component
- `dev_orchestrator.py` + `component_loop.py` deleted

### Phase 3 — `optimization_node`, `json_schema_gate`, `planning_correction_stage` (commit `1644ccb`)
- `optimization_node` executor: 5 parallel proposers → 3-judge two-round vote → winning proposal
- Split `optimization` stage into `optimization_propose` + `optimization_implement` (migration 0120)
- `json_schema_gate` executor: validates planning result fields, routes to `planning_correction` on failure
- `planning_correction_stage` executor: thin wrapper around `PlanningCorrectionAgent`
- Inserted `json_schema_gate` + `planning_correction` between `planning` and `indev` (migration 0121)
- Deleted: `optimization.py` (989 lines), `dev_orchestrator.py`, `component_loop.py`

### Phase 4 — `planning_node`, test gate, 7 test fixes (commit `ad79630`)
- `planning_node` executor: thin wrapper around `run_planning_pipeline`; replaces `_run_planning_task`
- Removed `"planning"` from `ADVANCE_HANDLERS`; `/run-planning` endpoint now just clears the stopped flag
- Restored `ComponentLoop` behaviors in `MaestroLoop`: test gate (`require_passing_tests`) and file-write containment warning
- `indev` stage config gains `"require_passing_tests": true`
- Fixed 7 test failures left by Phase 3 deletions

---

# Operation Fury Phase 5

## Context

Phase 4 shipped `planning_node` and the test gate, bringing the SW Dev template to full
node coverage. This phase does three things in order:

1. **Answer a design question** — how general-purpose are the existing nodes, and can they be
   applied to non-SW-Dev templates? (The answer reshapes the deletion plan below.)
2. **`intake_node` executor** — the last SW Dev stage still running through a legacy handler.
3. **Dead code deletion** — `planning.py`, the three SW-Dev review Python files, and their
   ~1,156 lines of scheduler handler code.

---

## Section A — Node Generality Analysis (Research Summary)

### The architecture answer

The node executor boundary (`register_agent_type_executor()` in `pipeline_router.py`) is the
abstraction layer. Custom Python doesn't need to be eliminated — it needs to live *inside* an
executor function. The pipeline sees only `(agent_type, config_json)`; what happens inside is
opaque. This means the "cutoff" is exactly the executor function signature, and domain-specific
Python is acceptable and expected below it.

### Three tiers of generality

**Tier 1 — Fully general-purpose (work in any template with only config changes):**
- `circuit_breaker` — pure counter logic, no domain assumptions
- `voting_panel` — configurable personas, prompts, tally strategy, output key
- `fan_out_judge` — configurable proposals + judge panel
- `reflection_agent` — configurable system prompt, max turns

**Tier 2 — Parameterizable (need 1–2 config additions, then fully general):**
- `json_schema_gate` — currently reads only from the `planning_results` table (SW-Dev specific).
  Adding a `source: "task_content"` option that reads from `task.content[field_key]` makes it
  general. The existing `source: "planning_result"` path is unchanged.
- `parallel_agents` — currently hardcoded to read `planning_result.implementation_steps` when
  `dynamic_agents_from_key` is set. Adding a `items_from_content_key` option that reads from
  `task.content[key]` generalizes it. Existing behavior unchanged.

**Tier 3 — Intentionally domain-specific (wrappers; custom Python lives inside):**
- `planning_node` — wraps `PlanningPipeline` (SW Dev specific)
- `intake_node` (proposed) — wraps `IntakePipeline` (generic, as below)
- `optimization_node` — cost/risk semantics are SW Dev specific
- `dangerous_edit_llm_agent` — wraps `MaestroLoop` with ACCEPTED/REJECTED verdicts; SW Dev
- `planning_correction_stage` — wraps `PlanningCorrectionAgent`; SW Dev
- `static_analysis_widget` — Python tree-sitter; language-specific

### Which non-SW-Dev templates can use existing nodes immediately?

**`voting_panel` — zero code changes needed, only migrations:**
| Template | Stage | Config |
|---|---|---|
| Math/Proof | `FORMAL_VERIFICATION` | 3 reviewers: symbolic verifier, logical checker, intuition challenger |
| Bug Triage | `root_cause` | 3 reviewers: runtime analyst, logic tracer, regression hunter |
| Research Report | `fact_check` | 3 reviewers: source validator, claim strength judge, bias detector |

**`json_schema_gate` — needs Tier 2 generalization first:**
After adding `source: "task_content"` — applicable to Novel Writing `continuity_check`
(validate character/timeline schema), Data Analysis `schema_design` (validate dataset schema),
Math `PROBLEM_FORMALIZATION` (validate problem structure).

**`parallel_agents` — needs Tier 2 generalization first:**
After adding `items_from_content_key` — applicable to Research Report `research` stage
(parallel researcher threads), Data Analysis parallel collection group.

### Why NOT to inline planning_gate.py + planning_correction.py

The Phase 5 plan in `OPERATION_FURY.md` listed this as Move 1. Research shows it's the wrong
call:

- `planning_gate.py` has ~900 lines of *unique* planning-specific validation logic (namespace
  conflicts, interface completeness with fuzzy matching, LLM feasibility re-check, context
  budget). It has zero overlap with `_run_json_schema_gate`.
- `PlanningCorrectionAgent` is a full multi-turn LLM loop with message history, turn counting,
  and saturation checks. Inlining it would produce 300–400 lines of procedural code in
  `stage_executors.py` and destroy the reusable class pattern.
- The correct call: keep both files as long-term domain logic. `_run_planning_correction_stage`
  imports them correctly now. Deletion deferred to Phase 6 when/if the correction flow is
  redesigned with a new architecture.

---

## Move 1 — Generalize two Tier 2 nodes (~50 lines in stage_executors.py)

**File:** `app/agent/stage_executors.py`

### A. `json_schema_gate` — add `source: "task_content"` option

In `_run_json_schema_gate`, the field-loading block currently always reads from
`planning_results` table. Change:

```python
source = stage_config.get("source", "planning_result")
if source == "task_content":
    task = get_task(task_id)
    context_data = task.content or {}
else:  # "planning_result" (default, existing behavior)
    pr = get_planning_result(task_id)
    context_data = {
        "file_manifest": json.loads(pr.file_manifest or "[]"),
        "implementation_steps": json.loads(pr.implementation_steps or "[]"),
        ...
    }
```

The `required_fields[*].key` then resolves against `context_data`. No schema change — this is
config-only. The three validators (`non_empty_list`, `valid_dag`, `valid_json`) are unchanged.

### B. `parallel_agents` — add `items_from_content_key` option

In `_run_parallel_agents`, the `dynamic_agents_from_key` branch currently hardcodes reading from
`planning_result` columns. Add:

```python
content_source = cfg.get("items_from_content_key")  # e.g. "research_threads"
if content_source:
    task = get_task(task_id)
    items = (task.content or {}).get(content_source, [])
else:
    # existing: reads planning_result.implementation_steps etc.
    ...
```

Each item in `items` becomes one agent, with the same template-expansion logic already in place.

---

## Move 2 — `intake_node` executor (~150 lines in stage_executors.py + migration 0122)

### What IntakePipeline does (confirmed generic)

`IntakePipeline` has **no SW-Dev-specific code**. It:
1. Runs 4 parallel LLM voters (scope, static analysis, conflict detection, feasibility)
2. Aggregates votes; spawns research or tie-breaker agents as needed
3. Returns `outcome` + vote tally

The actual subdivision routing (when vote is `SUBDIVIDE_IDEA`) happens in the caller, not the
pipeline class. The executor handles this via `advance_stage`.

### New executor: `_run_intake_node` in stage_executors.py

```python
def _run_intake_node(task_id, stage_config, llm_base_url, llm_model,
                     max_context=None, llm_id=None, budget_id=None,
                     project_path=None, **kwargs):
    from app.agent.intake import run_intake_pipeline
    from app.agent.tools import set_task_git_cwd
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_intake_pipeline(task_id=task_id, llm_base_url=llm_base_url,
                                llm_model=llm_model, max_context=max_context,
                                llm_id=llm_id, budget_id=budget_id,
                                project_path=project_path)
        )
        outcome = result.get("outcome", "fail")
        if outcome in ("passed", "needs_research"):
            advance_stage(task_id, "pass")
        elif outcome == "subdivide":
            advance_stage(task_id, "subdivide")
        else:
            advance_stage(task_id, "fail")
    except ShutdownError:
        ...
    finally:
        loop.close()
```

Register: `_reg_executor("intake_node", _run_intake_node)` in scheduler.py.

Note: session tracking (create_agent_session / close_agent_session) follows the same pattern
as `_run_planning_node`. The `_run_intake` handler in scheduler.py already shows the exact
session scaffolding to replicate.

### Migration 0122 — `intake_node_sw_dev`

```python
def up(conn):
    # SW Dev template: idea stage → intake_node
    conn.execute("""
        UPDATE pipeline_stages SET agent_type = 'intake_node'
        WHERE stage_key = 'idea'
        AND pipeline_template_id = (
            SELECT id FROM pipeline_templates WHERE name = 'Software Development'
        )
    """)
```

No config seeding needed — IntakePipeline reads its behavior from `maestro.ini [intake]` and
the task itself. The stage config JSON can remain empty.

### Deletion after validation

- `scheduler.py` — delete `_run_intake` (lines 4292–5103, ~809 lines) and
  `_register_stage_handler("idea", ...)` entry (~5631)
- `app/agent/intake.py` — delete (1,067 lines) once `_run_intake_node` has been validated on
  several real tasks; no other file imports `IntakePipeline` directly in the hot path
- `app/main.py` — delete `_run_intake_pipeline_bg` and remove `"idea"` from `ADVANCE_HANDLERS`
  if it's still there

---

## Move 3 — Apply voting_panel to three non-SW-Dev templates (migration 0123)

**Zero code changes.** This is a pure DB migration to prove voting_panel is general-purpose and
give three templates their first malleable node.

### Migration 0123 — `voting_panel_non_sw_dev`

Three stage updates within existing templates:

**Math/Proof — FORMAL_VERIFICATION:**
```json
{
  "reviewers": [
    {"name": "symbolic", "system_prompt": "You are a symbolic verifier. Check that each proof step follows from axioms by valid inference rules. Call submit_work with ACCEPTED or REJECTED and your reasoning.", "max_turns": 12},
    {"name": "logical",  "system_prompt": "You are a logical completeness checker. Verify no proof steps are skipped or unjustified. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 12},
    {"name": "intuition","system_prompt": "You are a mathematical intuition challenger. Look for hidden assumptions or cases the proof misses. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 12}
  ],
  "tally_strategy": "majority",
  "output_key": "formal_verification_result"
}
```

**Bug Triage — root_cause:**
```json
{
  "reviewers": [
    {"name": "runtime",    "system_prompt": "Analyze the bug report as a runtime execution fault. Identify the probable execution path and state at failure. Call submit_work with ACCEPTED (root cause identified) or REJECTED.", "max_turns": 10},
    {"name": "logic",      "system_prompt": "Analyze the bug as a logic/algorithmic error. Look for off-by-one, race conditions, or incorrect state transitions. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 10},
    {"name": "regression", "system_prompt": "Analyze whether this bug is a regression. Check what recent changes could have introduced it and what the fix surface area is. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 10}
  ],
  "tally_strategy": "majority",
  "output_key": "root_cause_analysis"
}
```

**Research Report — fact_check:**
```json
{
  "reviewers": [
    {"name": "source_validator", "system_prompt": "Verify all cited sources exist and support the claims made. Flag missing citations. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 10},
    {"name": "claim_strength",   "system_prompt": "Evaluate whether the strength of claims is justified by evidence. Flag overstated conclusions. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 10},
    {"name": "bias_detector",    "system_prompt": "Look for confirmation bias, missing contrary evidence, or framing effects. Call submit_work with ACCEPTED or REJECTED.", "max_turns": 10}
  ],
  "tally_strategy": "majority",
  "output_key": "fact_check_result"
}
```

For each, add transitions: `pass → next_stage`, `fail → previous_stage`.

---

## Move 4 — Delete planning.py and three dead review files

### planning.py (2,011 lines)

**Precondition:** At least 3 real SW Dev tasks have completed the `planning_node` path without
regression (check `agent_sessions` for `agent_type='planning'` sessions completing with
`exit_reason='completed'`).

Delete `app/agent/planning.py` once validation passes. The `_run_planning_node` executor
imports `run_planning_pipeline` from it — this import becomes the deletion trigger. Remove the
import and inline any remaining wiring.

### conceptual_review.py, security_review.py, final_review.py (~1,793 lines combined)

**Precondition:** Confirm these stage keys (`conceptual_review`, `security`, `final_review`) are
not used as agent types by any non-SW-Dev template stage (the template survey shows they are
not — other templates use their own unique stage keys like `FORMAL_VERIFICATION`, `root_cause`,
`fact_check`).

**Deletion sequence:**
1. Remove `_register_stage_handler("conceptual_review", ...)` from scheduler.py
2. Remove `_run_conceptual_review_task` function (~129 lines, 5104–5232)
3. Remove `_register_stage_handler("security", ...)` and `_run_security_task` (~101 lines)
4. Remove `_register_stage_handler("final_review", ...)` and `_run_final_review_task` (~117 lines)
5. Delete `app/agent/conceptual_review.py` (696 lines)
6. Delete `app/agent/security_review.py` (595 lines)
7. Delete `app/agent/final_review.py` (502 lines)

Verify no other file imports from these three modules before deleting.

Also remove `"security"` and `"final_review"` from `ADVANCE_HANDLERS` in `main.py` —
`_run_security_pipeline_bg` and `_run_final_review_pipeline_bg` become dead code when the
handlers are gone (SW Dev routes through voting_panel; no other template uses these keys).

---

## Verification

**Move 1 (node generalization):**
```bash
venv/Scripts/python.exe -m pytest app/tests/ -q
# All existing gate tests must pass with new source param defaulting correctly
```
Manual: Use `patch_planning_fields` MCP tool to set a bad field in a non-planning-result
task, configure a `json_schema_gate` stage with `source: "task_content"`, trigger it, verify
it fails and routes to `planning_correction`. Check `task.content["_gate_failures"]` is set.

**Move 2 (intake_node):**
1. Create a new SW Dev task, trigger advance from `idea`
2. Check `agent_sessions` for a session with `agent_type='intake'` spawned by the executor
3. Verify task advances to `planning` on a passed intake
4. Verify task is soft-deleted on a rejected intake (`task.is_active = False`)
5. Run tests: `venv/Scripts/python.exe -m pytest app/tests/test_pipeline_routing.py -v`

**Move 3 (voting_panel cross-template):**
1. Create a Math task, manually advance to `FORMAL_VERIFICATION`
2. Confirm three child `_psubagent` sessions spawn with the reviewer personas
3. Check `task.content["formal_verification_result"]` is written after all complete
4. Repeat for Bug Triage `root_cause` and Research Report `fact_check`

**Move 4 (deletions):**
```bash
# Before deleting: grep for any remaining imports
grep -r "from app.agent.conceptual_review\|from app.agent.security_review\|from app.agent.final_review\|from app.agent.planning import" app/ --include="*.py"
# Must be empty before deleting

venv/Scripts/python.exe -m pytest app/tests/ -q
# Target: 0 new failures
```

---

## LOC Delta Summary

| Move | Added | Deleted | Net |
|---|---|---|---|
| 1 — Generalize json_schema_gate + parallel_agents | ~50 | 0 | **+50** |
| 2 — intake_node | ~150 | ~1,876 | **-1,726** |
| 3 — voting_panel cross-template | ~0 | 0 | **0** (migrations only) |
| 4 — planning.py + 3 review files | ~0 | ~3,804 | **-3,804** |
| **Phase 5 total** | **~200** | **~5,680** | **~-5,480** |
| **Fury cumulative** | **~1,145** | **~10,518** | **~-9,373** |

---

# Operation Fury Phase 6

## Context

`planning_node` is a thin wrapper around `PlanningPipeline` in `planning.py` (2,011 lines).
Phase 6 decomposes this monolith into 6 sequential DB stages so `planning.py` can be deleted.
Each stage becomes either a reuse of an existing executor or a new wrapper (~100 lines each).

The current `planning → json_schema_gate → planning_correction → indev` sequence expands to:

```
planning_survey → planning_propose → planning_review → planning_pitfalls
    → planning_consolidate → planning_gate → json_schema_gate → planning_correction → indev
```

The retry loop (design → judge → review → reject → retry) becomes a DB back-transition:
`planning_review` fail → `planning_propose`. No retry logic needed in any executor.

---

## PlanningPipeline stage map (what is being decomposed)

`planning.py:PlanningPipeline` has 6 distinct stages inside `run()` (lines 535–787):

| Internal method | Lines | What it does | LLM calls |
|---|---|---|---|
| inline in `run()` | 542–565 | Task classification (`_is_proof`, `_is_simple`, `_effective_best_of_n`) | 0 |
| `_stage_codebase_survey()` | 793–1012 | Multi-turn agentic codebase exploration (read-only tools) | N sequential turns |
| `_stage_design_generation()` | 1018–1261 | N sequential persona LLM calls; each outputs full JSON design | N |
| `_stage_judge_designs()` | 1263–1392 | 1 LLM call selects best design by index | 1 |
| `_stage_design_review()` | 1398–1624 | 2–5 sequential reviewer LLMs; ACCEPTED/REJECTED/NEEDS_RESEARCH | 2–5 |
| `_stage_pitfall_detection()` | 1630–1732 | Deterministic graph checks + 1 LLM call | 1 |
| `_stage_consolidation()` | 1738–1803 | 1 LLM call merging winning_design + pitfalls | 1 |

---

## Stage decomposition

### Stage 1 — `planning_survey` (`planning_survey_node`, new executor ~150 lines)

**File:** `app/agent/stage_executors.py`
**Wraps:** `_stage_codebase_survey()` + inline classification block (`run()` lines 542–565)

- Runs the multi-turn agentic survey loop (read-only tools, up to `PLANNING_SURVEY_MAX_TURNS`)
- Computes task classification: `_is_proof`, `_is_simple`, `_effective_best_of_n` from task title/description/demotion history
- Writes to `task.content`: `survey_summary` (str), `is_proof` (bool), `is_simple` (bool), `best_of_n` (int)
- `advance_stage("pass")` on completion; `advance_stage("fail")` on ShutdownError

### Stage 2 — `planning_propose` (reuse `fan_out_judge`, no new code)

**Existing executor:** `_run_fan_out_judge` at `stage_executors.py:475`

Stage config:
```json
{
  "required_input_keys": ["survey_summary"],
  "personas": [
    {"name": "pragmatic",  "system_prompt": "..."},
    {"name": "defensive",  "system_prompt": "..."},
    {"name": "innovative", "system_prompt": "..."},
    {"name": "minimal",    "system_prompt": "..."},
    {"name": "resilient",  "system_prompt": "..."}
  ],
  "judge_system_prompt": "Select the design best suited for production...",
  "output_key": "winning_design"
}
```

- `survey_summary` injected into each persona's preamble via `_build_required_keys_preamble()` (stage_executors.py:241)
- `best_of_n` from `task.content` overrides config `n` at runtime (add this 1-line read to `_run_fan_out_judge`)
- Judge selects winning design; written to `task.content["winning_design"]`

### Stage 3 — `planning_review` (reuse `voting_panel`, no new code)

**Existing executor:** `_run_voting_panel` at `stage_executors.py:262`

Stage config:
```json
{
  "required_input_keys": ["survey_summary", "winning_design"],
  "reviewers": [
    {"name": "coupling",    "system_prompt": "...", "max_turns": 12},
    {"name": "interface",   "system_prompt": "...", "max_turns": 12},
    {"name": "testability", "system_prompt": "...", "max_turns": 12},
    {"name": "security",    "system_prompt": "...", "max_turns": 12},
    {"name": "performance", "system_prompt": "...", "max_turns": 12}
  ],
  "tally_strategy": "majority",
  "output_key": "design_review_result"
}
```

- `fail` DB transition → `planning_propose` (this replaces the Python retry loop)
- `NEEDS_RESEARCH` verdict is treated as `fail` in Phase 6; full research intercept deferred to
  Phase 7 when `research_node` is built

### Stage 4 — `planning_pitfalls` (`pitfall_node`, new executor ~100 lines)

**File:** `app/agent/stage_executors.py`
**Wraps:** `_stage_pitfall_detection()` lines 1630–1732

- Deterministic checks: circular dependencies via `static_analysis._detect_cycles()`, path safety via `tools._assert_safe_path()`
- 1 LLM call for edge-case/race-condition detection
- Reads `winning_design` from `task.content`
- Writes `pitfalls` list to `task.content`
- Always `advance_stage("pass")` — pitfall detection is informational, not a gate

### Stage 5 — `planning_consolidate` (`consolidation_node`, new executor ~80 lines)

**File:** `app/agent/stage_executors.py`
**Wraps:** `_stage_consolidation()` lines 1738–1803 + `_build_result()` + `_store_result()` lines 1830–1935

- Single LLM call merging `winning_design` + `pitfalls` from `task.content`
- Writes `consolidated_design` and persists full `PlanningResult` to `planning_results` table
- `advance_stage("pass")` on success

> **Note on `_build_result()` / `_store_result()`:** These two helpers in `planning.py`
> (lines 1830–1935) are the only pieces that must be kept accessible to `consolidation_node`.
> Options: (a) move them to a new `app/agent/planning_utils.py` before deleting `planning.py`;
> (b) inline them directly into the new executor. Decision deferred to implementation.

### Stage 6 — `planning_gate` (`planning_gate_node`, new executor ~100 lines)

**File:** `app/agent/stage_executors.py`
**Wraps:** `run_planning_gate()` from `app/agent/planning_gate.py`

- Thin wrapper identical in structure to `_run_planning_node`'s existing gate call path
- Runs the 10-check gate (namespace conflicts, interface completeness, cycles, test strategy, feasibility recheck, context budget)
- `pass` → `json_schema_gate` (existing, unchanged)
- `fail` → `planning_correction` (existing, unchanged)

---

## Migration 0124 — `planning_decompose_sw_dev`

- Remove the single `planning` stage from the SW Dev template
- Insert 6 new stages with positions slotting between `idea`/`planning_survey` and `json_schema_gate`
- Seed stage configs for `planning_propose` (5 personas + judge prompt) and `planning_review` (5 reviewer configs)
- Wire transitions: survey→propose, propose→review, review pass→pitfalls, **review fail→propose**, pitfalls→consolidate, consolidate→planning_gate, gate pass→json_schema_gate, gate fail→planning_correction
- Existing `json_schema_gate`, `planning_correction`, and `indev` stages are **unchanged**

---

## Deletion precondition

Once at least 3 SW Dev tasks complete the full 6-node planning path without regression
(check `agent_sessions` for the new `agent_type` values completing with `exit_reason='completed'`):

1. Delete `_run_planning_node` from `stage_executors.py` and its `_reg_executor` call
2. Delete `app/agent/planning.py` (2,011 lines)
3. Remove any remaining `"planning"` references in `scheduler.py`

`planning_gate.py` and `planning_correction.py` are **not deleted** — they remain active
domain logic used by `planning_gate_node` and `planning_correction_stage` respectively.

---

## LOC Delta (Phase 6)

| Move | Added | Deleted | Net |
|---|---|---|---|
| 4 new executors (survey, pitfalls, consolidate, gate_node) | ~430 | 0 | **+430** |
| Migration 0124 (stage config + transitions) | ~60 | 0 | **+60** |
| Delete `_run_planning_node` executor | 0 | ~385 | **-385** |
| Delete `planning.py` | 0 | ~2,011 | **-2,011** |
| **Phase 6 total** | **~490** | **~2,396** | **~-1,906** |
| **Fury cumulative** | **~1,635** | **~12,914** | **~-11,279** |
