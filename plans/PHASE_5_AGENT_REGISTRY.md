# Phase 5 — Agent Registry & Custom Agents

> **Status:** SUBSTANTIALLY COMPLETE — 2026-05-15 ⚠️ (verifier framework coded but not wired into gate; see audit)  
> **Depends on:** Phase 2 (registry stub exists); Phase 3 (API for custom_agent_definitions)  
> **Estimated effort:** 4 days  
> **Goal:** Make `CustomLLMAgent` fully operational, refactor subdivision into the
> registry model, add the `batch_create_cards` tool, and add the pluggable verifier
> system. Built-in agents get registered specs. The Software Development pipeline
> continues working unchanged.

---

## Deliverables

1. `CustomLLMAgent` class — reads `custom_agent_definitions`, injects system prompt,
   enforces tool allowlist, runs gate, writes output keys to task content blob
2. `batch_create_cards` tool — available to any agent whose stage config includes it
3. `SubdivisionAgent` refactored to use `batch_create_cards` instead of its current
   hardcoded card-creation path
4. Pluggable verifier framework — `run_verifier(task_id, stage_config)` dispatches
   to the configured verifier (none, lean4, coq, python_sympy, custom_script)
5. CRUD endpoints for `custom_agent_definitions` (wired through Phase 3 API layer)
6. All built-in agents have complete `AgentSpec` entries in `AGENT_REGISTRY`

---

## `CustomLLMAgent`

```python
class CustomLLMAgent(AgentLoop):
    """
    A generic LLM agent whose behavior is entirely driven by a
    custom_agent_definitions row rather than hardcoded Python logic.
    """
    def __init__(self, task, stage_config: StageConfig):
        defn = get_custom_agent_definition(stage_config.agent_type)
        system_prompt = defn.system_prompt
        allowed_tools = build_tool_schemas(defn.allowed_tools)
        super().__init__(task, system_prompt=system_prompt, tools=allowed_tools)
        self.stage_config = stage_config
        self.verifier = stage_config.verifier

    async def run(self):
        result = await self._llm_loop()          # inherited from AgentLoop
        if self.verifier != "none":
            passed = run_verifier(self.task.id, self.stage_config)
            condition = "pass" if passed else "fail"
        else:
            condition = self._evaluate_gate(result)
        pipeline_router.advance_stage(self.task.id, condition)
```

The `_llm_loop()` in `AgentLoop` handles tool calls, context management, and the
retry/timeout logic. `CustomLLMAgent` only overrides where its system prompt and
tool list come from.

---

## `batch_create_cards` Tool

Available to any agent whose stage config includes `"batch_create_cards"` in
`tool_allowlist`.

### Tool schema (as seen by the LLM)

```json
{
  "name": "batch_create_cards",
  "description": "Create multiple new cards in the current project. Each card enters the pipeline at the specified entry stage. If a parent card is specified, the current card becomes a legacy archive card.",
  "parameters": {
    "type": "object",
    "properties": {
      "cards": {
        "type": "array",
        "items": {
          "type": "object",
          "properties": {
            "title":       { "type": "string" },
            "description": { "type": "string" },
            "entry_stage": { "type": "string", "description": "stage_key where the new card starts" },
            "tags":        { "type": "array", "items": { "type": "string" } },
            "prereq_ids":  { "type": "array", "items": { "type": "string" } }
          },
          "required": ["title", "entry_stage"]
        }
      },
      "new_parent": {
        "type": "object",
        "description": "If provided, creates this as a new parent card and re-parents the created cards under it.",
        "properties": {
          "title":       { "type": "string" },
          "description": { "type": "string" }
        }
      },
      "archive_origin": {
        "type": "boolean",
        "default": false,
        "description": "If true, the current card is demoted to an archive/origin card after batch creation."
      }
    },
    "required": ["cards"]
  }
}
```

### Tool implementation

