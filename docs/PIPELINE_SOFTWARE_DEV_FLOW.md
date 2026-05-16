# Software Development Pipeline — Complete Flow Reference

> **Purpose:** Exhaustive description of every stage, sub-process, gate, feedback loop,
> circuit-breaker, and demotion path in the hardcoded Software Development pipeline.
> Written as a reference for extracting this logic into generic, composable pipeline
> nodes that require no Python.
>
> **Source of truth:** `app/agent/scheduler.py`, `app/agent/intake.py`,
> `app/agent/planning.py`, `app/agent/dev_orchestrator.py`,
> `app/agent/component_loop.py`, `app/agent/conceptual_review.py`,
> `app/agent/optimization.py`, `app/agent/security_review.py`,
> `app/agent/final_review.py`, `app/agent/pip_agent.py`,
> `app/agent/pipeline_router.py`.

---

## Table of Contents

1. [Pipeline Overview](#1-pipeline-overview)
2. [Pre-Dispatch Infrastructure](#2-pre-dispatch-infrastructure)
3. [Stage 0 — IDEA (Intake Voting)](#3-stage-0--idea-intake-voting)
4. [Stage 1 — PLANNING](#4-stage-1--planning)
5. [Stage 2 — INDEV (Dev Orchestrator)](#5-stage-2--indev-dev-orchestrator)
6. [Stage 3 — CONCEPTUAL REVIEW](#6-stage-3--conceptual-review)
7. [Stage 4 — OPTIMIZATION](#7-stage-4--optimization)
8. [Stage 5 — SECURITY](#8-stage-5--security)
9. [Stage 6 — FINAL REVIEW](#9-stage-6--final-review)
10. [Stage 7 — HUMAN REVIEW](#10-stage-7--human-review)
11. [Cross-Cutting Subsystems](#11-cross-cutting-subsystems)
12. [Demotion & Feedback Paths](#12-demotion--feedback-paths)
13. [Generic Node Primitives Needed](#13-generic-node-primitives-needed)

---

## 1. Pipeline Overview

```
IDEA ──pass──► PLANNING ──pass──► INDEV ──pass──► CONCEPTUAL_REVIEW
  ▲               │    ◄──fail──────┘       ◄──fail────────────┘
  │               │                                    │ pass
  │               ▼ (exhausted)                        ▼
  │           [STOPPED]                         OPTIMIZATION
  │                                                    │ pass
  │                                             SECURITY
  │                                                    │ pass
  │                                             FINAL_REVIEW
  │                                                    │ pass
  └────────────────────────────────────────────HUMAN_REVIEW
                                                       │ (manual merge)
                                                  COMPLETED
```

**Failure paths are not uniform.** Each review stage has its own opinion about where
to demote: some always go back to INDEV, others let the reviewer pick the demotion
target at runtime (`indev` or `optimization`).

---

## 2. Pre-Dispatch Infrastructure

Before any stage runner fires, the scheduler performs several checks:

### 2a. Dispatchable Types Gate

The scheduler tick only considers tasks whose `stage_key` (or `type`) is in the
`SCHEDULER_DISPATCHABLE_TYPES` list (config: `[scheduler] dispatchable_types`):

```
idea, planning, indev, conceptual_review, optimization, security, final_review
```

Any other stage key is invisible to the scheduler. A task parked in a stage not on
this list will never be auto-dispatched — it requires a manual trigger.

### 2b. DAG / Prerequisite Gate

The `DAGResolver` checks that all prerequisite task IDs are in a "done" state before
marking a task ready. Big-Idea parents satisfy prerequisites once all their active
children are recursively done (without the parent itself reaching `completed`).
Tasks with living children are skipped in dispatch.

### 2c. Per-LLM and Per-Node Capacity Gate

Each LLM endpoint has a `parallel_sessions` limit. Each compute node has a
`max_parallel_sessions` limit. The scheduler checks both before claiming a slot.
Maestro's own orchestrator thread consumes one slot.

### 2d. Worktree Isolation

Before the stage runner is called, a `git worktree add` creates an isolated checkout
at `{project_path}/.maestro-worktrees/{task_id}/` on branch `maestro/task-{task_id}`.
The runner receives `project_path = worktree_path`, not the shared working tree.
Teardown (`git worktree remove --force`) runs in the `finally` block regardless of
outcome.

### 2e. Handler Dispatch

`dispatch_task()` resolves `task.stage_key` → `_stage_handlers[stage_key]` → calls
the registered Python function. If no handler is registered, falls back to
`_run_maestro_loop` (generic MaestroLoop agent).

---

## 3. Stage 0 — IDEA (Intake Voting)

**Entry guard:** task must have `description`, `llm_id`, and `budget_id` set.
If any are missing the scheduler skips silently without creating a session.

### 3a. Four-Stage Vote Panel

The `IntakePipeline` class runs four analysis stages in a specific order:

```
Stage 1 (serial):   Scope Analysis
                          │
                    ┌─────┴──── Early exit if REJECTED ────► rejected
                    ▼
Stage 2a (parallel):  Static Analysis   ──┐
Stage 2b (parallel):  Conflict Detection ──┤ (both run concurrently via asyncio.gather)
                    ▼                    ◄─┘
Stage 3 (serial):   Feasibility Analysis
                    (informed by Stage 1 + 2a outputs)
```

**Each stage returns a vote dict:**
```json
{
  "stage": "scope_analysis",
  "verdict": "ACCEPTED | NEEDS_RESEARCH | REJECTED | SUBDIVIDE",
  "confidence": 0-100,
  "justification": "...",
  "prompt_tokens": N,
  "completion_tokens": N
}
```

**Verdict meanings:**
- `ACCEPTED` — stage approves the idea
- `NEEDS_RESEARCH` — not enough information to decide; spawn a ResearchAgent
- `REJECTED` — idea is not suitable
- `SUBDIVIDE` — idea is too large; should be decomposed

**Scope Analysis:** LLM reviews task title + description against the full list of
existing tasks to check clarity, scope bounds, and whether this duplicates something
already in flight.

**Static Analysis (2a):** Deterministic tree-sitter parse of the project's Python
source; produces a structural code summary injected into the feasibility prompt.

**Conflict Detection (2b):** LLM checks whether the proposed change conflicts with
any in-flight or completed tasks; uses the full task list snapshot.

**Feasibility Analysis (3):** LLM given scope vote + static analysis output; decides
whether the project can actually execute this as described.

### 3b. Vote Tally

All four votes are aggregated by `tally_votes()`:

| Outcome | Condition |
|---|---|
| `passed` | Majority ACCEPTED with adequate confidence |
| `needs_research` | Any stage voted NEEDS_RESEARCH |
| `subdivide` | Any stage voted SUBDIVIDE |
| `tie` | Split vote with no majority |
| `rejected` | Majority REJECTED |

### 3c. Needs-Research Branch

If `needs_research`: for each stage that voted NEEDS_RESEARCH, a `ResearchAgent` is
spawned. Its findings replace the original vote. Votes are then re-tallied. If the
research agent hits a context overflow (`TOO_LARGE`), the outcome is forced to
`subdivide`.

A tie-breaker ResearchAgent is spawned on a tied vote, adding a fifth opinion.

### 3d. Subdivide Branch

If `subdivide`: a `SubdivisionAgent` decomposes the idea into child tasks. The parent
stays in IDEA stage with children spawned as new tasks. Children proceed through the
pipeline independently. The parent's prerequisite satisfaction waits for all children.

### 3e. Rejection Circuit Breaker

Each rejection is counted across all historical `TransitionResult` rows for this task
with `transition="idea_to_planning"`. After **3 rejections**, `intake_exhausted_at`
is written to the task and the scheduler stops auto-retrying. The user must use
"Reset Intake" to clear the flag.

Between rejections (before exhaustion), a `_rejection_cooldowns` timestamp prevents
immediate re-dispatch (default cooldown: configurable).

### 3f. Outputs

On `passed`:
- Writes `TransitionResult` row (`transition="idea_to_planning"`, `outcome="passed"`)
- Writes one `TransitionVote` row per stage vote
- Calls `advance_stage(task_id, "pass")` → moves task to `planning`

---

## 4. Stage 1 — PLANNING

The most complex stage. Five distinct sub-phases with cache, retry, and correction logic.

### 4a. Content Hash & Cache Gate

A SHA-256 hash of `"{title}||{description}"` is computed. If a prior `PlanningResult`
row exists for this task with the same hash AND `gate_passed=True`, it is restored
and the entire planning pipeline is skipped. This makes re-queuing after a transient
failure instant.

Cache modes:
- `normal` — use cache if available
- `force_with_context` — skip cache, run full pipeline
- `force_fresh` — skip cache, skip prior-failure context injection too

### 4b. Prior Failure Context Injection

Previous demotion history (from `PlanningResult` rows marked as failed/rejected) is
loaded and injected into the designer prompts so the agent knows what to avoid.
Specific gate check names that failed previously (e.g. `implementation_steps_present`)
trigger mandatory warnings embedded in the system prompt.

Old `planning_results` rows from prior runs are superseded (soft-archived) before
the new session starts.

### 4c. Design Generation — Best of N

```
Surveyor Agent
  │  (reads project structure, builds codebase summary)
  ▼
N × Designer Agents  (parallel asyncio.gather)
  │  Each produces:
  │    - design_rationale
  │    - file_manifest       [{path, action, purpose, estimated_lines, depends_on}]
  │    - interface_contracts [{component, provides, consumes, invariants}]
  │    - test_strategy       [{component, test_file, test_cases, fixtures}]
  │    - implementation_steps (REQUIRED — hard gate failure if empty)
  ▼
Judge Agent
  │  Reads all N designs, selects the best one
  ▼
Selected Design (stored as PlanningResult)
```

`N = PLANNING_BEST_OF_N` (default 5); reduced to 2 for "simple" tasks
(heuristic: short description + small file count).

**Hard gate on `implementation_steps`:** if empty or missing in any design, that
design is immediately rejected mid-stream with a re-prompt. After 3 failed attempts
per design slot the slot is abandoned.

### 4d. Vote Panel (Planning Review)

After a design is selected, a review panel votes on it (same ACCEPTED/REJECTED
structure as intake). On `rejected`, the rejection is counted.

**Planning rejection circuit breaker:**
After `PLANNING_MAX_REJECTIONS` (default 5) rejections, the task is added to
`_planning_stopped` and an Inbox notification is sent. The scheduler will not
auto-dispatch it again. The user must click "Run Planning" to clear the stopped flag.

Between rejections (below the limit), the task is also parked in `_planning_stopped`
requiring a manual "Run Planning" trigger (not an auto-retry cooldown — this is
intentional: failed plans need human review before retrying).

### 4e. Planning Gate (Structural Checks)

If the review panel passes the design, a second check runs: the Planning Gate.
This is a set of structural assertions:

| Check | What it verifies |
|---|---|
| `implementation_steps_present` | At least one step exists |
| `file_manifest_present` | At least one file listed |
| `interface_contracts_consistency` | Contracts are internally consistent |
| `feasibility_recheck` | LLM re-evaluates whether the plan is actually feasible |
| `context_safety_margin` | Plan fits within the LLM's context window with 15% margin |

Hard-fail checks (marked `hard_fail=True`) can trigger the PlanningCorrectionAgent.

### 4f. Planning Correction Agent

If the gate fails on a hard check AND `correction_attempts < CORRECTION_SKIP_AFTER_FAILURES`:
a `PlanningCorrectionAgent` runs. It receives the current plan, the specific gate
failures, and rewrites the plan to fix them. The corrected plan is saved back to the
`PlanningResult` row. `correction_attempts` is incremented.

After `PLANNING_GATE_FAILURES_MAX` gate failures, the task is permanently parked
(added to `_planning_stopped` + Inbox notification).

### 4g. Outputs on Pass

- `PlanningResult` row written with all five structured fields
- `gate_passed=True` and `content_hash` written for cache reuse
- `TransitionResult` row (`transition="planning_to_indev"`, `outcome="passed"`)
- `advance_stage(task_id, "pass")` → task moves to `indev`

---

## 5. Stage 2 — INDEV (Dev Orchestrator)

### 5a. Entry Guard

Requires a valid `PlanningResult` with parseable JSON in all five fields. If missing
or corrupt, immediately demotes to `planning` via `advance_stage(task_id, "fail")`.

### 5b. Review Feedback Injection

The most recent demotion from any review stage (`conceptual_to_optimization`,
`optimization_to_security`, `security_to_final_review`, `final_review_to_human_review`)
is loaded and injected into the dev agent's context. High-severity and medium-severity
findings are formatted as `[PRIOR REVIEW REJECTION — {transition}]` blocks. This
ensures the implementation agent knows what the reviewers found wrong.

### 5c. Dependency-Ordered Batch Execution

The `implementation_steps` from the planning result are grouped into batches using
topological sort on their `depends_on` relationships:

```
Batch 1: [step_A, step_B]          (no dependencies)
Batch 2: [step_C]                  (depends on A)
Batch 3: [step_D, step_E]          (depend on B, C)
```

Each batch runs in parallel (limited by `max_parallel` = LLM `parallel_sessions`).
Each step gets its own `ComponentLoop` agent.

### 5d. ComponentLoop Agent

Each component step gets a `MaestroLoop`-style agent (full tool access: `read_file`,
`write_file`, `run_pytest`, `run_mypy`, `run_ruff`, `git_add`, `git_restore`, etc.)
with the following context injected:
- The specific implementation step (component name, instructions, files to touch)
- The full planning result (file manifest, interface contracts, test strategy)
- The review feedback (if any)
- Project file-structure snapshot
- Architecture cards (all categories)

The component agent runs until it calls `submit_work` with ACCEPTED or REJECTED,
or hits `max_turns`. On REJECTED or max_turns, the component is marked failed.

**Retry:** failed components are retried up to `INDEV_COMPONENT_MAX_RETRIES` (default 2)
times within the same batch execution.

**Test-Fix loop:** after implementation, if `run_pytest` fails, the agent enters a
focused test-fix loop (up to `INDEV_TEST_FIX_MAX_RETRIES` × `INDEV_TEST_FIX_MAX_TURNS`).

### 5e. Batch Failure Handling

If any component in a batch fails after all retries:
- If the agent emitted `{"signal": "REVERT_TO_DESIGN"}`: demote to `planning`
  (`advance_stage(task_id, "reject")`). This signals the design itself is wrong.
- Otherwise: transient failure. Task stays in `indev`. Scheduler re-dispatches on
  next tick (the component work may be partially committed on the branch).

### 5f. Outputs on Pass

All batches completed with all components ACCEPTED:
- Each component's result stored as `ComponentResult` row
- `advance_stage(task_id, "pass")` → task moves to `conceptual_review`

---

## 6. Stage 3 — CONCEPTUAL REVIEW

### 6a. PIP Pre-Flight Gate

Before running the review, the PIP (Performance Improvement Plan) pre-flight gate
runs. For each active PIP on this task, an LLM call checks: *"Has this specific
requirement been addressed in the code changes since the PIP was created?"*

This check uses `git diff {pip.created_at_commit}..HEAD --stat` as evidence.
PIPs are checked concurrently via `asyncio.gather`.

If any PIP fails: dispatch `pip_resolution_jobs` (ResearchAgent → PIPResolutionAgent
cycle). Task stays in `conceptual_review`. Scheduler does not re-dispatch it until
all PIPs pass.

### 6b. Review Pipeline

Requires a valid `PlanningResult`. The `run_conceptual_review` pipeline:

1. **Research Agent** (up to `CONCEPTUAL_REVIEW_RESEARCH_LIVES` lives): reads the
   actual implementation, compares it against the plan, flags discrepancies.
   Tools: `read_file`, `list_directory`, `search_files`, `run_pytest`, `run_mypy`.

2. **Reviewer Agent** (up to `CONCEPTUAL_REVIEW_MAX_TURNS` turns): reviews research
   findings + implementation; produces a structured verdict with severity-tagged
   findings (`high`, `medium`, `low`).

3. **Tally:** If `high_severity_findings` exist AND
   `CONCEPTUAL_REVIEW_HIGH_SEVERITY_BLOCKS=True`, outcome is `failed`.
   Otherwise `passed` or `needs_human`.

### 6c. Outcomes

| Outcome | Action |
|---|---|
| `passed` | `advance_stage("pass")` → optimization |
| `failed` | `advance_stage("fail")` → indev + PIP generated for highest-severity finding |
| `needs_human` | `advance_stage("pass")` → human_review + Inbox notification |

Demotion always targets `indev` (hardcoded).

---

## 7. Stage 4 — OPTIMIZATION

### 7a. PIP Pre-Flight Gate

Same PIP gate as Conceptual Review (runs before the optimization pipeline).

### 7b. Optimization Pipeline

`run_optimization_pipeline` runs:

1. **Proposal Generation:** N agents (default `OPTIMIZATION_PROPOSAL_COUNT=5`) each
   propose a specific performance improvement targeting one bottleneck.

2. **Judge Panel:** `OPTIMIZATION_JUDGE_COUNT=3` judge agents score each proposal on a
   weighted metric:
   - Compute improvement × `OPTIMIZATION_COMPUTE_WEIGHT` (1.0)
   - Memory improvement × `OPTIMIZATION_MEMORY_WEIGHT` (0.6)
   - Storage improvement × `OPTIMIZATION_STORAGE_WEIGHT` (0.3)
   - Readability penalty (up to `OPTIMIZATION_READABILITY_PENALTY_MAX` = 0.5)
   - Tech-debt bonus (`OPTIMIZATION_TECH_DEBT_BONUS_PCT` = 1%)
   - Big-O improvement bonus (`OPTIMIZATION_BIG_O_BONUS_PCT` = 10%)
   - Premature-optimization penalty multiplier (2×)
   - Minimum improvement threshold: `OPTIMIZATION_MIN_IMPROVEMENT_PCT` = 2%
   - Maximum regression allowed: `OPTIMIZATION_MAX_REGRESSION_PCT` = 5%

3. **Implementation Agent:** The winning proposal is implemented by a `MaestroLoop`-
   style agent (up to `OPTIMIZATION_IMPL_MAX_TURNS` turns).

4. **Reviewer Agent:** Verifies the optimization was actually applied correctly and
   did not introduce regressions (up to `OPTIMIZATION_MAX_REVIEWER_TURNS` turns).

### 7c. Outcome

**Always advances to `security` on pass** (the pipeline does not have a fail/reject
path — a transient exception falls back to `indev`).

On exception: `advance_stage("fail")` → indev + demotion record.

---

## 8. Stage 5 — SECURITY

### 8a. PIP Pre-Flight Gate

Same PIP gate.

### 8b. Security Review Pipeline

`run_security_pipeline`:

1. **Research Agent** (up to `SECURITY_REVIEW_RESEARCH_LIVES` lives): reads
   the codebase; looks for OWASP top 10, injection vectors, auth flaws,
   data exposure, dependency vulnerabilities. Uses `run_bandit`, `run_pip_audit`,
   `run_semgrep`, `run_npm_audit` tools.

2. **Security Reviewer Agent** (up to `SECURITY_REVIEW_MAX_REVIEWER_TURNS` turns):
   synthesizes research findings into a verdict.

3. **Veto Power:** if `SECURITY_REVIEW_VETO_POWER=True` (default) and any
   finding is categorised as critical/high severity, the reviewer can veto
   regardless of other opinions.

### 8c. Outcomes

| Outcome | Action |
|---|---|
| `passed` | `advance_stage("pass")` → final_review |
| `failed` | **Reviewer picks demotion target** at runtime: `indev` or `optimization`; `update_task(type=demotion)` directly (bypasses `advance_stage`) |

The demotion target being chosen at runtime (not from the transition graph) is the
specific hardcoded behaviour that makes this stage non-generic.

---

## 9. Stage 6 — FINAL REVIEW

### 9a. PIP Pre-Flight Gate

Same PIP gate.

### 9b. Final Review Pipeline

`run_final_review_pipeline`:

1. **Research Agent** (up to `FINAL_REVIEW_RESEARCH_LIVES` lives):
   - Code quality check (tools: `run_mypy`, `run_ruff`, `run_bandit`)
   - Functional correctness check (`run_pytest`)
   - If frontend files detected (`FINAL_REVIEW_FRONTEND_PATTERNS`): UX review pass
     (`FINAL_REVIEW_AUTO_UX=True` by default)

2. **Final Reviewer Agent**: synthesizes all findings. Produces:
   - `outcome`: `passed` | `failed` | `needs_human`
   - `summary`
   - `demotion_target` (if failed): `indev` or `optimization`

### 9c. Virtual Merge Test

On `passed`, before advancing to human_review, a **virtual merge dry-run** executes:

```
execute_merge(task_id, project_path=real_project_root, dry_run=True)
```

This does a `git merge --no-commit --no-ff maestro/task-{id}` into a clean checkout
of `main` / `master`, then runs the test suite.

| Merge result | Action |
|---|---|
| `virtual_passed` | `advance_stage("pass")` → human_review |
| `conflict` | `advance_stage("fail")` → indev + demotion record |
| `test_failure` | `advance_stage("fail")` → indev + demotion record |
| `error` (infra) | `advance_stage("pass")` → human_review with warning (code review passed; infra failure shouldn't block) |

### 9d. Outcomes

| Outcome | Action |
|---|---|
| `needs_human` | `advance_stage("pass")` → human_review + Inbox |
| `passed` + virtual merge pass | → human_review |
| `passed` + virtual merge fail | → indev |
| `failed` | Reviewer picks `indev` or `optimization`; `update_task()` directly |

---

## 10. Stage 7 — HUMAN REVIEW

**Not auto-dispatched.** The scheduler's `SCHEDULER_DISPATCHABLE_TYPES` does not
include `human_review`. The task sits here until a human acts.

Human actions (via the board UI):
- **Approve & Merge:** triggers `execute_merge()` on the real project root; writes
  `MergeRecord`; moves task to `completed`.
- **Request Changes:** uses "Demote" to push task back to any prior stage.
- **Run Review Again:** triggers `_run_final_review_task` manually via the
  `/run-final-review` endpoint.

---

## 11. Cross-Cutting Subsystems

### 11a. PIP System (Performance Improvement Plans)

A PIP is generated every time a task is demoted from a review stage:

1. `generate_pip(task_id, origin_stage, reason)` — LLM produces a concise list of
   specific requirements the implementation MUST satisfy before re-entering review.
   The git `HEAD` SHA at time of PIP creation is stored.

2. At entry to each subsequent review stage, `run_pip_preflight()` checks all active
   PIPs concurrently:
   - Shows `git diff {created_at_commit}..HEAD --stat` as evidence
   - LLM answers: "Has this PIP requirement been meaningfully addressed?"

3. On PIP failure: `pip_resolution_jobs` are created; a `ResearchAgent` researches
   the gap; a `PIPResolutionAgent` implements fixes. Both consume LLM slots. On
   resolution completion, scheduler re-dispatches the parent stage.

### 11b. Agent Sessions

Every stage run opens an `agent_sessions` row (`create_agent_session`) and closes it
in the `finally` block (`close_agent_session`). Sessions are visible to:
- The diagnostics viewer (LLM call history)
- The scheduler's alive-check (prevents double-dispatch of active tasks)
- The MCP monitor (activity detection)

### 11c. Inbox Notifications

Triggered on specific events:
- `card_stopped`: planning exhausted; planning gate exhausted
- `needs_human`: conceptual_review or final_review escalation
- Future: merge completed, intake rejected

### 11d. Budget Tracking

Every LLM call writes a `BudgetEntry` (full prompt + response JSON) and an `Expense`
(µ¢ cost breakdown). Budget limits are enforced before each LLM call; on exhaustion
the agent loop terminates with a `BudgetExhaustedError`.

### 11e. Context Saturation Guard

`ContextTooLargeError` is raised in `llm_client.py` as a pre-flight check before
any HTTP call when `estimated_tokens > context_window - max_tokens`. Agents catch
this and terminate their loop with a `TOO_LARGE` exit rather than hitting a server
error.

---

## 12. Demotion & Feedback Paths

| From stage | Condition | To stage | Mechanism |
|---|---|---|---|
| PLANNING | review rejected | PLANNING | `_planning_stopped` (manual re-trigger) |
| PLANNING | gate failed | PLANNING | `_planning_stopped` (manual re-trigger) |
| INDEV | no planning result | PLANNING | `advance_stage("fail")` |
| INDEV | REVERT_TO_DESIGN signal | PLANNING | `advance_stage("reject")` |
| INDEV | batch failure (transient) | INDEV | stays (re-dispatch on next tick) |
| CONCEPTUAL_REVIEW | failed | INDEV | `advance_stage("fail")` + PIP generated |
| CONCEPTUAL_REVIEW | needs_human | HUMAN_REVIEW | `advance_stage("pass")` + Inbox |
| OPTIMIZATION | exception | INDEV | `advance_stage("fail")` |
| SECURITY | failed | indev OR optimization | `update_task()` directly (reviewer chooses) |
| FINAL_REVIEW | failed | indev OR optimization | `update_task()` directly (reviewer chooses) |
| FINAL_REVIEW | virtual merge conflict/fail | INDEV | `advance_stage("fail")` |

**On every demotion:** `_record_demotion_inline()` writes a `TaskHistory` row and
calls `generate_pip()` if the demotion came from a review stage.

---

## 13. Generic Node Primitives Needed

The following table maps each hardcoded behaviour to the generic node primitive
that would replace it, enabling no-Python pipeline construction.

### 13a. Node Types

| Primitive | Replaces | Config surface |
|---|---|---|
| **LLM Agent** | Every agent (surveyor, designer, judge, reviewer, researcher, implementer) | system_prompt, tools allowlist, max_turns, output schema |
| **Fan-Out / Best-of-N** | Design generation (N parallel designers) | N, agent type, merge strategy |
| **Fan-In / Judge** | Judge agent selecting best design | judge system_prompt, scoring criteria, N inputs |
| **Voting Panel** | Intake 4-stage vote, planning review vote | N voters, tally strategy (majority/unanimous/threshold), confidence threshold |
| **Parallel Branch** | Static Analysis + Conflict Detection concurrent | N parallel agents, join mode (all/any) |
| **Sequential Chain** | Scope → (Static + Conflict) → Feasibility | ordered list of nodes |
| **Research Branch** | Needs-Research handler | trigger condition (verdict == NEEDS_RESEARCH), research agent config, re-tally hook |
| **Circuit Breaker** | Intake exhaustion, planning stopped | counter key, max attempts, on-exhaust action (park / notify / demote) |
| **Gate / Assertion** | Planning gate structural checks | check list, hard_fail flags, on-fail action |
| **Correction Agent** | PlanningCorrectionAgent | trigger (gate hard-fail), agent config, max_correction_attempts |
| **PIP Generator** | `generate_pip()` on demotion | trigger (on demotion), prompt, output schema |
| **PIP Pre-Flight** | `run_pip_preflight()` at stage entry | runs before stage body; on-fail: dispatch resolution jobs |
| **Resolution Job** | ResearchAgent + PIPResolutionAgent cycle | two-phase: research → implement; completion signals parent re-dispatch |
| **Batch Executor** | Dev orchestrator dependency batches | input: step list with depends_on; executes topological batches; parallel within batch |
| **Test-Fix Loop** | ComponentLoop test-fix | trigger (test failure), max_retries, max_turns per retry |
| **Virtual Merge** | `execute_merge(dry_run=True)` in final_review | git merge target branch, run test suite, pass/fail condition |
| **Human Gate** | HUMAN_REVIEW stage | wait for manual approval signal; configurable autopilot timeout |
| **Subdivision** | SubdivisionAgent | trigger (SUBDIVIDE verdict), decomposition agent, child spawn, parent wait |
| **Inbox Notification** | `create_inbox_message()` at stop/escalation | trigger condition, subject template, outcome |
| **Cooldown / Park** | `_planning_stopped`, `_rejection_cooldowns` | max_attempts, park-until-manual vs. timed retry |
| **Score-Weighted Judge** | Optimization proposal scoring | metric weights dict, min threshold, max regression, penalty rules |

### 13b. Transition Conditions (Edge Labels)

Beyond the current `pass / fail / reject`, the generic system needs:

| Condition | Current usage |
|---|---|
| `pass` | Standard advance |
| `fail` | Soft failure (transient; stay or retry) |
| `reject` | Design rejection; demote further back |
| `needs_human` | Escalation to human gate |
| `subdivide` | Decompose into children |
| `needs_research` | Spawn research then re-evaluate |
| `pip_blocked` | PIP gate failed; stay until resolved |
| `stopped` | Circuit breaker exhausted; park indefinitely |
| `virtual_merge_conflict` | Merge dry-run failed; demote |
| `revert_to_design` | Agent signal; demote to planning |

### 13c. Data Flow Between Nodes

Each stage currently reads from fixed DB tables
(`PlanningResult`, `ComponentResult`, `TransitionResult`, etc.).
In a generic system, this becomes a **content blob** (JSON dict) on the task card
that nodes can read from and write to via declared `required_input_keys` and
`output_keys`. The document store (`project_documents`) is already available for
cross-card shared state.

### 13d. Wiring Order (priority for extraction)

Based on complexity and reuse potential:

1. **LLM Agent node** — foundational; everything else composes it
2. **Voting Panel node** — used by intake and planning; high value
3. **Circuit Breaker node** — used everywhere; prevents infinite loops
4. **Fan-Out + Judge node** — Best-of-N design generation pattern
5. **Batch Executor node** — Dev orchestrator; enables parallel work
6. **PIP Pre-Flight node** — cross-stage gate; currently duplicated in 4 stages
7. **Research Branch node** — conditional research spawn; used in 4+ stages
8. **Virtual Merge node** — end-of-pipeline gate
9. **Score-Weighted Judge node** — optimization scoring; specialized but powerful
10. **Correction Agent node** — planning self-repair; generalizes to any stage

---

*Last updated: 2026-05-16. Reflects code state as of commit `64234fd` + malleable pipeline work.*
