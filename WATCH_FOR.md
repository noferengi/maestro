# WATCH_FOR.md
## Context handoff — what was wrong, what was fixed, what to watch next

This file exists so a fresh session (after `/clear`) can resume monitoring without
re-deriving context from scratch. Update it after each significant intervention.

---

## What was wrong (session ending ~2026-05-07)

### Root causes found and fixed

| # | Location | Symptom | Fix applied |
|---|----------|---------|-------------|
| 1 | DB `tasks.type` (5 EasyProject tasks) | Tasks stuck in `full_review` for 11 days — completely invisible to scheduler because `SCHEDULER_DISPATCHABLE_TYPES` only lists `final_review` | `set_task_type` → `final_review` on all 5; migration 0059 added to backfill any remaining stragglers |
| 2 | `app/migrations/versions/0056_rename_full_review_to_final_review.py` | Migration renamed the results TABLE but never ran `UPDATE tasks SET type = 'final_review' WHERE type = 'full_review'` — so tasks that passed security before the code change were permanently stranded | Added the `UPDATE` to 0056's `up()`/`down()`; created migration 0059 to apply the fix to current DB |
| 3 | `scripts/inspect_cards.py` lines 34–36 | Script's hardcoded `DISPATCHABLE` set was missing `security` and `final_review`; `security` was wrongly in `NEVER_DISPATCH`; `non_dispatchable` bucket was silently dropped with no print | Updated `DISPATCHABLE` to match real scheduler; removed `security` from `NEVER_DISPATCH`; added `[!] NON-DISPATCHABLE TYPE` print block so stranded tasks surface |

### What was NOT a bug (healthy / self-resolving)

- **Survey task** (`task-1777972562.612366`, "Survey the Architecture System", INDEV): zombie-killed by repeated server restarts. Marked READY by DAG — will auto-dispatch once LLM 1 has capacity. 3 tasks unblock when it completes.
- **6 StoryOrchestrator planning tasks**: all READY, waiting on LLM 1 slots held by 3 active intake agents.
- **$0 budget spend in health report**: tasks not linked to budget IDs — spend tracking is blank, but LLM calls are recording correctly in `budget_entries`.

---

## The recurring pattern to watch: stage-rename stranding

When a pipeline stage is renamed in code (e.g. `full_review` → `final_review`), tasks
already in the DB retain the old type string. The scheduler silently skips them because
the old string isn't in `SCHEDULER_DISPATCHABLE_TYPES`. They show no error — they just
never get dispatched. This can go undetected for weeks.

**How to spot it:**
```
# Check for any task types that aren't in the dispatchable list
venv/Scripts/python.exe scripts/inspect_cards.py scheduler
# Look for [!] NON-DISPATCHABLE TYPE section at the bottom
```

**How to fix it:**
1. `mcp__maestro__set_task_type(task_id, "correct_type")` for each affected task
2. Write a migration that does `UPDATE tasks SET type = 'new_name' WHERE type = 'old_name'`
3. Make sure future stage-rename migrations always include the tasks UPDATE — not just table/column renames

---

## Current system state (as of this session)

### Active LLM topology
- LLM 1: `Qwen3p6-35B-A3B-Q4` on `localhost:8008`, 3 parallel sessions — **primary workhorse**
- LLM 45: `Qwen3CoderNextBatch`, 4 sessions free
- LLM 46: `Qwen3p5-Omnicoder-9B-BATCH`, 5 sessions free
- All tasks currently assigned to LLM 1 — LLMs 45/46 are idle

### Pipeline stage counts (at session end)
- `idea`: 14 (many READY, queued behind LLM 1 capacity)
- `planning`: 6 (all READY, StoryOrchestrator)
- `indev`: 1 (Survey task, READY, idle zombie)
- `final_review`: 5 (EasyProject — now visible to scheduler after fix)
- `completed`: 5
- `architecture`: 6

### Pending merges: 5 (completed tasks awaiting human "Accept & Merge")

---

## How to diagnose if tasks are stuck

### Quick triage (run in order)

```
# 1. Overall health
mcp__maestro__get_project_health()

# 2. Scheduler view — shows READY / BLOCKED / NON-DISPATCHABLE
venv/Scripts/python.exe scripts/inspect_cards.py scheduler

# 3. Find sessions with no LLM activity in 15+ minutes
mcp__maestro__find_stuck_tasks(idle_minutes=15)

# 4. Full picture for a specific task
mcp__maestro__diagnose_task(task_id="<id>")
```

