# Gap 6 — Reflection agent (skeptical verification before commit)

**Status:** Complete  
**Effort:** Medium  
**Priority:** High — directly addresses long-chain reasoning drift

---

## Problem

LLM outputs are generated in a single forward pass with no internal review. An agent
that writes 200 lines of code and calls `submit_work` has never re-read its own output
critically. Tests catch functional failures but not logical errors, wrong assumptions,
missed edge cases, or subtly incorrect reasoning that happens to produce passing output.
The architecture already handles unreliability at the system level (retries, demotions,
PIPs) but not at the output level before it enters the pipeline.

A separate invocation that reads the output skeptically — with a different framing,
possibly a different model — catches a class of errors that neither tests nor human
review reliably catch because both tend to read charitably.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Trigger** | Pipeline stage node (`agent_type = "reflection"`) — opt-in, wired by the template author wherever skeptical review is needed. Not a global system hook. |
| **Context** | Task description + final output (base); test results if available; prior reflection reports from earlier stages; tool `get_task_history_recent(max_turns)` for inspecting recent turns up to a configurable cap (default 20). Full history available via tool — not injected blindly. |
| **Output** | Structured JSON confidence block — `{confidence, issues, uncertain_about}`. After the reflection agent writes its report, Maestro reviews it using its full normal toolset and decides the next action (advance, retry, demote, create PIP). No special-case failure logic in the scheduler — Maestro is the decision-maker. |
| **Model** | Configurable per stage via `reflection_llm_id` on the stage config; falls back to `maestro_llm_id`, then project default LLM. |

---

## Implementation plan

### Phase 1 — `reflection` agent type in AGENT_REGISTRY

**`app/agent/agent_registry.py`** — add:

```python
"reflection": {
    "class": "ReflectionAgent",
    "description": "Skeptical review of a prior stage's output. Produces a structured confidence report consumed by Maestro.",
    "supported_stage_keys": None,  # usable in any stage position
}
```

**`app/agent/reflection_agent.py`** — new file:

```python
class ReflectionAgent:
    """
    Reads a task's recent output, runs skeptical analysis,
    and produces a structured JSON confidence report.
    """

    MAX_HISTORY_TURNS = 20  # configurable via stage config

    def run(self, task_id, stage_config, db, settings) -> dict:
        context = self._build_context(task_id, stage_config, db)
        report = self._run_llm_session(context, stage_config, db, settings)
        self._store_report(task_id, stage_config["stage_key"], report, db)
        return report

    def _build_context(self, task_id, stage_config, db) -> dict:
        return {
            "task_description": get_task_description(task_id, db),
            "final_output":     get_task_final_output(task_id, db),   # last write_file / write_document content
            "test_results":     get_task_test_results(task_id, db),   # last run_pytest result, or None
            "prior_reflections": get_prior_reflection_reports(task_id, db),  # list of earlier reports
        }
```

The agent's LLM session has access to one additional tool: `get_task_history_recent(task_id, max_turns)`.
This lets it pull in more context when its base context isn't enough — without flooding the
context window on every call.

---

### Phase 2 — Reflection LLM resolution

**Stage config schema** — add optional field to `PipelineStage.stage_config` JSON:

```json
{
  "reflection_llm_id": 7,
  "reflection_max_history_turns": 20
}
```

**Resolution chain in `ReflectionAgent._resolve_llm`:**
1. `stage_config.reflection_llm_id` (if set)
2. `settings.maestro_llm_id` (from `[orchestration]` section)
3. `project.llm_id` (project default)
4. Error — no LLM available

The pipeline editor UI shows a "Reflection LLM" picker on stages of type `reflection`,
identical in design to the existing LLM picker on project settings.

---

### Phase 3 — Structured confidence report schema

The reflection agent is instructed to produce (and only produce) a JSON block at the end of its session:

```json
{
  "confidence": 0.72,
  "issues": [
    {
      "severity": "blocking",
      "finding": "Off-by-one error in loop bounds — passes tests because test range ends at N-1, but real input range starts at 0."
    },
    {
      "severity": "warning",
      "finding": "Function assumes input is already sorted; this invariant is not documented or enforced at the call site."
    },
    {
      "severity": "note",
      "finding": "Variable name `x` in line 47 is ambiguous; consider renaming for clarity."
    }
  ],
  "uncertain_about": [
    "Whether the caching strategy is thread-safe under concurrent writes from multiple agents."
  ]
}
```

**Severity levels:**
- `blocking` — the reflection agent believes this is a real defect that should not advance.
- `warning` — potential issue; Maestro should weigh it but may choose to advance.
- `note` — cosmetic or speculative; for human review, not a blocker.
- `uncertain_about` — not an assertion of a bug, but an honest statement of what the agent couldn't verify.

**Report storage:** Upserted to the project document store under key
`reflection:{task_id}:{stage_key}`. Multiple reflection stages on a single task produce
separate keys. `get_prior_reflection_reports` reads all keys matching `reflection:{task_id}:*`.

---

### Phase 4 — `get_task_history_recent` tool

**`app/agent/tools.py`** — add to TOOL_REGISTRY:

```python
"get_task_history_recent": {
    "fn": handle_get_task_history_recent,
    "schema": {
        "name": "get_task_history_recent",
        "description": (
            "Read the most recent N turns of a task's LLM session history. "
            "Use to inspect the worker agent's reasoning when base context is insufficient. "
            "Cap is enforced to avoid context window overflow."
        ),
        "parameters": {
            "task_id":   {"type": "integer"},
            "max_turns": {"type": "integer", "description": "Max turns to return. Clamped to [1, 50]."}
        },
        "required": ["task_id"]
    }
}
```

