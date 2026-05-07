# PIP System: Full Implementation Plan

**Status:** Design complete — ready for implementation  
**Supersedes:** The original single-column PIP approach (migration 0040 schema is kept; the
`pip_verification` stage routing and column are removed).

---

## Vision

PIPs are **permanent, card-attached annotations** — not a pipeline stage. A card with N PIPs
renders as a vertical stack of N+1 card segments. The stack is a single draggable, reorderable,
unbreakable unit everywhere it appears (board columns, Column Map view). Every pipeline stage
transition is guarded by a per-PIP pre-flight gate. If any PIP fails its gate, a PIP Resolution
Agent is launched to do targeted work, and the card cannot advance until every gate clears. PIPs
accumulate across the card's lifetime and are never removed — even at COMPLETED they are a visible
record that extra scrutiny was applied.

---

## Design Decisions (locked)

| Question | Decision |
|---|---|
| Satisfied PIPs stay visible? | Yes, always. Status badge updates per-stage per-gate-run. |
| PIPs in the Arch Bar? | Never. PIPs are card-specific; the Arch Bar is project-wide. |
| Failure at non-INDEV stage? | Pre-flight blocks advance; Resolution Agent works on the same branch. |
| Resolution agent vs voter? | Pre-flight gate + dedicated Resolution Agent. No additional voters. |
| Resolution branch? | Same `maestro/task-{id}` branch — additional commits, not a new branch. |
| PIP concurrency? | All PIPs for a card run resolution agents independently and in parallel. |
| `created_at_commit` when no git history? | Store `"none"` — pre-flight skips diff context, uses snapshot only. |
| Map view treatment? | PIP segments stack vertically below the main card node; group moves as one. |

---

## 1. Database Changes

### Migration 0041 — `pip_verifications` table + `created_at_commit` column

```python
def up(conn):
    # Verification audit trail — one row per (pip, stage, run)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pip_verifications (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            pip_id            INTEGER NOT NULL,
            task_id           TEXT    NOT NULL,
            checked_at_stage  TEXT    NOT NULL,   -- 'conceptual_review', 'optimization', etc.
            outcome           TEXT    NOT NULL,   -- 'passed' | 'failed' | 'pending'
            summary           TEXT,              -- one-line LLM verdict
            findings          TEXT,              -- JSON: [{requirement, status, detail}]
            agent_session_id  TEXT,              -- links to budget_entries for cost tracking
            created_at        TEXT    NOT NULL,
            FOREIGN KEY (pip_id)  REFERENCES performance_improvement_plans(id),
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pip_verifications_pip_stage
        ON pip_verifications(pip_id, checked_at_stage, created_at DESC)
    """)
    # Record the git commit at which the PIP was created — enables git diff context
    # in pre-flight prompts. Value is 'none' when project has no commits yet.
    conn.execute("""
        ALTER TABLE performance_improvement_plans
        ADD COLUMN created_at_commit TEXT NOT NULL DEFAULT 'none'
    """)
    conn.commit()
```

### New CRUD functions (add to `crud_tasks.py`, export from `database/__init__.py`)

```python
create_pip_verification(pip_id, task_id, stage, outcome, summary, findings, agent_session_id=None)
get_latest_pip_verification(pip_id, stage) -> PipVerification | None
get_pip_verification_map(task_id, stage) -> dict[int, str]  # pip_id → outcome
get_pip_verifications_for_pip(pip_id) -> list[PipVerification]  # full history for modal
```

### Updated `create_pip()` signature

```python
def create_pip(task_id, origin_stage, requirements,
               llm_id=None, budget_id=None,
               prompt_tokens=0, completion_tokens=0,
               created_at_commit="none"):
```

At call sites (`pip_agent.generate_pip` and any demotion hooks), capture commit via:
```python
import subprocess
result = subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=project_root, capture_output=True, text=True
)
commit = result.stdout.strip() if result.returncode == 0 else "none"
```

### ORM model: `PipVerification`

```python
class PipVerification(Base):
    __tablename__ = "pip_verifications"
    id               = Column(Integer, primary_key=True)
    pip_id           = Column(Integer, ForeignKey("performance_improvement_plans.id"), nullable=False)
    task_id          = Column(String, ForeignKey("tasks.id"), nullable=False)
    checked_at_stage = Column(String, nullable=False)
    outcome          = Column(String, nullable=False)  # 'passed' | 'failed' | 'pending'
    summary          = Column(Text)
    findings         = Column(Text)      # JSON string
    agent_session_id = Column(String)
    created_at       = Column(String, nullable=False)
```

