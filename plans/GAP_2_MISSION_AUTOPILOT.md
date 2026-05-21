# Gap 2 — Mission-driven autopilot (closed-loop proactive orchestration)

**Status:** Complete (2026-05-19)  
**Effort:** Medium  
**Priority:** High — unlocks research, story writing, math exploration autonomously

---

## Problem

Maestro currently fires only on stalls (no pipeline activity for `MAESTRO_STALL_TICKS`
consecutive ticks). It can propose new cards in survey mode but only reactively. It
cannot initiate a project from scratch, pursue a stated goal across multiple ticks, or
drive work to completion without a human creating the first card.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Mission storage** | Dedicated `autopilot_objectives` table — one row per objective, with priority, status, time_box, and objective_id tagged on all spawned cards. |
| **Trigger** | Adaptive — fires on stall detection OR task completion events; self-suppresses when in-flight card count exceeds the project saturation cap. |
| **Progress signal** | LLM self-assessment (narrative notes + explicit dead-end recording) + stage distribution heuristic as a cheap secondary signal + spin detection as a hard safety brake. Dead ends are treated as positive progress — "we know which paths don't work" is a valid forward state. |
| **Card creation** | All autopilot-spawned cards start at IDEA and go through the existing intake vote. No fast-pathing. |
| **Shutdown** | Time-box expiry; Maestro autonomous completion (requires multi-tick sustained "appears complete" before flipping status); human toggle (pause/complete/delete via UI); spin detection auto-pauses stuck objectives and surfaces them for human review. |
| **Multi-project** | Each project with autopilot enabled gets a dedicated `autopilot_budget_id`. Ticks for that project are charged to its pool; autopilot self-suppresses when the pool is exhausted. |

---

## Implementation plan

### Phase 1 — `autopilot_objectives` table and API

**Migration** (`NNNN_autopilot_objectives.py`):

```sql
CREATE TABLE autopilot_objectives (
    id                  SERIAL PRIMARY KEY,
    project_id          INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    description         TEXT    NOT NULL,
    priority            INTEGER NOT NULL DEFAULT 5,
    status              TEXT    NOT NULL DEFAULT 'active'
                            CHECK (status IN ('active', 'paused', 'complete')),
    time_box_hours      INTEGER NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at          TIMESTAMPTZ NULL,   -- set on create if time_box_hours provided
    completed_at        TIMESTAMPTZ NULL,
    last_assessment     TEXT    NULL,       -- Maestro's most recent narrative note
    assessment_tick     INTEGER NULL,       -- scheduler tick of last assessment
    appears_complete_since TIMESTAMPTZ NULL -- set when Maestro first flags as "appears complete"
);

-- Tag spawned cards with the objective that created them
ALTER TABLE tasks ADD COLUMN autopilot_objective_id INTEGER NULL
    REFERENCES autopilot_objectives(id) ON DELETE SET NULL;
```

Also add to `projects`:
```sql
ALTER TABLE projects ADD COLUMN autopilot_budget_id INTEGER NULL REFERENCES budgets(id);
ALTER TABLE projects ADD COLUMN autopilot_max_in_flight INTEGER NOT NULL DEFAULT 10;
```

**`app/database/crud_autopilot.py`** — new CRUD module:
- `create_objective(project_id, description, priority, time_box_hours) → Objective`
- `list_objectives(project_id, status='active') → list[Objective]`
- `update_objective_status(obj_id, status, completed_at=None)`
- `record_assessment(obj_id, notes, tick, appears_complete: bool)`
- `get_in_flight_count(project_id) → int`  — counts tasks tagged with an objective_id that are not completed/failed

**API routes** (in `app/main.py`):
```
GET    /api/projects/{name}/objectives          — list objectives (filterable by status)
POST   /api/projects/{name}/objectives          — create objective
PUT    /api/projects/{name}/objectives/{id}     — edit description, priority, time_box, status
DELETE /api/projects/{name}/objectives/{id}     — delete
```

---

### Phase 2 — Trigger integration

**Stall path augmentation (`app/agent/maestro_loop.py` or scheduler):**

Add an `autopilot_tick(project_id, db, settings)` function called:
1. When the existing stall detector fires for a project.
2. When any task for the project transitions to COMPLETED or a terminal failure stage.

Before running, `autopilot_tick` checks:
```python
active_objectives = list_objectives(project_id, status='active')
if not active_objectives:
    return  # nothing to do

in_flight = get_in_flight_count(project_id)
if in_flight >= project.autopilot_max_in_flight:
    log("autopilot suppressed: board saturated")
    return

budget_ok = check_autopilot_budget(project.autopilot_budget_id)
if not budget_ok:
    log("autopilot suppressed: budget exhausted")
    return
```

Objectives are processed in descending priority order. If the Maestro LLM slot is busy, the tick is deferred to the next natural trigger rather than queuing.

---

### Phase 3 — LLM self-assessment

At the start of each tick, for each active objective (highest priority first, up to a configurable `max_objectives_per_tick = 2`):