### Key signals in `diagnose_task` output

| Signal | What it means | Action |
|--------|--------------|--------|
| `activity_status: "idle"` | Zombie session — server restarted mid-run | Scheduler auto-recovers; wait one tick or check capacity |
| `budget_trace[0].finish_reason == "length"` | `max_tokens` too low | Increase max_tokens on the LLM endpoint |
| Task missing from `inspect_cards scheduler` output | Type not in `DISPATCHABLE` | Check `[!] NON-DISPATCHABLE TYPE` section; use `set_task_type` to fix |
| Planning gate failing repeatedly | `implementation_steps_present` hard-fail | Check `get_gate_history`; use `patch_planning_fields` to fix the plan |
| `correction_attempts > 0` | PlanningCorrectionAgent ran; check `exit_reason` | May need manual plan fix via `patch_planning_fields` |
| `exit_reason: "error"` on dev_orchestrator | Unexpected exception; task stays INDEV | Scheduler auto-retries; check budget trace for tool errors |

### What a stage-rename migration must include

Every time a `tasks.type` string is renamed:
```sql
-- In up():
UPDATE tasks SET type = 'new_name' WHERE type = 'old_name';

-- In down():
UPDATE tasks SET type = 'old_name' WHERE type = 'new_name';
```

If this is forgotten, run:
```
venv/Scripts/python.exe scripts/create_migration.py "backfill X task types to Y"
# Edit the scaffolded file, then:
venv/Scripts/python.exe app/migrations/runner.py migrate
```

---

## Remaining known issues (not fixed in this session)

- **SQLite lock contention**: `mcp_tools/helpers.py` and `app/database/session.py` have no
  WAL mode or lock timeout configured. MCP tool calls can silently hang when the scheduler
  holds a write lock. Retry after a few seconds, or use `restart_server()` to drain.

- **Merge test race**: two concurrent task completions modifying the same file → second
  "Accept & Merge" hits a conflict and demotes back to `human_review`. Handled correctly
  but requires manual re-review.

- **LLMs 45/46 underutilised**: all tasks are pinned to LLM 1. With 9 free slots on 45/46,
  there's latent capacity. No fix attempted — task LLM assignment is a UI/config concern.

- **SQLite lock contention**: `mcp_tools/helpers.py` and `app/database/session.py` have no
  WAL mode or lock timeout configured. MCP tool calls can silently hang when the scheduler
  holds a write lock. Retry after a few seconds, or use `restart_server()` to drain.

- **Merge test race**: two concurrent task completions modifying the same file → second
  "Accept & Merge" hits a conflict and demotes back to `human_review`. Handled correctly
  but requires manual re-review.

- **LLMs 45/46 underutilised**: all tasks are pinned to LLM 1. With 9 free slots on 45/46,
  there's latent capacity. No fix attempted — task LLM assignment is a UI/config concern.

---

## Files changed in this session

```
maestro.ini                          — added submit_work to research_agent_tools (CRITICAL BUG FIX)
app/agent/config.py                  — turn warning at ≤5 now says CRITICAL + explicit REVERT_TO_DESIGN guidance
app/migrations/versions/0056_rename_full_review_to_final_review.py  — added UPDATE tasks to up()/down()
app/migrations/versions/0059_backfill_full_review_task_types_to_final_review.py  — new migration, applied
scripts/inspect_cards.py             — DISPATCHABLE/NEVER_DISPATCH corrected; non_dispatchable bucket now printed
```

---

## Research Agent submit-loop (FIXED — 2026-05-07)

**Root cause**: `maestro.ini` `research_agent_tools` override listed read/search/web tools but
omitted `submit_work`. The config.py default includes it, but the ini override wins and strips
it. The Research Agent's system prompt tells it to "call submit_work to finish", but that tool
was never in the schema the LLM sees — so it looped forever calling other tools instead.

**Signal in budget trace**: many consecutive entries with finish_reason="tool_calls" and
reasoning that says "I've exhausted my search budget, submitting now" but never stops.

**Fix**: added `submit_work` to `research_agent_tools` in `maestro.ini` line 164.

**Takes effect**: on next server restart — running intake sessions keep the old schema.

**If you see this recur**: check `maestro.ini [intake] research_agent_tools` and confirm
`submit_work` is in the list. Also check `[subdivision] subdivision_agent_tools` for the
same omission.