---

## 2. PIP Status Derivation

There is no `status` column on a PIP that gets bulk-flipped. Status is **derived at read time**:

```
pip_status_at_stage(pip, current_stage):
    v = get_latest_pip_verification(pip.id, current_stage)
    if v is None:      return "unverified"   # never checked at this stage
    if v.outcome == "passed":  return "satisfied"
    if v.outcome == "failed":  return "unsatisfied"
    if v.outcome == "pending": return "checking"
```

This means: a PIP satisfied at `conceptual_review` automatically appears as `unverified` when the
card advances to `optimization` — no manual resets needed, just no row yet for that stage.

The old `status` column on `performance_improvement_plans` (`active`/`satisfied`) is **deprecated**
and ignored. Do not remove it (avoids a migration that shuffles existing rows), just stop writing
to it.

---

## 3. API Changes

### Task response: include `pips` array

In `_task_to_dict()` (or wherever task serialization happens in `main.py`), add:

```python
"pips": [
    {
        "id":            pip.id,
        "origin_stage":  pip.origin_stage,
        "requirements":  json.loads(pip.requirements),   # list of strings
        "created_at":    pip.created_at,
        "status":        pip_status_at_stage(pip, task.type),  # derived
        "last_summary":  latest_verification.summary if latest_verification else None,
        "last_checked":  latest_verification.created_at if latest_verification else None,
    }
    for pip in get_pips_for_task(task.id)
]
```

The `pips` array is always present (empty list `[]` when no PIPs). Frontend checks `task.pips.length`.

### New endpoints

```
GET  /api/tasks/{id}/pips
     → full PIP list with complete verification history per PIP
     → used by the PIP detail modal (full findings, all stages checked)

GET  /api/tasks/{id}/pips/{pip_id}/verifications
     → verification history for one PIP across all stages
     → used by the "History" tab in the PIP detail modal

POST /api/tasks/{id}/pips/{pip_id}/verify
     → manually trigger pre-flight for one PIP at the card's current stage
     → body: { llm_id?, budget_id? }
     → runs synchronously, returns { outcome, summary, findings }

POST /api/tasks/{id}/run-pip-resolution/{pip_id}
     → manually trigger PIP Resolution Agent for one PIP
     → body: { llm_id?, budget_id? }
     → fires in background; returns 202 Accepted
```

---

## 4. Pre-flight Gate Architecture

### Where it fires

Each stage worker thread (in `scheduler.py`) wraps its pipeline call with a pre-flight check.
Affected stages: `conceptual_review`, `optimization`, `security`, `final_review`.

The check does **not** add voters to the existing pipeline — it runs before the pipeline starts.
If it blocks, the pipeline function is never called.

### Pre-flight function (`pip_agent.py`)

```python
async def run_pip_preflight(
    task_id: str,
    stage: str,
    llm_id: int,
    budget_id: int,
    project_root: str,
) -> dict:
    """
    Returns:
        {
            "all_passed": bool,
            "results": [
                {
                    "pip_id": int,
                    "outcome": "passed" | "failed",
                    "summary": str,
                    "findings": list[dict],
                }
            ]
        }
    """
```

**For each PIP**, runs this prompt independently (concurrent `asyncio.gather`):

```
You are the Maestro PIP Pre-flight Verifier.

STAGE BEING ATTEMPTED: {stage}
PIP REQUIREMENT (task was demoted from: {origin_stage}):
{requirements_as_bullets}

WORK DONE SINCE PIP WAS CREATED:
{git_diff_stat}    ← output of: git diff {created_at_commit}..HEAD --stat
                    or "No commit history to diff against." if created_at_commit == 'none'

CURRENT PROJECT SNAPSHOT:
{snapshot}

Has this PIP requirement been meaningfully addressed in the code and/or documentation?
Be rigorous. A requirement is only satisfied if there is concrete evidence in the diff
or current snapshot — not just intent or comments.

Respond with JSON:
{
  "outcome": "passed" | "failed",
  "summary": "One sentence verdict.",
  "findings": [
    {"requirement": "...", "status": "satisfied" | "missing", "detail": "..."}
  ]
}
```

