# Maestro — Plan

## Context

Project Maestro is a Kanban board with agentic LLM orchestration. The last session added
the Arch Bar Populate feature (⚡ Populate button → queues `arch_gen_jobs` → scheduler
generates missing architecture category cards from file summaries). 36 migrations applied,
server restartable.

---

## In-flight / immediate

**Server restart needed** to pick up:
- `app/agent/arch_gen_agent.py` (new file)
- `app/agent/scheduler.py` (new dispatch step + rescue)
- `app/database/` changes (new model, new CRUD)
- `app/main.py` (new endpoint)
- `app/web/` (new button + JS)

No tasks currently mid-pipeline. DB is clean.

---

## Pending features

### 1. Populate deduplication

**Problem:** clicking ⚡ Populate twice creates duplicate `arch_gen_jobs` for the same
category (both will run and create duplicate cards).

**Fix:** in `app/main.py` `populate_arch()`, before creating jobs, also query for
already-pending/running arch_gen_jobs for this project:

```python
# After collecting `missing` categories:
from app.database import SessionLocal, ArchGenJob
from sqlalchemy import or_
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
```

---

### 2. Populate prewarm gate

**Problem:** if no file summaries exist for the project, every arch_gen_job will fail with
"No file summaries found". User gets no feedback.

**Fix:** in `populate_arch()`, after resolving `project`, check summary count:

```python
from app.database import get_file_summaries_for_project_root
summaries = get_file_summaries_for_project_root(project.path or "")
if not summaries:
    raise HTTPException(
        status_code=409,
        detail="No file summaries found for this project. "
               "Set a project path and run a prewarm first."
    )
```

---

### 3. Verify short_summary population

**Problem:** `arch_gen_agent.py` prefers `short_summary` over full summary, but migration
0035 only added the column — `file_summary_agent.py` may not populate it.

**Check:** `grep -n "short_summary" app/agent/file_summary_agent.py`

**If missing:** in `file_summary_agent.py`, find where `create_file_summary()` is called
and add `short_summary=<parsed_short>`. The `_parse_dual_summary()` function already
extracts both `FULL_SUMMARY:` and `SHORT_SUMMARY:` sections — just pass the short one to
`create_file_summary()`.

---

### 4. Populate progress indicator

**Goal:** show "Generating N…" in the arch bar subtitle while arch_gen_jobs are in flight.

**Backend:** add `GET /api/projects/{name}/arch-gen-jobs` returning:
```json
{"pending": 3, "running": 1, "completed": 8, "failed": 0}
```

```python
@app.get("/api/projects/{project_name}/arch-gen-jobs")
def arch_gen_job_status(project_name: str):
    from app.database import SessionLocal, ArchGenJob
    db = SessionLocal()
    jobs = db.query(ArchGenJob).filter(ArchGenJob.project == project_name).all()
    db.close()
    from collections import Counter
    counts = Counter(j.status for j in jobs)
    return {s: counts.get(s, 0) for s in ('pending', 'running', 'completed', 'failed')}
```

**Frontend:** in `populateArchBar()`, after a successful POST, start polling this endpoint
every 3 s. Update `#arch-bar-subtitle` to "Generating N categories…". Stop when
`pending + running == 0`. `reconcile()` will surface new cards automatically.

---

### 5. Regenerate single arch card

**Goal:** toolbar button on an arch card to replace it with a freshly generated one.

**Flow:** delete the existing card → create a new `arch_gen_job` for that category.

**Backend:** `POST /api/projects/{name}/regen-arch-card` with body `{"category": "Security"}`:
```python
@app.post("/api/projects/{project_name}/regen-arch-card")
def regen_arch_card(project_name: str, body: dict):
    category = body.get("category")
    if not category or category not in _ARCH_CATEGORIES:
        raise HTTPException(status_code=400, detail="Invalid category")
    # Soft-delete existing card for this category
    tasks = get_tasks_by_project(project_name)
    for t in tasks:
        if t.type == 'architecture':
            content = t.content if isinstance(t.content, dict) else {}
            if content.get('category') == category:
                delete_task(t.id)
    llm_id, budget_id = _pick_prewarm_resources(...)
    create_arch_gen_job(project_name, category, llm_id=llm_id, budget_id=budget_id)
    return {"queued": 1, "category": category}
```

**Frontend:** add a ↻ Regen button to each arch card's hover toolbar in `renderArchBar()`.

---

## CLAUDE.md migration list update needed

Add to the migration list in `CLAUDE.md`:
```
- `0035` — `short_summary` column on `file_summaries`
- `0036` — `arch_gen_jobs` table; `category` + `project` fields; scheduler-dispatched arch card generation
```