```python
def batch_create_cards(task_id: str, cards: list[CardSpec],
                       new_parent: ParentSpec | None, archive_origin: bool) -> dict:
    project = get_project_for_task(task_id)
    template = get_template_for_project(project.id)

    # Validate all entry_stage values exist in the template
    for card in cards:
        if not template.has_stage(card.entry_stage):
            raise ValueError(f"Unknown entry_stage: {card.entry_stage}")

    created_ids = []
    parent_id = None

    if new_parent:
        parent = create_task(project=project.name, title=new_parent.title,
                             description=new_parent.description,
                             stage_key=template.first_stage_key, type=template.first_stage_key)
        parent_id = parent.id

    for card in cards:
        t = create_task(project=project.name, title=card.title,
                        description=card.description, stage_key=card.entry_stage,
                        type=card.entry_stage, parent_task_id=parent_id,
                        prerequisites=card.prereq_ids, tags=card.tags)
        created_ids.append(t.id)

    if archive_origin:
        update_task(task_id, type="archive", stage_key="archive",
                    description_original=get_task(task_id).description)

    return {"created_ids": created_ids, "parent_id": parent_id}
```

---

## Subdivision Refactor

The existing `SubdivisionAgent` in `app/agent/subdivide.py` currently:
1. Segments the task via LLM
2. Creates child tasks directly using internal DB calls
3. Has hardcoded stage assignment logic

After this phase, `SubdivisionAgent` becomes a thin wrapper:
1. Calls `_llm_loop()` with a system prompt that instructs the LLM to call
   `batch_create_cards` to segment the work