**After running all PIPs concurrently**, persist results:
```python
for result in results:
    create_pip_verification(
        pip_id=result["pip_id"],
        task_id=task_id,
        stage=stage,
        outcome=result["outcome"],
        summary=result["summary"],
        findings=json.dumps(result["findings"]),
    )
```

Return `{"all_passed": all(r["outcome"] == "passed" for r in results), "results": results}`.

### Stage worker integration (scheduler.py)

Each stage worker (e.g., the conceptual_review dispatch thread) becomes:

```python
def _run_conceptual_review_worker(task_id, ...):
    loop = asyncio.new_event_loop()
    try:
        pips = get_pips_for_task(task_id)
        if pips:
            preflight = loop.run_until_complete(
                run_pip_preflight(task_id, "conceptual_review", llm_id, budget_id, project_root)
            )
            if not preflight["all_passed"]:
                _schedule_pip_resolution(task_id, preflight["results"], loop)
                return   # ← stage pipeline NOT called; card stays in current column
        # All PIPs passed (or no PIPs) — run stage normally
        result = loop.run_until_complete(run_conceptual_review_pipeline(...))
        ...
    finally:
        loop.close()
```

`_schedule_pip_resolution(task_id, failed_results, loop)`:
- For each failed PIP: enqueue a `pip_resolution` job (see §6)
- Also enqueue a research job per failed PIP (using existing `ResearchAgent` machinery)
- Research output is stored and fed into the Resolution Agent's context

---

## 5. PIP Resolution Agent (`app/agent/pip_resolution.py`)

New module. A targeted, bounded implementation agent.

### Class: `PIPResolutionAgent`

```python
class PIPResolutionAgent:
    def __init__(
        self,
        task_id: str,
        pip_id: int,
        requirements: list[str],
        research_findings: str,       # from pre-flight research agent
        last_verification_findings: list[dict],  # what specifically failed
        project_root: str,
        llm_id: int,
        budget_id: int,
    ): ...
```

### System prompt structure

```
You are the Maestro PIP Resolution Agent.

Your sole objective is to satisfy the specific requirements listed below. These requirements
represent quality debts from a prior demotion. The implementation agent has already completed
the core work — you are here to close the remaining gaps.

TASK: {task_title}
BRANCH: maestro/task-{task_id}   ← commit your work here

PIP REQUIREMENTS (task was demoted from: {origin_stage}):
{requirements_as_bullets}

WHAT FAILED IN THE LAST VERIFICATION:
{last_verification_findings_formatted}

RESEARCH FINDINGS — WHAT WORK IS NEEDED:
{research_findings}

PROJECT SNAPSHOT:
{snapshot}

ARCHITECTURE CONTEXT:
{arch_context}

Work iteratively. Read the relevant files. Make targeted, minimal changes.
Commit your changes with clear messages referencing the PIP requirement.
Stop when you are confident every requirement above is satisfied.
Do NOT expand scope beyond these requirements.
After 3 consecutive tool failures, stop and emit {"signal": "RESOLUTION_STALLED"}.
```

### Tools available

Same set as `MaestroLoop`: `read_file`, `write_file`, `append_file`, `list_files`, `count_lines`,
`run_shell`, `git_status`, `git_diff`, `git_checkout`, `git_commit`, `git_branch`.  
**No** `web_search` or `web_fetch` — this is targeted implementation, not research.

### Lifecycle

- Max turns: `[pip] resolution_max_turns` (default: `20`)
- Ends normally when agent stops calling tools
- Ends early on `RESOLUTION_STALLED` signal
- On completion: calls `signal_completion(f"pip_resolution_{pip_id}")` so the scheduler knows
  to re-dispatch the parent stage (which re-runs pre-flight)

### Research phase (before Resolution Agent)

Before launching a Resolution Agent for a failed PIP, a Research Agent runs first:

```
Investigate what concrete work needs to be done to satisfy the following requirement.
Do not implement anything — produce a findings report only.

PIP REQUIREMENT: {requirement}
WHAT FAILED IN LAST VERIFICATION: {last_findings}
PROJECT SNAPSHOT: {snapshot}
```

Research findings are stored (in the `pip_resolution_jobs` table, see §6) and passed directly into
the Resolution Agent's system prompt.

---

## 6. Scheduler Changes

### Remove old pip_verification routing

- **`scheduler.py:1840`** — remove `update_task(task_id, type="pip_verification")` branch.  
  INDEV completion always routes to `conceptual_review`. Pre-flight runs there.
