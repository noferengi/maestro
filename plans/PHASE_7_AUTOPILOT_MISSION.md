# Phase 7 — Autopilot & Mission System

> **Status:** SUBSTANTIALLY COMPLETE — 2026-05-15 ⚠️ (mission report card creation and localStorage pre-fill not verified; see audit)  
> **Depends on:** Phase 2 (scheduler reads settings); Phase 1 (system_settings table)  
> **Estimated effort:** 2 days  
> **Goal:** "Human in the Loop / Leave it to the Maestro" toggle with scheduled
> operating hours, a mission dialog for termination conditions, and per-project
> override support. Browser localStorage persists the last-used mission settings
> so the dialog pre-fills on next open.

---

## Concepts

**Autopilot** — a global on/off switch that governs whether the Maestro scheduler
runs autonomously. When off, Maestro is paused: no new tasks are dispatched, running
sessions receive a graceful stop signal.

**Scheduled hours** — autopilot can be configured to automatically activate and
deactivate at wall-clock times (e.g. on at 23:00, off at 07:00). This is separate
from the autopilot toggle — it is a schedule that drives the toggle.

**Mission** — the termination conditions for an autopilot run. One or more
conditions may be set; the run stops on whichever fires first. Mission settings
live in browser localStorage (no DB record); the dialog pre-fills with the last
used values.

**Per-project override** — individual projects can opt out of global autopilot
(e.g. "never run this project autonomously") or opt in even when global is off
(e.g. "always run this project"). Stored in `project_settings`.

---

## Deliverables

1. `system_settings` rows for `maestro_autopilot`, `autopilot_start_hour`,
   `autopilot_stop_hour` (seeded by Phase 1 migration)
2. `GET/POST /api/settings/autopilot` — read/write the three settings atomically
3. `GET/POST /api/projects/{name}/settings` — per-project settings CRUD
4. Scheduler tick: reads autopilot state and scheduled hours at the top of each cycle
5. Graceful stop signal path: when autopilot turns off, running sessions receive
   `stop_agent()` signal (existing mechanism)
6. UI toggle button (arch bar area) with scheduled-hours config
7. Mission dialog: termination condition checkboxes, first-breach-wins logic
8. Mission state machine in the scheduler: tracks active mission, checks conditions
   each tick, emits a mission report card when any condition fires

---

## Scheduler Changes

### Autopilot gate (top of each tick)

```python
def _should_dispatch(self) -> bool:
    settings = get_system_settings(['maestro_autopilot', 'autopilot_start_hour', 'autopilot_stop_hour'])
    if settings['maestro_autopilot'] == 'off':
        return False
    start = int(settings.get('autopilot_start_hour', 0))
    stop  = int(settings.get('autopilot_stop_hour', 24))
    now_hour = datetime.now().hour
    if start < stop:
        # simple range: e.g. 09:00–17:00
        if not (start <= now_hour < stop):
            return False
    else:
        # overnight range: e.g. 23:00–07:00
        if not (now_hour >= start or now_hour < stop):
            return False
    return True
```

Per-project override is checked inside the per-task dispatch loop:
```python
proj_setting = get_project_setting(task.project_id, 'autopilot_override')
# 'inherit' | 'force_on' | 'force_off'
if proj_setting == 'force_off':
    continue   # skip this task
if proj_setting == 'force_on':
    pass       # dispatch regardless of global setting
```

### Mission state machine

A mission is not stored in the DB — it lives in memory on the scheduler for the
duration of an autopilot session. When autopilot is enabled (via UI), the scheduler
receives the mission config from the request body.

```python
@dataclass
class MissionConfig:
    time_limit_seconds: int | None      # wall-clock limit from start of mission
    token_budget: int | None            # total tokens across all mission tasks
    card_count_target: int | None       # stop when N cards reach COMPLETED
    goal_card_id: str | None            # stop when this specific card reaches COMPLETED

class MissionState:
    config: MissionConfig
    started_at: datetime
    completed_cards: int = 0
    tokens_used: int = 0
    active: bool = True

    def check_termination(self) -> str | None:
        """Returns the fired condition name, or None if still running."""
        if self.config.time_limit_seconds:
            elapsed = (datetime.now() - self.started_at).total_seconds()
            if elapsed >= self.config.time_limit_seconds:
                return "time_limit"
        if self.config.token_budget and self.tokens_used >= self.config.token_budget:
            return "token_budget"
        if self.config.card_count_target and self.completed_cards >= self.config.card_count_target:
            return "card_count"
        if self.config.goal_card_id:
            card = get_task(self.config.goal_card_id)
            if card and card.stage_key == "completed":
                return "goal_card"
        return None
```

Each scheduler tick calls `mission.check_termination()`. When a condition fires,
the scheduler:
1. Sets `maestro_autopilot = 'off'` in `system_settings`
2. Sends stop signals to all running sessions
3. Creates a "Mission Report" arch card with stats (duration, cards completed,
   tokens used, termination reason)