2. The LLM decides segmentation and calls the tool
3. `batch_create_cards` handles card creation, parent creation, and origin archiving
4. `entry_stage` in each card comes from the pipeline template's
   `subdivision_entry_stage` config key (default: template's first stage)

The `SubdivisionRecord` audit table is populated from the `batch_create_cards`
return value, preserving the existing audit trail.

---

## Pluggable Verifier Framework

```python
# app/agent/verifiers.py

def run_verifier(task_id: str, stage_config: StageConfig) -> bool:
    """Returns True (pass) or False (fail)."""
    verifier = stage_config.verifier
    if verifier == "none":
        return True
    elif verifier == "python_sympy":
        return _run_sympy(task_id, stage_config)
    elif verifier == "lean4":
        return _run_lean4(task_id, stage_config)   # requires Lean 4 installed
    elif verifier == "coq":
        return _run_coq(task_id, stage_config)     # requires Coq installed
    elif verifier == "custom_script":
        return _run_custom(task_id, stage_config)  # runs verifier_cmd with task content as stdin
    else:
        raise ValueError(f"Unknown verifier: {verifier}")

def _run_sympy(task_id: str, stage_config: StageConfig) -> bool:
    content = get_task_content(task_id)
    proof_code = content.get("sympy_proof_code", "")
    if not proof_code:
        return False
    result = subprocess.run(
        ["python", "-c", proof_code],
        capture_output=True, text=True, timeout=30
    )
    return result.returncode == 0

def _run_lean4(task_id: str, stage_config: StageConfig) -> bool:
    # Requires `lean` on PATH. Punt Lean 4 specifics to later.
    content = get_task_content(task_id)
    lean_code = content.get("lean_proof", "")
    if not lean_code:
        return False
    with tempfile.NamedTemporaryFile(suffix=".lean", mode="w", delete=False) as f:
        f.write(lean_code)
    result = subprocess.run(["lean", f.name], capture_output=True, timeout=120)
    return result.returncode == 0

def _run_custom(task_id: str, stage_config: StageConfig) -> bool:
    content_json = json.dumps(get_task_content(task_id))
    result = subprocess.run(
        stage_config.verifier_cmd, shell=True, input=content_json,
        capture_output=True, text=True, timeout=60
    )
    return result.returncode == 0
```

Note: Lean 4 specifics are deferred — the framework slot exists but the
implementation is a stub that logs a warning and returns `False` until Lean 4
integration is properly specified and tested.

---

## Custom Agent Definition CRUD

Piggybacks on the Phase 3 API pattern:

```
GET    /api/agent-definitions             — list all custom agent definitions
POST   /api/agent-definitions             — create new definition
GET    /api/agent-definitions/{id}        — get one definition
PUT    /api/agent-definitions/{id}        — update (name, system_prompt, tools, gate_type, verifier)
DELETE /api/agent-definitions/{id}        — delete (blocked if used in any pipeline stage)
```

On create/update, the definition's `name` is registered as a key in `AGENT_REGISTRY`
at runtime. On server restart, the startup sequence loads all `custom_agent_definitions`
rows and populates the registry dynamically alongside the built-in agents.

---

## Test Criteria

- Create a `custom_agent_definitions` row with a simple system prompt and
  `allowed_tools=["read_file", "write_file"]`; assign it to a stage in a test pipeline;
  dispatch a task to that stage → agent runs with the custom prompt
- `batch_create_cards` called with 3 cards → 3 new tasks appear in the DB at the
  specified entry stages
- `batch_create_cards` with `archive_origin=true` → origin task type becomes `archive`
- `run_verifier` with `verifier="python_sympy"` and valid SymPy code → returns True;
  with invalid code → returns False (not an exception)
- Subdivision agent dispatched on a task → calls `batch_create_cards`, produces
  child tasks, `SubdivisionRecord` row created
- Server restart with a custom agent definition in DB → definition appears in
  `GET /api/pipelines/agent-types`

---

## Risk Factors

**SubdivisionAgent system prompt regression** — the current subdivision system prompt
is tuned for the software development domain. After the refactor, the LLM drives
card creation via `batch_create_cards`. The system prompt must include explicit
instructions about the tool's JSON schema. Run the existing subdivision tests
against the refactored agent before merging.

**Verifier subprocess isolation** — SymPy and custom verifier scripts run in a
subprocess with `timeout`. For a multi-tenant or adversarial environment this is
insufficient sandboxing. For the current single-user local deployment it is
acceptable. Document the limitation; do not add sandboxing now.

**Registry mutation during dispatch** — if a custom agent definition is deleted
while a task is mid-dispatch, `AGENT_REGISTRY[stage_config.agent_type]` will
KeyError. Add a try/except that logs and leaves the task in place (scheduler will
retry next tick).

---

## Implementation Audit (2026-05-15)

### What was delivered

`CustomLLMAgent` in `app/agent/custom_llm_agent.py` is fully functional: reads
`custom_agent_definitions` by name, injects system_prompt, enforces allowed_tools
(always appends `submit_work`), handles ACCEPTED/REJECTED/MAX_TURNS exits, and
correctly advances stage via `pipeline_router`.

`batch_create_cards` tool in `app/agent/tools.py` is fully implemented with the
specified schema: `cards`, `new_parent`, `archive_origin`. It resolves `sub-N`
prerequisite references, inherits LLM/budget IDs from the parent task, and
archives the origin task when `archive_origin=True`.

`SubdivisionAgent` in `subdivide.py` was refactored: `batch_create_cards` is in
its tool allowlist, the system prompt documents the tool, and the handler captures
created IDs on success without requiring a legacy structured-output parse.

All five custom agent definition CRUD endpoints in `main.py` are functional including
the deletion-blocked-if-in-use guard. `load_custom_agents_into_registry()` populates
the registry both at startup and on create/update.

The verifier framework (`app/agent/verifiers.py`) implements all five verifier types
with correct subprocess isolation and timeouts.

### Critical gap: verifier framework not wired in

`run_verifier()` **is never called**. `CustomLLMAgent` does not invoke the verifier
after its LLM loop even when `stage_config.verifier != "none"`. The plan's spec
(`app/agent/custom_llm_agent.py` excerpt, lines 43–50) shows the call sequence:
```python
if self.verifier != "none":
    passed = run_verifier(self.task.id, self.stage_config)
    condition = "pass" if passed else "fail"
```
This block was not implemented. Any stage configured with a verifier (python_sympy,
custom_script) will silently skip verification and gate on the LLM's own output instead.

**Fix:** In `custom_llm_agent.py`, after `result = await self._llm_loop()`, add the
verifier call before calling `pipeline_router.advance_stage()`.

### No test for `_run_llm_segmented` (Phase 9 overlap)

The custom agent test path is covered by `test_pipeline_router.py` dispatch tests but
there is no end-to-end test of `CustomLLMAgent.run()` with a mocked LLM.