- **`config.py:408`** — remove `pip_verification` from `PIPELINE_COLUMN_ORDER`.
- **`config.py:459`** — remove `pip_verification` from `SCHEDULER_DISPATCHABLE_TYPES`;  
  add `pip_resolution`.

### New table: `pip_resolution_jobs`

Lightweight job queue (migration 0042):

```sql
CREATE TABLE pip_resolution_jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id           TEXT    NOT NULL,
    pip_id            INTEGER NOT NULL,
    stage_blocked_at  TEXT    NOT NULL,   -- which stage triggered the block
    research_findings TEXT,               -- populated after research agent completes
    status            TEXT    NOT NULL DEFAULT 'pending',  -- pending|researching|resolving|done|failed
    created_at        TEXT    NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id),
    FOREIGN KEY (pip_id)  REFERENCES performance_improvement_plans(id)
)
```

### New dispatch function: `_dispatch_pip_resolution_jobs()`

Called in the scheduler tick, after `_dispatch_arch_gen_jobs()` and before pipeline tasks.

Tick-order:
```
_dispatch_file_summary_jobs()
_dispatch_arch_gen_jobs()
_dispatch_pip_resolution_jobs()   ← new
pipeline tasks
```

Logic:
1. Fetch `pip_resolution_jobs` where `status IN ('pending', 'researching')`
2. For `pending` jobs: dispatch a `ResearchAgent` thread; set status → `researching`
3. For `researching` jobs where research is complete: dispatch `PIPResolutionAgent` thread;  
   set status → `resolving`
4. For `resolving` jobs where `signal_completion` has fired: set status → `done`;  
   the normal scheduler tick will now re-dispatch the parent task's pipeline stage,  
   which will re-run the pre-flight

**Concurrency:** All PIPs for a card dispatch their research/resolution workers independently.
Each resolution agent counts against per-LLM and per-compute-node caps as a normal session.
Key in `_active_sessions`: `f"pip_resolution_{pip_id}"` (not `task_id`).

### `_dispatch_conceptual_review_jobs()` (and optimization / security / final_review equivalents)

Add pre-flight wrapper at the top of each dispatch thread worker. Pattern is identical across
all four stages — extract a shared `_run_pip_preflight_and_gate(task_id, stage, ...)` helper
to avoid repeating the same wrapper four times.

---

## 7. Frontend: Card Stack Rendering

### DOM structure

Tasks with PIPs render inside a `.task-card-group` wrapper:

```html
<div class="task-card-group" data-task-id="X" draggable="true">
  <div class="task-card" data-task-id="X">
    <!-- existing card markup, unchanged -->
  </div>
  <div class="pip-card" data-pip-id="1" data-task-id="X">
    <div class="pip-card-header">
      <span class="pip-label">PIP 1</span>
      <span class="pip-origin">demoted from security</span>
      <span class="pip-status pip-status--unsatisfied">✗ Unsatisfied</span>
    </div>
    <div class="pip-card-body">
      Ensure all error paths log at ERROR level…
      <span class="pip-req-count">+2 more requirements</span>
    </div>
    <div class="pip-card-toolbar">
      <button onclick="pipVerify(X, 1)">🔍 Verify</button>
      <button onclick="pipResolve(X, 1)">🔧 Resolve</button>
      <button onclick="pipHistory(X, 1)">📋 History</button>
    </div>
  </div>
  <div class="pip-card" data-pip-id="2" ...>...</div>
</div>
```

Tasks with zero PIPs render as today — a bare `.task-card` with no wrapper.

### Status badge classes

```
pip-status--satisfied    → green  ✓ Satisfied
pip-status--unsatisfied  → red    ✗ Unsatisfied
pip-status--unverified   → amber  ◌ Unverified
pip-status--checking     → blue   ⟳ Checking
```

### CSS structure (additions to `style.css`)

- `.task-card-group` — `display: flex; flex-direction: column; gap: 0;`
- `.pip-card` — same width as `.task-card`; slightly darker background; left border 3px colored
  by status variable; no `border-top`; top border replaced with a `2px dashed` connector line
  in a muted color (the "staple" visual joining it to the card above)
- `.pip-card-header` — flex row; `pip-label` bold, `pip-origin` muted italic, `pip-status` right-aligned
- `.pip-card-body` — smaller font; truncated at 2 lines; shows first requirement + count
- `.pip-card-toolbar` — hidden by default; revealed on `.task-card-group:hover`; same reveal
  mechanic as the existing `.card-toolbar`