`handle_get_task_history_recent(task_id, max_turns=20)`:
- Clamps `max_turns` to `[1, 50]`.
- Reads budget entries for `task_id`, sorted by `created_at DESC`, limit `max_turns`.
- Returns each entry as `{role, content_preview, finish_reason, tokens}`.
- **Does not** reconstruct full delta history (that's `get_budget_entry_full`) — previews only, to control size.

This tool is available only to `reflection` stage agents and Maestro. Not in the standard
worker allowlist.

---

### Phase 5 — Maestro review of reflection reports

The reflection stage does **not** have a built-in pass/fail gate. After the reflection
agent writes its report to the document store and returns, the pipeline router calls
`autopilot_tick()` for the project (or triggers a Maestro assessment if autopilot is not
enabled). Maestro reads the reflection report with its full toolset and decides:

- **No blocking issues, confidence ≥ threshold:** Advance the task to the next stage.
- **Blocking issues found:** One of:
  - Retry the prior stage with the report injected into the worker's context as a
    `[REFLECTION FEEDBACK]` prefix in the system prompt.
  - Demote the task (existing demotion logic).
  - Create a PIP card describing the issue for human resolution.
- **Warnings only:** Advance but append findings to the task's `last_assessment` for
  the human_review stage to surface.

This design means Maestro's judgment, not hardcoded routing, determines the consequence
of any specific finding. A warning in a `CALIBRATION` stage is treated differently from
the same warning in a `FINAL_REVIEW` stage.

**API route** — `POST /api/tasks/{id}/trigger-reflection`:
- Explicitly triggers the reflection stage for a task (manual human trigger).
- Returns `{report: {...}, stored_key: "reflection:{id}:{stage_key}"}`.

**Confidence threshold** — configurable in `maestro.ini`:
```ini
[reflection]
confidence_threshold = 0.7   ; below this, Maestro treats as blocking regardless of issues list
max_history_turns = 20
```

---

### Phase 6 — Pipeline editor integration

**New stage type `reflection`** — appears in the stage type dropdown alongside `custom_llm`,
`verifier`, `human_review`, etc.

When a stage of type `reflection` is selected in the editor:
- The **Reflection LLM** picker appears (defaults to "Use orchestrator LLM").
- A **Max history turns** number input appears (default 20, range 1–50).
- The system prompt field is pre-filled with a starter template:

```
You are a skeptical reviewer. Your role is to find problems with the work product
described below — not to be encouraging, but to identify real defects, wrong assumptions,
and missed edge cases that the producing agent may have overlooked.

Be specific. Vague concerns do not help. If you are uncertain, say so in `uncertain_about`.
Do not invent issues. A high-confidence clean report is valuable. Produce your structured
JSON report at the end of your analysis.
```

**Built-in template updates** — the Software Development template gets a reflection stage
wired between `OPTIMIZATION` and `SECURITY`. The Math Proof template gets one after
`PROOF_ATTEMPT` (before `FORMAL_VERIFICATION`).

---

### Phase 7 — Tests

1. **Unit** — `ReflectionAgent._build_context`: includes test results when available, `None`
   when not; includes all prior reflection reports for the task.
2. **Unit** — `get_task_history_recent`: `max_turns` clamped to [1, 50]; returns previews,
   not full delta reconstructions.
3. **Unit** — LLM resolution chain: `reflection_llm_id` on stage → `maestro_llm_id` → project default → error.
4. **Unit** — report storage: `reflection:{task_id}:{stage_key}` key format; second reflection
   on same stage overwrites; different stage keys produce different keys.
5. **Unit** — `get_prior_reflection_reports`: returns reports from all stages for a task,
   sorted by stage position.
6. **Integration** — full reflection lifecycle: task at reflection stage → agent runs →
   report stored → Maestro tick reads report → advances or retries as appropriate.
7. **Integration** — pipeline editor: stage of type `reflection` serialises/deserialises
   `reflection_llm_id` and `reflection_max_history_turns` through the existing stage CRUD.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/agent/reflection_agent.py` | **New file** — ReflectionAgent class |
| `app/agent/agent_registry.py` | Register `"reflection"` type |
| `app/agent/tools.py` | Register `get_task_history_recent` |
| `app/agent/pipeline_router.py` | Handle `reflection` stage transitions; trigger Maestro review after |
| `app/agent/maestro_loop.py` | Maestro reads reflection reports during tick assessment |
| `app/database/crud_documents.py` | `get_prior_reflection_reports(task_id)` helper |
| `app/main.py` | `POST /api/tasks/{id}/trigger-reflection` route |
| `maestro.ini` | `[reflection]` section: `confidence_threshold`, `max_history_turns` |
| `app/web/pipeline_editor.js` | Reflection LLM picker + max_history_turns input for reflection stage type |
| Built-in template seeds (DB) | Software Dev + Math Proof templates get reflection stages |
| `app/tests/test_reflection_agent.py` | **New file** — all tests for this gap |

---

## Acceptance criteria

- [x] A `reflection` stage node in the pipeline editor runs `ReflectionAgent`, produces a structured JSON report, and stores it at `reflection:{task_id}:{stage_key}` in the document store.
- [x] The reflection agent has access to `get_task_history_recent` with a capped turn count; it does not have access to write tools.
- [x] Prior reflection reports from earlier stages are available in the reflection agent's context.
- [x] LLM resolution uses `reflection_llm_id` → `maestro_llm_id` → project default.
- [x] Maestro reads the report and decides the next action — no hardcoded pass/fail gate in the scheduler.
- [x] Software Development and Math Proof built-in templates include reflection stages at the specified positions.
- [x] All new code passes existing test suite with no regressions.