**Assessment prompt** fed to ConsultAgent-style session (uses `maestro_llm_id`):
```
System: You are The Maestro. Your role is to evaluate progress toward an objective
        and decide the next action.

Objective: {description}
Time box: {time_box_hours or "none"}
Created: {created_at}
Cards spawned by this objective: {tagged_cards with stage + demotion count}
Stage distribution this tick vs. last assessment tick: {diff}
Prior assessment notes: {last_assessment}

Evaluate:
1. Am I making forward progress? Include dead ends — knowing what doesn't work IS progress.
2. Is this objective complete? (Only say yes if you are certain. Err toward "not yet".)
3. What should be created next? Provide 0-3 card ideas with titles and brief descriptions.
4. If I appear stuck, explain why and what a human should decide.
```

**Response parsed for:**
- `appears_complete: bool` — if True and `appears_complete_since` is already set and the gap is ≥ 1 tick, flip status to `complete`.
- `stuck: bool` — if True, flip status to `paused`, surface badge in UI.
- `new_cards: list[{title, description}]` — each created as an IDEA card tagged with `autopilot_objective_id`.
- `assessment_notes: str` — stored in `last_assessment`.

**Multi-tick completion confirmation:** Maestro sets `appears_complete_since` on first confident "complete" assessment. Only flips `status='complete'` on a subsequent tick if still confident. Human can accept or dismiss the completion via UI.

---

### Phase 4 — Spin detection

Separate from the LLM assessment — a cheap DB query run every tick before the LLM call:

```python
def detect_spin(objective_id, demotion_threshold=2, card_threshold=2) -> bool:
    # Count cards spawned by this objective with demotion_count >= demotion_threshold
    demoted = count_demoted_cards(objective_id, min_demotions=demotion_threshold)
    return demoted >= card_threshold
```

If spin detected: auto-pause the objective, skip LLM assessment for this tick, write a synthetic assessment note explaining the spin, show UI badge.

---

### Phase 5 — Time-box expiry

The scheduler's existing tick loop checks `expires_at` for all active objectives each tick:
```python
expired = db.query(Objective).filter(
    Objective.status == 'active',
    Objective.expires_at <= now()
).all()
for obj in expired:
    update_objective_status(obj.id, 'complete', completed_at=now())
```

---

### Phase 6 — UI *(partially complete)*

**Board card badge** ✅ — cards tagged with an `autopilot_objective_id` show a small ⚡ badge so it's clear they were autopilot-spawned. Implemented in `kanban.js`.

**Objectives panel** ✅ — added as a new section inside the project ⚙ modal (`edit-project-modal`), below the LLM/budget pickers:
- Lists all objectives (active/paused/complete) with status badge, priority, time-box, and last assessment preview (first 120 chars).
- Inline create/edit form (textarea + priority + time-box hours + Save/Cancel).
- Pause ⏸ / Resume ▶ / Edit ✏ / Delete ✕ per-objective action buttons.
- "Appears complete — Confirm ✓ / Dismiss" banner when `appears_complete_since` is set.
- Stuck badge ("🔴 Stuck — review needed") shown when status is paused and assessment mentions spin.

**Project settings** ✅ — Autopilot Budget picker (`edit-project-autopilot-budget-select`) and Max in-flight cards number input (`edit-project-max-in-flight`) added to edit-project-modal and wired into `saveEditProject`.

---

### Phase 7 — Tests

1. **Unit** — `autopilot_tick` suppression logic: saturated board, exhausted budget, no active objectives.
2. **Unit** — spin detector: correct firing threshold.
3. **Unit** — multi-tick completion confirmation: objective only flips on second `appears_complete=True` tick.
4. **Unit** — time-box expiry: `expires_at` set correctly on create, status flips when expired.
5. **Integration** — full tick: objective spawns a card at IDEA, card is tagged with `autopilot_objective_id`.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_autopilot_objectives.py` | New table + `tasks.autopilot_objective_id` + project columns |
| `app/database/models.py` | `AutopilotObjective` model + FK on `Task` |
| `app/database/crud_autopilot.py` | New CRUD module |
| `app/agent/maestro_loop.py` | `autopilot_tick()` + stall/completion trigger hooks |
| `app/main.py` | Objectives API routes + project CRUD update |
| `maestro.ini` | `[autopilot]` section: `max_objectives_per_tick`, `spin_demotion_threshold`, `spin_card_threshold` |
| `app/web/` | Objectives panel UI + card ⚡ badge |
| `app/tests/` | Unit + integration tests |

---

## Acceptance criteria

- [x] Creating an objective and triggering a stall causes Maestro to run an assessment and optionally create IDEA cards tagged with `autopilot_objective_id`.
- [x] Autopilot suppresses itself when `in_flight >= autopilot_max_in_flight` or budget exhausted.
- [x] Spin detector auto-pauses an objective after the configured threshold and surfaces a UI badge.
- [x] Maestro only marks an objective complete on the second consecutive tick where it is confident.
- [x] Time-boxed objectives expire correctly at `expires_at`.
- [x] Autopilot-spawned cards show ⚡ badge on the board.
- [x] All new code passes existing test suite with no regressions.
