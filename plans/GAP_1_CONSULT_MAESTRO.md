# Gap 1 — Fix `consult_maestro` (broken escalation tool)

**Status:** Implemented (2026-05-19)  
**Effort:** Small-Medium  
**Priority:** Highest — blocks every other capability

---

## Problem

`consult_maestro` exists in `TOOL_SCHEMAS` and `TOOL_CATEGORIES` but is absent from
`TOOL_REGISTRY`. Any agent that calls it receives `ERROR: Unknown tool`. Inner agents
have no working path to escalate architectural ambiguity or repeated failures upward
to the orchestrator.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Signal semantics** | Non-terminal. ConsultAgent fires synchronously inline; calling agent receives the answer as a normal tool result and continues in the same session. |
| **Who answers** | A dedicated `ConsultAgent` — a slim Maestro-mode session that spins up, reasons over the question, and returns an answer before the calling session continues. No human loop, no re-dispatch. |
| **ConsultAgent context** | All arch cards in context; project document store titles as a menu; tools to pull full document content, module summaries, and project-level summaries on demand. Same bird's-eye tooling Maestro uses in any other role. |
| **Maestro LLM** | A new `maestro_llm_id` config key (project setting + `maestro.ini` fallback). Shared by ConsultAgent and all future Maestro-mode operations. Independent of the task's worker LLM. |
| **Guard-rails** | Per-session call cap: `N` calls (configurable, default 3). On the Nth+1 call, the tool returns an error instructing the agent to make its best judgment with available information. |
| **Scope** | Global — available to every agent in every stage automatically. Excluded from Maestro's own `allowed_tools` to prevent self-consultation loops. |
| **Persistence** | No `consultations` table needed. The answer is injected as a tool result into the existing budget trace; no extra state survives beyond what already does. |

---

## Implementation plan

### Phase 1 — Maestro LLM config

**Goal:** Introduce `maestro_llm_id` as a first-class config key used by all Maestro-mode operations.

1. **`maestro.ini`** — add `[orchestration]` section with `maestro_llm_id` (integer, optional; falls back to project default if unset).
2. **`app/agent/config.py`** — read `maestro_llm_id` from ini + env override (`MAESTRO_ORCHESTRATOR_LLM_ID`). Expose as `settings.maestro_llm_id`.
3. **`app/database/models.py`** — add `maestro_llm_id` nullable FK column to `projects` table.
4. **Migration** — new migration `NNNN_maestro_llm_id.py`: `ALTER TABLE projects ADD COLUMN maestro_llm_id INTEGER REFERENCES llms(id)`.
5. **`app/main.py`** — include `maestro_llm_id` in project CRUD (GET/PUT). Update `_task_to_dict` / project serialisation.
6. **UI** — add an "Orchestrator LLM" dropdown to the project settings panel (same component pattern as the existing LLM picker).

---

### Phase 2 — Register `consult_maestro` in TOOL_REGISTRY

**Goal:** Remove the `ERROR: Unknown tool` failure.

1. Locate current stub in `TOOL_SCHEMAS` / `TOOL_CATEGORIES` (likely `app/agent/tools.py` or `app/agent/tool_schemas.py`).
2. Add entry to `TOOL_REGISTRY`:
   ```python
   "consult_maestro": {
       "fn": handle_consult_maestro,
       "schema": TOOL_SCHEMAS["consult_maestro"],
   }
   ```
3. The handler (`handle_consult_maestro`) is a placeholder at this stage — it raises `NotImplementedError` with a clear message so tests can target it. Full implementation in Phase 3.
4. Add `consult_maestro` to the pipeline editor UI's tool picker (it should be visible and selectable, but the stage-level allowed_tools system is bypassed — see Phase 4).

---

### Phase 3 — Build ConsultAgent

**Goal:** Implement the synchronous sub-agent that answers the escalated question.

**`app/agent/consult_agent.py`** — new file.

