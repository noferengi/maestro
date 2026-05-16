# Phase 2 — Scheduler Decoupling

> **Status:** SUBSTANTIALLY COMPLETE — 2026-05-15 ⚠️ (core infrastructure done; ~15 scheduler call sites still bypass pipeline router — see audit)  
> **Depends on:** Phase 1 (all new tables present, `stage_key` populated)  
> **Estimated effort:** 5 days  
> **Goal:** Replace scattered `update_task(type=...)` mutations and the hardcoded
> per-agent dispatcher functions with a data-driven `pipeline_router.py` that resolves
> stage transitions from the DB graph. Zero visible behavior change on the Software
> Development template.

---

## Context

`scheduler.py` is 4910 lines containing 9 independent dispatch queues. Only the
**DAG task queue** (the queue that dispatches `planning`, `indev`, `review`, etc.
cards) needs to become pipeline-aware. The other 8 queues (file summaries, research
jobs, arch-gen, clarification, PIP resolution, survey, subdivision recovery, Maestro
loop) are infrastructure concerns and are untouched in this phase.

Stage transitions today are **not centralised**. They happen via:
- `update_task(task_id, type="planning")` in `app/main.py` (intake result handler)
- `update_task(task_id, type=...)` in `crud_tasks.py` (reorder/transition helpers)
- Direct `task.type = ...` assignments inside individual agent files

There is no `advance_task_type()` function to replace — instead, every call site
that mutates `task.type` must be found and routed through the new centralised path.

---

## Deliverables

1. `app/agent/pipeline_router.py` — new module with the stage resolution and
   dispatch logic
2. `app/agent/agent_registry.py` — agent registry dict mapping `agent_type` keys
   to `AgentSpec` objects for the built-in agents
3. All `update_task(type=...)` call sites updated to call
   `pipeline_router.advance_stage()` instead
4. DAG task dispatch in `scheduler.py` replaced with registry lookup
5. `tasks.type` kept in sync (written alongside `stage_key`) for backward compat
6. Full test suite passes; no behavioral change on Software Development pipeline

---

## `pipeline_router.py` — Public API

```python
# app/agent/pipeline_router.py

def get_next_stage(task_id: str, condition: str) -> str | None:
    """
    Look up the outgoing transition edges for the task's current stage_key
    in its project's pipeline template. Return the stage_key of the highest-
    priority matching edge, or None if no transition fires.
    """

def advance_stage(task_id: str, condition: str) -> bool:
    """
    Resolve the next stage_key via get_next_stage(), then write both
    task.stage_key and task.type (kept in sync) in a single DB transaction.
    Returns True if a transition fired, False if the task stays put.
    Raises ValueError if the task has no pipeline_template_id.
    """

def get_stage_config(task_id: str) -> StageConfig | None:
    """
    Return the pipeline_stages row for the task's current stage_key,
    plus its resolved config JSON (gate type, retries, tool allowlist,
    required_input_keys, upstream_task_gate, verifier, system_prompt).
    """

def dispatch_task(task_id: str, stage_key: str | None = None, **llm_config) -> bool:
    """
    Look up the registered handler for stage_key (or current task stage)
    and call it with llm_config.
    """
```

---

## `agent_registry.py` — Structure

```python
from dataclasses import dataclass, field

@dataclass
class AgentSpec:
    cls: type                          # Agent class to instantiate
    display_name: str
    description: str
    default_tools: list[str] = field(default_factory=list)
    gate_type: str = "llm_judge"       # llm_judge | single_pass | test_suite | human | voting

AGENT_REGISTRY: dict[str, AgentSpec] = {
    "planning_agent":        AgentSpec(cls=PlanningAgent, ...),
    "implementation_agent":  AgentSpec(cls=DevOrchestrator, ...),
    "review_agent":          AgentSpec(cls=ConceptualReviewAgent, ...),
    "optimization_agent":    AgentSpec(cls=OptimizationAgent, ...),
    "security_agent":        AgentSpec(cls=SecurityAgent, ...),
    "final_review_agent":    AgentSpec(cls=FinalReviewAgent, ...),
    "human_gate":            AgentSpec(cls=HumanGateAgent, ...),
    "intake_agent":          AgentSpec(cls=None, ...),  # handled by intake pipeline
    "terminal":              AgentSpec(cls=None, ...),  # no dispatch
    "arch_agent":            AgentSpec(cls=ArchGenAgent, ...),
    "custom_llm_agent":      AgentSpec(cls=CustomLLMAgent, ...),  # Phase 5
    "factory_node":          AgentSpec(cls=CardFactoryAgent, ...),  # Phase 9
}
```

`CustomLLMAgent` and `CardFactoryAgent` are stubbed (raise `NotImplementedError`)
in this phase; their stubs allow the registry to be populated without blocking Phase 2.

---

## Dispatch Change in `scheduler.py`

**Before (per-stage if/elif chain, simplified):**
```python
if task.type == "planning":
    agent = PlanningAgent(task, llm_config)
    thread = Thread(target=agent.run)
    ...
elif task.type == "indev":
    agent = DevOrchestrator(task, llm_config)
    ...
```

**After:**
```python
dispatched = pipeline_router.dispatch_task(task.id)
```

The capacity counting, thread management, and error handling that surround the
current dispatch block remain in `scheduler.py`; only the agent resolution and
instantiation move to `pipeline_router.dispatch_task()`.

