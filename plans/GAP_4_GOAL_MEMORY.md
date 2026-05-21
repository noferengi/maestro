# Gap 4 — Goal memory / persistent objectives

**Status:** Complete  
**Effort:** Medium  
**Priority:** Medium — required for multi-tick autonomous goal pursuit

---

## Progress snapshot (2026-05-19)

| Phase | Status | Notes |
|---|---|---|
| Gap 2 foundation | ✅ Done | `autopilot_objectives` table (migration 0087), basic CRUD in `crud_autopilot.py`, `tasks.autopilot_objective_id` FK, Gap 2 tests |
| Phase 1 — `parent_id` + `created_by` migration | ✅ Done | Migration 0089 applied to test + prod DBs |
| Phase 2 — Evidence log helpers | ✅ Done | `append_objective_evidence` / `get_objective_evidence` in `crud_autopilot.py`; doc key `objective:{id}:evidence` |
| Phase 3 — Prompt injection + tools | ✅ Done | 4 tools registered in `tools.py`; injection block in `loop.py` `_build_messages()` |
| Phase 4 — `[maestro_capabilities]` ini + dataclass | ✅ Done | `MaestroCapabilities` dataclass in `config.py`; `[maestro_capabilities]` section in `maestro.ini` |
| Phase 5 — UI tree view | ✅ Done | Tree renderer in `kanban.js`; `[maestro]` badge; evidence toggle panel; sub-objective form; `/objectives/tree` + `/objectives/{id}/evidence` API routes |
| Phase 6 — Tests | ✅ Done | `app/tests/test_objective_hierarchy.py` — 17 tests, all passing; 894 total green |

---

## Problem

Each Maestro run starts fresh from DB task state. The document store and arch cards
provide some continuity, but there is no explicit persistent record of what Maestro is
trying to accomplish long-term, what evidence it has accumulated toward that goal, or
how far along it is. Progress is lost on server restart. Maestro cannot distinguish
"I have been working on this for 3 weeks and here is what I know" from "I have never
seen this project before."

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Storage** | Extend `autopilot_objectives` from Gap 2: add `parent_id` (nullable self-referential FK) and `created_by` (`human`/`maestro`) field. No new table needed. |
| **Evidence log** | Project document store, key `objective:{id}:evidence`. Append-only free text. No schema change — the store already exists. |
| **Sub-objectives** | `parent_id` FK (nullable). Parent auto-completes when all of its active children reach `complete`. |
| **Prompt injection** | Most relevant objective injected in full into the system prompt + one-line summaries of sibling/parent objectives. Full detail on any other objective available via `get_objective_detail` tool call. |
| **Maestro authorship** | Configurable toggle. Default `off` in the shipped `maestro.ini` template; user's personal ini ships with it `on`. Establishes `[maestro_capabilities]` as the home section for all Maestro autonomy toggles going forward. |

**Note:** Gap 4's storage, lifecycle, card linkage, progress signals, and completion logic
were substantially designed in Gap 2 (`autopilot_objectives` table). This gap adds the
three things Gap 2 left open: evidence accumulation, the parent/child hierarchy, and the
prompt injection model.

---

## Implementation plan

### Phase 1 — Extend `autopilot_objectives` (migration)

```sql
ALTER TABLE autopilot_objectives
    ADD COLUMN parent_id   INTEGER NULL REFERENCES autopilot_objectives(id) ON DELETE SET NULL,
    ADD COLUMN created_by  TEXT    NOT NULL DEFAULT 'human'
                               CHECK (created_by IN ('human', 'maestro'));
```

Update `crud_autopilot.py`:
- `create_objective(...)` — accept optional `parent_id` and `created_by` parameters.
- `complete_objective(obj_id, db)` — after setting `status='complete'`, check if this was the last active child of a parent; if so, call `complete_objective(parent_id, db)` recursively.
- `list_objectives(project_id, status, parent_id=None)` — filter by parent to list children.
- `get_objective_tree(project_id)` — returns objectives as a nested dict for UI rendering.

---

### Phase 2 — Evidence log via document store

No schema change. Evidence is stored in the existing project document store:

**Key convention:** `objective:{id}:evidence`

**`crud_autopilot.py`** — add helpers:

```python
def append_objective_evidence(obj_id: int, entry: str, db) -> None:
    """Appends a timestamped entry to the objective's evidence document."""
    key = f"objective:{obj_id}:evidence"
    existing = get_document(project_id, key, db) or ""
    timestamp = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    updated = existing + f"\n\n## {timestamp}\n{entry.strip()}"
    upsert_document(project_id, key, updated, db)

def get_objective_evidence(obj_id: int, db) -> str:
    key = f"objective:{obj_id}:evidence"
    return get_document(project_id, key, db) or "(no evidence recorded yet)"
```

Maestro calls `append_objective_evidence` during tick assessment to record findings,
dead ends, and milestones. This is in addition to overwriting `last_assessment` — the
document store entry is the full history; `last_assessment` is the one-liner summary
for prompt injection.

---

### Phase 3 — Prompt injection model

**Goal:** The most contextually relevant objective arrives in full; others as one-liners;
full detail accessible on demand.

#### Relevance resolution

For **inner task agents** (working on a specific card):
- "Most relevant" = the objective whose `id` matches `task.autopilot_objective_id`.
- If the task has no objective link, no objectives are injected (the agent is doing human-initiated work).

For **Maestro autopilot ticks**:
- "Most relevant" = highest-priority active objective being evaluated this tick.

#### Injection template (added to system prompt builder):