- `.pip-card.pip-satisfied` — slightly faded (80% opacity) to visually de-emphasize closed debt
- In the arch-bar: `.pip-card` never appears (PIPs are not arch tasks)

### `renderTasksFromDatabase()` changes

After building a task card element, check `task.pips.length`. If non-zero:
1. Create `.task-card-group` wrapper
2. Append the `.task-card` into it
3. For each PIP in `task.pips`, call `buildPipCard(pip, task.type)` → returns `.pip-card` element
4. Append pip cards into wrapper
5. Append wrapper to column container (instead of bare card)

`buildPipCard(pip, currentStage)`:
- Derives status from `pip.status` (which the API pre-computes from `pip_status_at_stage`)
- Shows first requirement string, truncated at ~80 chars
- Shows `+N more` if `pip.requirements.length > 1`

### `reconcile()` changes

The PIP status badge needs to update when a verification completes server-side. The reconciler
compares a fingerprint that includes PIP data. Add to the fingerprint string:
```js
+ task.pips.map(p => `${p.id}:${p.status}:${p.last_checked}`).join(",")
```

When fingerprint changes for a task with PIPs, re-render the group (update badge class + summary
text in-place rather than a full column re-render, to avoid scroll-position jumps).

---

## 8. Frontend: Group Drag-and-Drop

### Drag handles

`draggable="true"` lives on `.task-card-group`, not `.task-card`. For bare cards (no PIPs),
the existing `.task-card[draggable]` attribute is kept (no regression).

The drag event listeners (`dragstart`, `dragover`, `drop`, `dragend`) currently target
`.task-card`. Extend all four to also match `.task-card-group`:

```js
const isDraggable = el =>
    el.classList.contains("task-card") || el.classList.contains("task-card-group");
```

`dragstart`: record `dragging-task-id` from `dataset.taskId` (present on both `.task-card` and
`.task-card-group`). The reorder API call is the same — only the main card's `task_id` is sent.

`dragover` / `drop`: drop targets in the column are `.task-card-group` and bare `.task-card`
elements. Insert position is calculated from the top of the group container (or the bare card),
not just the card.

### Ghost element

During drag, `opacity: 0.3` is set on the entire `.task-card-group` (all segments fade together).
The browser's default drag ghost will capture the full group height since `draggable` is on the
wrapper — no custom ghost element needed.

---

## 9. Frontend: Column Map View

### Layout

In `_mapComputeLayout`, the node for a task with PIPs is given extra height in the layout
algorithm. The rendered node block is a vertical stack:
- Main card box (existing height)
- N PIP chip boxes below it (fixed height per chip, e.g. 48px each)

All chips share the same `(x, y)` anchor as the main card; they are offset vertically in
render-space only.

### Interactivity

Clicking the main card area → existing behavior (task detail / toolbar).  
Clicking a PIP chip → `openPipDetailModal(taskId, pipId)`.  
Dragging any part of the group → `_mapStartNodeDrag` fires on the group's bounding box.  
The existing group-drag logic (parent moves all descendants) is extended: PIP chips are not
separate nodes in the DAG — they have no edges — they are visual sub-elements of their parent
node and are always carried with it.

### Edge rendering

PIP chips have no incoming or outgoing DAG edges. They are decorations, not nodes, in the
graph-theory sense. No changes to `DAGResolver` needed.

---

## 10. PIP Detail Modal

New modal `#pip-detail-modal` (add to `index.html`). Opened by clicking a PIP card segment or
its "📋 History" toolbar button.

**Content:**
- Header: `PIP {N} — demoted from {origin_stage} on {created_at}`
- Requirements list (all of them, one per line, with check/cross based on latest finding)
- Verification history table: stage | outcome | summary | when
- Footer buttons: `🔍 Run Verification` · `🔧 Run Resolution` · Close

**API calls:** `GET /api/tasks/{id}/pips/{pip_id}/verifications` for full history.

---

## 11. `pip_agent.py` Refactoring

**Keep:**
- `generate_pip()` — extended with `created_at_commit` capture
- `PIP_GENERATOR_PROMPT`

**Remove:**
- `run_pip_verification_pipeline()` — superseded by `run_pip_preflight()`
- `PIP_VERIFIER_PROMPT` — replaced by pre-flight prompt (inlined in `run_pip_preflight`)