```
ConsultAgent(question, task_id, session_id, db, settings)
  → builds context pack:
      arch_cards         = fetch all architecture tasks for the project (title + description)
      document_titles    = list all keys in project document store
      tools              = [get_document, get_module_summary, get_project_summary,
                            list_tasks, get_task_description]   # read-only Maestro tools
  → resolves maestro_llm_id (project setting → ini → error)
  → runs a single-turn (or short multi-turn) LLM session:
      system: "You are The Maestro, the orchestrating intelligence for this project.
               An inner agent has escalated a question that requires architectural
               judgment. Answer concisely and decisively. Use your tools if needed.
               The calling agent will continue its session with your answer."
      user:   <question>
  → returns answer string
```

Key constraints:
- ConsultAgent does **not** call `consult_maestro` (excluded from its tool list).
- ConsultAgent budget entries are charged to the same task's budget, tagged `role=consult`.
- ConsultAgent session ends after it produces an answer — it does not loop.
- Max turns for ConsultAgent: configurable, default 5 (keeps it tight; it shouldn't need to browse extensively).

---

### Phase 4 — Wire call cap into MaestroLoop

**Goal:** Prevent runaway escalation chains.

1. **`app/agent/maestro_loop.py`** (or equivalent) — add `consult_call_count: int = 0` to session state.
2. In `handle_consult_maestro`:
   - Increment `consult_call_count`.
   - If `consult_call_count > settings.consult_max_calls_per_session` (default 3):
     - Return a tool result: `"You have reached the consult_maestro limit for this session. Make your best judgment with the information available and proceed."`
     - Do **not** spin up ConsultAgent.
   - Otherwise, spin up ConsultAgent, await answer, return answer as tool result.
3. **`maestro.ini`** — add `consult_max_calls_per_session = 3` under `[orchestration]`.

---

### Phase 5 — Scope enforcement (exclude from Maestro)

**Goal:** Prevent Maestro from consulting itself.

1. Identify where Maestro's own `allowed_tools` list is built (likely `build_tool_schemas(allowed_names)` in `config.py` or the Maestro agent definition).
2. Ensure `consult_maestro` is absent from that list.
3. For all other agent types, `consult_maestro` is injected automatically (no per-stage config needed). Verify this in `build_tool_schemas` — if it filters by an allowlist, `consult_maestro` must be in the global baseline, not the per-stage list.

---

### Phase 6 — Tests

1. **Unit** — `handle_consult_maestro` with a mock ConsultAgent: verify answer is returned as tool result, verify call cap fires on the Nth+1 call, verify Maestro's tool list excludes it.
2. **Integration** — a minimal inner agent session that calls `consult_maestro` once; assert the answer appears in the budget trace as a `role=consult` entry and the calling session continues.
3. **Config** — verify `maestro_llm_id` resolves correctly through the ini → env → project-setting priority chain.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/agent/tools.py` (or `tool_schemas.py`) | Register `consult_maestro` handler in TOOL_REGISTRY |
| `app/agent/consult_agent.py` | New file — ConsultAgent implementation |
| `app/agent/maestro_loop.py` | Add `consult_call_count` session state; route to handler |
| `app/agent/config.py` | Read `maestro_llm_id`, `consult_max_calls_per_session` |
| `app/database/models.py` | Add `maestro_llm_id` FK to `projects` |
| `app/migrations/versions/NNNN_maestro_llm_id.py` | New migration |
| `app/main.py` | Project CRUD: include `maestro_llm_id` |
| `maestro.ini` | New `[orchestration]` section |
| `app/web/` (project settings UI) | Orchestrator LLM picker |
| `app/tests/` | Unit + integration tests for the above |

---

## Acceptance criteria

- [x] Calling `consult_maestro` from any non-Maestro agent returns a coherent answer as a tool result; the calling session continues without interruption.
- [x] ConsultAgent uses `maestro_llm_id` (not the task's worker LLM).
- [x] A session that calls `consult_maestro` more than `consult_max_calls_per_session` times receives the cap error on the excess calls.
- [x] Maestro's own tool list does not include `consult_maestro`.
- [x] `maestro_llm_id` is configurable per-project in the DB and falls back gracefully to the ini setting and system setting. UI picker not yet added (post-implementation enhancement).
- [x] All new code passes existing test suite (no regressions — 844 tests pass).
- [x] ConsultAgent budget entries appear in the task's budget trace tagged with agent_name="ConsultAgent".