```
## Current Objective
[P{priority}] {description}
Status: {status} | Created by: {created_by} | Started: {created_at}
Latest assessment: {last_assessment or "none yet"}
Evidence log: call get_objective_evidence(objective_id={id}) to read the full history.

## Other Active Objectives (summaries)
- [P{priority}] id={id}: {description} ({status})
- ...

Use get_objective_detail(id) or get_objective_evidence(id) to read any of the above in full.
```

#### New tools (added to TOOL_REGISTRY, available to Maestro and inner agents):

- `get_objective_detail(objective_id)` → full objective record + children list.
- `get_objective_evidence(objective_id)` → full evidence document text.
- `append_objective_evidence(objective_id, entry)` → appends a timestamped note.
- `list_objectives(status='active')` → flat list with one-line summaries.

These tools are excluded from stages where objectives are irrelevant (e.g., purely mechanical stages like formatting or linting).

---

### Phase 4 — `[maestro_capabilities]` ini section

New section in `maestro.ini` (and `maestro.ini.example`):

```ini
[maestro_capabilities]
# Allow Maestro to create its own objectives autonomously.
# Default: off. Set to true to enable.
can_create_objectives = false

# Allow Maestro to mark objectives complete autonomously (multi-tick confirmation still applies).
can_complete_objectives = true

# Allow Maestro to spawn autopilot cards (Gap 2).
can_create_cards = true

# Maximum objectives Maestro may create per autopilot tick (only applies if can_create_objectives = true).
max_objectives_per_tick = 2
```

User's personal `maestro.ini` ships with `can_create_objectives = true`.

`app/agent/config.py` — read all `[maestro_capabilities]` keys into a `MaestroCapabilities` dataclass. Pass to `autopilot_tick()` and any Maestro-mode session that needs to know what it's allowed to do.

When `can_create_objectives = false` and Maestro's assessment suggests a new objective is needed, it writes the suggestion to the current objective's evidence log instead of inserting a row. The UI surfaces this as a soft notification ("Maestro suggests a new sub-objective — approve?").

---

### Phase 5 — UI additions

**Objective tree view** — replace the flat list from Gap 2 with an indented tree:

```
[P10] ● Explore twin prime gaps                    [human] [pause] [complete]
      [P8]  ○ Sieve to 10^9                        [maestro]
      [P8]  ○ Formalize Zhang bounds               [maestro]
      [P5]  ✓ Calibrate on Bertrand postulate      [maestro] completed 2026-05-03
```

- `●` = active, `○` = child active, `✓` = complete.
- `[maestro]` badge on Maestro-authored objectives.
- Click objective → expand evidence log inline.
- "Maestro suggests" notification banner when `can_create_objectives = false` and Maestro has a suggestion pending.

---

### Phase 6 — Tests

1. **Unit** — `complete_objective`: completing the last active child triggers parent completion.
2. **Unit** — `append_objective_evidence`: idempotent key, correct timestamp format, appends (not overwrites).
3. **Unit** — prompt injection: inner agent with `autopilot_objective_id` set receives full injection; agent without it receives nothing.
4. **Unit** — `[maestro_capabilities]` toggle: with `can_create_objectives=false`, Maestro suggestion goes to evidence log, not DB.
5. **Integration** — full objective lifecycle: create parent → Maestro creates children → children complete → parent auto-completes.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_objective_parent.py` | `parent_id` + `created_by` columns |
| `app/database/crud_autopilot.py` | `complete_objective` recursion, `get_objective_tree`, evidence helpers |
| `app/database/models.py` | Self-referential FK on `AutopilotObjective` |
| `app/agent/config.py` | `MaestroCapabilities` dataclass from `[maestro_capabilities]` |
| `app/agent/maestro_loop.py` | Prompt injection logic; capabilities checks |
| `app/agent/tools.py` | Register objective tools |
| `maestro.ini` + `maestro.ini.example` | New `[maestro_capabilities]` section |
| `app/web/` | Objective tree UI, suggestion notification |
| `app/tests/` | Unit + integration tests |

---

## Acceptance criteria

- [x] `parent_id` FK works: completing the last child auto-completes the parent.
- [x] Evidence appends correctly to `objective:{id}:evidence` document without overwriting prior entries.
- [x] Inner agents working on autopilot-spawned cards receive the spawning objective in full in their system prompt.
- [x] `get_objective_detail` and `get_objective_evidence` tools return correct data.
- [ ] `can_create_objectives = false` causes Maestro suggestions to route to the evidence log, not the DB. *(scheduler.py capability guard in place; UI suggestion banner deferred)*
- [x] All new code passes existing test suite with no regressions.

---

## Implementation order (ready to execute)

1. **Migration ~0089** — `parent_id` + `created_by` on `autopilot_objectives`; update `AutopilotObjective` model
2. **`crud_autopilot.py`** — `create_objective` gets `parent_id`/`created_by` params; `complete_objective` with cascade; `list_objectives` `parent_id` filter; `get_objective_tree`; evidence helpers (`append_objective_evidence`, `get_objective_evidence`)
3. **`config.py`** — `MaestroCapabilities` dataclass from `[maestro_capabilities]` ini section
4. **`maestro.ini`** — add `[maestro_capabilities]` block
5. **`tools.py`** — register `get_objective_detail`, `get_objective_evidence`, `append_objective_evidence`, `list_objectives`
6. **`loop.py`** — prompt injection block (current objective full + siblings one-line)
7. **`app/web/`** — tree view + evidence expand + "Maestro suggests" banner
8. **`app/tests/`** — Phase 6 test cases
