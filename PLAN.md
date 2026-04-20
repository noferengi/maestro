# Maestro — Plan

## Context

Three tasks have been stuck in `planning` type across two projects (AndroidStreetPass,
Garden). Root causes identified and two bugs fixed this session (judge `max_tokens` too
low; reviewers running in parallel causing starvation). A third bug — `PlanningCorrectionAgent`
never triggered because all prior sessions died in server restarts before reaching the gate
— is in-flight. Current planning sessions are the first to run with the correction code live.
52 migrations applied, 690 tests passing.

---

## In-flight / immediate

**Three planning sessions are actively running.** They started at 07:13 UTC and are working
through the design cycle. They need to complete and hit the gate (expected ~30-90 min from
session start) before the correction agent path can be tested. Do NOT restart the server
while these are running.

**After they hit the gate:**

1. Check for `planning_correction` agent sessions:
```bash
venv/Scripts/python.exe -c "
import sqlite3
conn = sqlite3.connect('data/kanban.db')
print(conn.execute(\"SELECT task_id, agent_type, exit_reason, exit_summary, started_at FROM agent_sessions WHERE agent_type='planning_correction' ORDER BY id DESC LIMIT 5\").fetchall())
"
```

2. If correction sessions appear with `exit_reason='corrected'` → gate re-runs → task
   should advance to `type='indev'`. Verify via board or:
```bash
venv/Scripts/python.exe scripts/inspect_cards.py scheduler
```

3. If correction sessions appear with `exit_reason='stalled'` → go to Plan item 2.

4. If no correction sessions appear despite gate failures → debug trigger at
   `scheduler.py:3089`; check logs for `[planning_correction]` prefix.

---

## Pending features / fixes

### 1. Fix task descriptions to stop design review failures

These are the fastest fix and unblock the design review immediately.

**Task: SQL Migration - Basic Table Structure**  
The LLM designs a simplified users table that removes `password_hash`, `is_active`,
`last_login_at`. Security reviewer rejects it every time. Fix: edit the task description
via the board UI or directly in DB:

```python
import sqlite3
conn = sqlite3.connect('data/kanban.db')
conn.execute("""
    UPDATE tasks SET description = description ||
    '\n\nIMPORTANT: The existing migration at migrations/001_create_users_table.sql is ' ||
    'authoritative. Preserve ALL existing columns (including password_hash, is_active, ' ||
    'last_login_at). Only add new columns if the task requires them.'
    WHERE id = 'task-1776559187.604922'
""")
conn.commit()
conn.close()
```

**Task: Create Supporting Types (PacketPayload and PacketMetadata)**  
`PacketMetadata.kt` already exists at
`core/models/src/main/java/com/androidstreetpass/core/models/PacketMetadata.kt`. The
interface reviewer flags a scope mismatch every time. Fix: update task description to scope
to PacketPayload only:

```python
conn.execute("""
    UPDATE tasks SET description = description ||
    '\n\nSCOPE NOTE: PacketMetadata already exists at ' ||
    'core/models/src/main/java/com/androidstreetpass/core/models/PacketMetadata.kt. ' ||
    'DO NOT create it again. Focus exclusively on creating PacketPayload.'
    WHERE id = 'task-1776548777.749239'
""")
```

Do these DB edits, then the next planning session will pick up the updated descriptions.

---

### 2. If correction agent stalls: soften `interface_completeness` to soft fail

**Trigger:** `planning_correction` sessions exist but all have `exit_reason='stalled'`, and
gate keeps failing with `interface_completeness`.

**File:** `app/agent/planning_gate.py`

Find the `interface_completeness` check (around line 162-167):
```python
return GateCheck(
    name="interface_completeness",
    passed=False,
    hard_fail=True,                   # ← change this to False
    detail=f"Unresolved consumes: {', '.join(sorted(unresolved))}",
)
```

Change `hard_fail=True` → `hard_fail=False`. This makes it a warning, not a blocker.
The gate will still log the failure and record it in `gate_checks`, but `hard_failures`
list in scheduler will be empty → correction agent won't attempt it → task advances to
INDEV anyway. The INDEV agent can deal with actual missing interfaces at implementation time.