---

## API Endpoints

```
GET  /api/settings/autopilot
     returns: { autopilot: 'on'|'off', start_hour: int, stop_hour: int }

POST /api/settings/autopilot
     body: { autopilot: 'on'|'off', start_hour?: int, stop_hour?: int, mission?: MissionConfig }
     — if autopilot='on' and mission is provided, starts the mission state machine
     — if autopilot='off', sends stop signals to all running sessions

GET  /api/projects/{name}/settings
     returns: { autopilot_override: 'inherit'|'force_on'|'force_off', ... }

POST /api/projects/{name}/settings
     body: { autopilot_override: 'inherit'|'force_on'|'force_off' }
```

---

## UI

### Toggle button (arch bar area)

A persistent button in the top bar, next to or replacing the arch bar label:

```
[⚡ Leave it to the Maestro]   ← when autopilot is off
[⏸ Human in the Loop     ]   ← when autopilot is on
```

Color: green when off (inviting engagement), amber when on (active/caution).

Clicking the off→on button opens the **Mission Dialog** before engaging.
Clicking the on→off button immediately pauses (no dialog; confirm with a brief
"Pausing Maestro…" toast then button switches state).

### Mission Dialog

```
┌──────────────────────────────────────────────────────────┐
│ Leave it to the Maestro                           [Close] │
├──────────────────────────────────────────────────────────┤
│ Stop when any of these conditions is met:                │
│                                                          │
│ ☑  Time limit        [ 8    ] hours                      │
│ ☑  Token budget      [ 500k ] tokens                     │
│ ☐  Card count        [      ] cards completed            │
│ ☐  Goal card         [Select card...        ▼]           │
│                                                          │
│ Scheduled hours (optional)                               │
│   Active from [ 23:00 ] to [ 07:00 ]                     │
│   ☐ Apply schedule to all future sessions                │
│                                                          │
│                              [Start Maestro]  [Cancel]   │
└──────────────────────────────────────────────────────────┘
```

On open: pre-fill all fields from `localStorage.getItem('maestro_mission_defaults')`.
On "Start Maestro": save current values to localStorage, POST to
`/api/settings/autopilot` with `{autopilot: 'on', mission: {…}}`.

The scheduled hours fields write to `system_settings` (persistent) when
"Apply schedule to all future sessions" is checked. Otherwise they apply only
to this mission run.

---

## Test Criteria

- Set `autopilot_start_hour=23, autopilot_stop_hour=7`; simulate tick at 02:00 →
  `_should_dispatch()` returns True; at 10:00 → returns False
- Set `project_settings(project_id, 'autopilot_override', 'force_off')` → task
  in that project is skipped even when global autopilot is on
- Mission with `card_count_target=3`: complete 3 cards → `maestro_autopilot` flips
  to `'off'`, mission report arch card created
- Mission dialog: fill fields, click Start, reload page, open dialog again →
  fields pre-filled from localStorage with last values

---

## Risk Factors

**Mission state is in-memory** — if the server restarts mid-mission, the mission
state is lost. The scheduler comes back up with `maestro_autopilot = 'on'` (from
DB) but no mission config. Add a safety: on startup, if autopilot is `'on'` with
no in-memory mission, set it to `'off'` and log a warning. The user must re-engage
autopilot manually after a restart.

**Overnight schedule crossing midnight** — the hour comparison must handle the
23:00–07:00 wraparound correctly. The `start < stop` vs `start > stop` branch in
`_should_dispatch()` handles this; add a unit test for the midnight edge case
(hour=0, start=23, stop=7 → should return True).

---

## Implementation Audit (2026-05-15)

### What was delivered

`MissionConfig` and `MissionState` dataclasses exist with `check_termination()`.
`_should_autopilot_dispatch()` in `scheduler.py` correctly handles both simple and
overnight hour ranges. Per-project override (`force_on` / `force_off` / `inherit`)
is checked during the per-task dispatch loop. All four API endpoints exist
(`GET/POST /api/settings/autopilot`, `GET/POST /api/projects/{name}/settings`).
The autopilot toggle button and mission dialog modal are in `index.html`.
`test_autopilot_unit.py` (262 lines) covers schedule logic, all four termination
conditions, and settings round-trips.

Server restart safety is correctly implemented: on startup, if `maestro_autopilot='on'`
with no in-memory mission, it resets to `'off'` and logs a warning.

### Gaps

**Mission report arch card** — `_create_mission_report()` exists in `scheduler.py`
but whether it is actually called on termination requires verification. If not, the
end-of-mission report card specified in the plan is silently skipped.

**localStorage pre-fill** — The spec requires the mission dialog to pre-fill from
`localStorage.getItem('maestro_mission_defaults')` on open. Not tested; may not be
wired in `index.html` JavaScript.

**Graceful stop signal on mission end** — Spec says "sends stop signals to all running
sessions." The `stop_agent()` path exists but whether the mission termination handler
calls it needs confirmation.