**Add:**
- `run_pip_preflight(task_id, stage, llm_id, budget_id, project_root) -> dict`
- `_check_single_pip(pip, task, stage, snapshot, llm_id, budget_id) -> dict` — called concurrently
- Helper: `_get_git_diff_stat(project_root, from_commit) -> str`

---

## 12. `pip_resolution.py` — New Module

Full implementation of `PIPResolutionAgent` class:
- `__init__` — stores all inputs
- `async run() -> dict` — main loop, returns `{"status": "done" | "stalled", "turns": int}`
- `_build_system_prompt() -> str`
- `_run_turn(messages) -> str` — single LLM call
- `_dispatch_tool(tool_name, args) -> str` — delegates to `tools.dispatch_tool()`
- Uses `signal_completion(f"pip_resolution_{pip_id}")` on exit

---

## 13. Cleanup: Remove Old pip_verification Stage Routing

Files to modify in this order (do not break mid-way):

1. **`app/agent/config.py`**
   - Remove `pip_verification` from `PIPELINE_COLUMN_ORDER` default
   - Remove `pip_verification` from `SCHEDULER_DISPATCHABLE_TYPES` default
   - Add `pip_resolution` to `SCHEDULER_DISPATCHABLE_TYPES`
   - Add new `[pip]` section: `resolution_max_turns = 20`, `preflight_temperature = 0.1`,
     `resolution_temperature = 0.4`

2. **`app/agent/scheduler.py:1836-1844`**
   - Remove the `if pips: update_task(..., type="pip_verification")` branch
   - INDEV completion always does `update_task(task_id, type="conceptual_review")`

3. **`app/web/index.html`**
   - Do NOT add a `pip_verification` column (there was never one added — confirm and keep it out)

4. **`app/web/kanban.js:39`**
   - Remove `'pip_verification': 5` from the column order map (or renumber if the map is used
     for ordering)

---

## 14. `maestro.ini` Additions

```ini
[pip]
resolution_max_turns   = 20
preflight_temperature  = 0.1
resolution_temperature = 0.4
```

---

## 15. Testing Plan

### Unit tests (`test_pip_agent_unit.py` — extend existing)
- `test_preflight_all_passed` — all PIPs return passed; stage not blocked
- `test_preflight_one_failed` — one PIP fails; `all_passed` is False; verification row written
- `test_preflight_no_commit_context` — `created_at_commit="none"`; diff section shows fallback text
- `test_generate_pip_captures_commit` — `created_at_commit` populated correctly when git exists
- `test_generate_pip_no_git` — `created_at_commit="none"` when git unavailable

### Unit tests (`test_pip_resolution_unit.py` — new)
- `test_resolution_agent_runs_to_completion`
- `test_resolution_agent_stalls_after_tool_failures`
- `test_resolution_agent_signals_completion`

### Integration test (`test_pip_workflow.py` — new)
Full workflow with mock LLM:
1. Create task → advance to INDEV → complete → advance to `conceptual_review`
2. Demote from `conceptual_review` → PIP generated with `created_at_commit`
3. Task in INDEV — verify PIP requirements injected into system prompt
4. Advance to `conceptual_review` — pre-flight runs — fails (mock LLM configured to fail)
5. `pip_resolution_jobs` row created; resolution agent dispatched
6. Resolution agent runs (mock); `signal_completion` fires
7. Scheduler re-dispatches conceptual_review — pre-flight passes — stage runs normally
8. Task at COMPLETED — PIPs still visible in API response

---

## 16. Implementation Order

Execute phases in this sequence to keep the server runnable at each step:

| Phase | Work | Testable? |
|---|---|---|
| 1 | Migration 0041 + ORM model + CRUD | DB only |
| 2 | `generate_pip()` extended; API `pips` field on task responses | API |
| 3 | Frontend card stack rendering + CSS (read-only, no drag changes yet) | Visual |
| 4 | `run_pip_preflight()` in `pip_agent.py` | Unit tested |
| 5 | Pre-flight wrapper in each stage worker | Integration |
| 6 | `pip_resolution_jobs` table (migration 0042) + scheduler dispatch | Scheduler |
| 7 | `PIPResolutionAgent` in `pip_resolution.py` | Unit tested |
| 8 | Group drag-and-drop (board + map) | Manual UI |
| 9 | PIP detail modal | Manual UI |
| 10 | Cleanup: remove pip_verification stage routing | Regression |
| 11 | Full integration test suite | CI |