---

## Call Sites to Migrate

Find every location that directly writes `task.type`:

```bash
grep -rn "type.*=.*['\"]planning\|indev\|conceptual\|optim\|security\|final\|human\|completed" \
  app/ --include="*.py"
```

Each must be replaced with `pipeline_router.advance_stage(task_id, condition)`
where `condition` is one of: `"pass"`, `"fail"`, `"reject"`, `"always"`.

During the transition period, `advance_stage()` writes **both** `stage_key` and
`type` so any code still reading `task.type` gets the correct value.

---

## Gate Condition Resolution

The existing gate logic (voting, test suite pass/fail, LLM judge) produces a
verdict. The verdict maps to a transition condition:

| Existing verdict        | Condition |
|-------------------------|-----------|
| ACCEPTED / PASS         | `pass`    |
| NEEDS_REVISION / FAIL   | `fail`    |
| NEEDS_REDESIGN / REJECT | `reject`  |
| SUBDIVIDE_IDEA          | special — handled by subdivision queue, not pipeline router |
| HUMAN_REVIEW_REQUIRED   | `pass` to human_gate stage |

---

## Data Gates (key-presence check)

Before dispatching a task to its current stage's agent, `dispatch_task()` checks:

```python
stage_config = get_stage_config(task_id)
if stage_config.required_input_keys:
    content = get_task_content(task_id)  # parsed JSON blob
    missing = [k for k in stage_config.required_input_keys if k not in content]
    if missing:
        log.info(f"Task {task_id} held at {stage_config.stage_key}: missing keys {missing}")
        return False  # scheduler skips; retries next tick
```

The upstream-task gate (second gate type) is checked similarly:
```python
if stage_config.upstream_task_gate:
    gate_task = get_task(stage_config.upstream_task_gate_id)
    if gate_task.stage_key != "completed":
        return False
```

Both checks are gating conditions, not errors. The task stays at its current stage
until the gate clears.

---

## Test Criteria

- `pipeline_router.get_next_stage(task_id, "pass")` returns the correct next
  `stage_key` for each stage in the Software Development template
- `pipeline_router.advance_stage()` writes both `stage_key` and `type` atomically
- Full intake → planning → indev → review → completed flow works end-to-end
- Demote path (reject edges) works: a card rejected from `indev` lands in `planning`
- All existing tests pass green
- `scheduler.py` diff shows no new per-agent if/elif blocks added

---

## Risk Factors

**Missed call sites** — a grep-based audit will find most `type=` writes, but some
may be in string-formatted queries or dynamically constructed. Run the test suite
after each migrated call site, not just at the end.

**Capacity counting** — the capacity counter in `scheduler.py` currently keys on
`task.type` to decide which LLM slot to charge. Verify it reads `task.stage_key`
(or `task.type` which stays in sync) correctly after the change.

**Rollback** — if Phase 2 breaks something, the rollback path is to revert
`pipeline_router.advance_stage()` calls back to direct `update_task(type=...)` calls.
Because Phase 1 left the system in a state where `type` and `stage_key` are both
present, the rollback does not require a schema change.

---

## Implementation Audit (2026-05-15)

### What was delivered

All four planned functions exist in `app/agent/pipeline_router.py` with the specified
signatures (plus a bonus `from_stage` kwarg on `advance_stage` for idempotency).
`AGENT_REGISTRY` in `app/agent/agent_registry.py` has 12 entries covering all built-in
and custom types. The DAG dispatch loop in `scheduler.py` was refactored: the per-stage
`if/elif` block is gone, replaced by `dispatch_task()` plus a late-binding handler
registration pattern that avoids circular imports. 12 unit tests in
`test_pipeline_router.py` cover the primary code paths.

Approximately 20+ call sites properly use `advance_stage()` with explicit conditions.

### Remaining hardcoded `update_task(type=...)` bypasses

~15 sites in `scheduler.py` still write `task.type` directly rather than going through
`advance_stage()`:

| Context | Lines (approx) | Why bypassed |
|---|---|---|
| MaestroLoop ACCEPTED / NEEDS_HUMAN / REVERT_TO_DESIGN / MAX_TURNS / ERROR exits | 4102–4209 | Complex multi-condition exits not modelled as graph edges |
| Planning correction-agent → INDEV path | ~3975 | Special case after PlanningCorrectionAgent patch |
| Planning → IDEA (subdivide outcome) | ~4015 | No "subdivide" condition in graph; handled as one-off |
| Security/FinalReview variable demotion targets | ~4633, ~4774 | Dynamic target (indev or conceptual_review) may not have a graph edge |

The plan's primary goal — removing the routing `if/elif` dispatch block — was achieved.
The remaining bypasses are in **outcome/error paths** inside individual agent runners,
not in the central dispatch. They do not break correctness (both `type` and `stage_key`
stay in sync via the caller's own `update_task` call) but they do mean those transitions
are not graph-driven and will not respect custom pipeline topology for non-software
templates.

### What still needs completing to call this 100%

1. Map MaestroLoop exits to graph conditions (`pass`, `fail`, `reject`, `escalate`) and
   remove the direct `update_task` calls in the DevOrchestrator exit handler.
2. Decide whether "subdivide" should be a first-class graph condition or remain a
   scheduler-level special case.
3. Replace variable-target demotions in security/final_review with a `fail` condition
   that follows the graph's default fail edge.