**Note:** This is a trade-off. Keeping it as hard fail is architecturally cleaner — the
correction agent is the intended fix. Only soften if the correction agent consistently
stalls after multiple attempts.

---

### 3. Call `supersede_planning_results` at start of each planning run

**Problem:** Each planning run creates a new `planning_results` row with `status='active'`
but old rows are never superseded. Multiple `status='active'` rows exist per task.
`get_planning_result()` returns the latest by timestamp so logic is correct, but the table
accumulates stale rows.

**File:** `app/agent/scheduler.py`, function `_run_planning_task` (around line 2988)

Add before `run_planning_pipeline(...)`:
```python
from app.database import supersede_planning_results
supersede_planning_results(task_id)
```

This is already defined in `app/database/crud_pipeline.py:225` — just needs to be called.

---

### 4. Arch bar: Populate deduplication

**Problem:** clicking ⚡ Populate twice creates duplicate `arch_gen_jobs` for the same
category.

**File:** `app/main.py`, function `populate_arch()`

After collecting `missing` categories, before creating jobs:
```python
from app.database import SessionLocal
from app.database.models import ArchGenJob
db = SessionLocal()
already_queued = {
    j.category for j in db.query(ArchGenJob)
    .filter(
        ArchGenJob.project == project_name,
        ArchGenJob.status.in_(['pending', 'running']),
    ).all()
}
db.close()
missing = [c for c in missing if c not in already_queued]
if not missing:
    return {"queued": 0, "categories": []}
```

---

### 5. Arch bar: Populate prewarm gate

**Problem:** if no file summaries exist for the project, every arch_gen_job silently fails.

**File:** `app/main.py`, function `populate_arch()`

After resolving `project`, before creating jobs:
```python
from app.database import get_file_summaries_for_project_root
summaries = get_file_summaries_for_project_root(project.path or "")
if not summaries:
    raise HTTPException(
        status_code=409,
        detail=(
            "No file summaries found for this project. "
            "Set a project path and run a prewarm first."
        )
    )
```

---

### 6. Arch bar: Regenerate single arch card

**Goal:** ↻ Regen button on each arch card's hover toolbar replaces it with a freshly
generated one.

**Backend** — `app/main.py`:
```python
@app.post("/api/projects/{project_name}/regen-arch-card")
def regen_arch_card(project_name: str, body: dict):
    category = body.get("category")
    if not category:
        raise HTTPException(status_code=400, detail="category required")
    from app.database import get_all_tasks, delete_task
    tasks = [t for t in get_all_tasks() if t.project == project_name and t.type == 'architecture']
    for t in tasks:
        content = json.loads(t.content or '{}') if isinstance(t.content, str) else (t.content or {})
        if content.get('category') == category:
            delete_task(t.id)
    llm_id, budget_id = _pick_prewarm_resources(project_name)
    from app.database import create_arch_gen_job
    create_arch_gen_job(project_name, category, llm_id=llm_id, budget_id=budget_id)
    return {"queued": 1, "category": category}
```

**Frontend** — `app/web/kanban.js`, in `renderArchBar()` where card toolbar is built, add
a ↻ button that calls `fetch('/api/projects/' + currentProject + '/regen-arch-card',
{method:'POST', body: JSON.stringify({category})})` then calls `loadArchGenJobs()`.

---

## Execution order

1. **NOW:** Wait for current planning sessions to complete and hit the gate (monitor via
   `inspect_cards.py activity --hours 1`).
2. **After gate hit:** Check for `planning_correction` sessions (Step 0 above). If they
   appear and pass → tasks should advance to INDEV. Done.
3. **If stuck:** Apply task description fixes (Plan item 1) to stop design review failures,
   restart sessions.
4. **If correction agent stalls repeatedly:** Soften `interface_completeness` to soft fail
   (Plan item 2).
5. **When planning tasks are flowing:** Apply Plan items 3-6 as quality-of-life improvements.
