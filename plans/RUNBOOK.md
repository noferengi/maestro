# Operational Runbook

Lookup table for the ten most common stuck-card patterns. Each entry maps a symptom → likely cause → fix command.

**Prerequisite:** MCP server connected (`/mcp` → `maestro connected`). All fixes are MCP tool calls unless noted.

---

## Quick Reference

| Symptom | Likely Cause | Fix |
|---|---|---|
| `activity_status: idle` on a running task | Zombie session after server restart | `stop_agent()` → re-dispatch |
| `finish_reason: length` + empty `content_preview` | max_tokens too low for reasoning model | Increase `max_tokens` on LLM config |
| Gate hard-fail "CREATE targets exist" | Stale plan — file created since plan was written | Demote to PLANNING, re-run planning |
| Task stuck in `subdividing` type | Subdivision agent exited without writing children | `set_task_type()` to PLANNING or IDEA |
| `rapid_cycling` monitor flag | PIP resolution failing, task demoting in loop | `stop_agent()` → demote → re-plan |
| Gate soft-fail on interface completeness | Fuzzy match missing in interface_contracts | `patch_planning_fields()` to fix contracts |
| Research job stuck in `running` | Server restarted mid-research | `set_task_type()` reset to pending state |
| `tool_call_storms` monitor flag | Agent in a read loop | `stop_agent()` → demote |
| Budget exhausted mid-run | Budget limit reached | Top up budget or reallocate |
| Orphaned worktree after crash | `prune_orphaned_worktrees` didn't fire | Manual `git worktree prune` |

---

## Detailed Patterns

### 1. Zombie Session — `activity_status: idle`

**Symptom:** `diagnose_task(task_id).activity_status == "idle"` but the task has an open `agent_sessions` row (`ended_at IS NULL`).

**Cause:** Server restarted while the agent loop was running. The session row was never closed; the thread is gone.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ activity_status: "idle"
→ active_sessions: [{agent_type: "MaestroLoop", ended_at: null}]
```

**Fix:**
```
mcp__maestro__stop_agent(task_id)        # closes the orphaned session row
mcp__maestro__set_task_type(task_id, "planning")  # or appropriate stage
```
Then re-trigger the pipeline (e.g., `mcp__maestro__trigger_planning_run(task_id)`).

---

### 2. Token Limit — `finish_reason: length`

**Symptom:** `diagnose_task(task_id).budget_trace[0].finish_reason == "length"` with an empty or missing `content_preview`.

**Cause:** The LLM hit `max_tokens` before completing its response. Common with reasoning models (o1, o3, DeepSeek-R1) that produce long chain-of-thought output. Planning is especially vulnerable — a reasoning model's design output can easily exceed 32k tokens.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ budget_trace[0].finish_reason: "length"
→ budget_trace[0].content_preview: "" (or missing)
```

**Fix:** Increase `max_tokens` on the LLM endpoint. For reasoning models, set to at least 65536 (64k). For coding models, 32768 is usually sufficient.

```
mcp__maestro__get_capacity_status()
→ find the LLM in use
```
Then update the LLM's `max_tokens` via the UI (LLM settings → max_tokens) or the API:
```
PUT /api/llms/{id}  →  {"max_tokens": 65536}
```

**Prevention:** When creating or editing an LLM endpoint, check the model type. Reasoning models need 64k; coding models need 32k; small models (7B) need 8k-16k.

---

### 3. Planning Gate Hard-Fail — "CREATE targets exist"

**Symptom:** Planning gate fails with a hard-fail check like "CREATE targets exist" or "file modified since plan."

**Cause:** The planning pipeline produced a plan referencing specific files. After the plan was written, someone (another task, the user, or the agent itself) created or modified one of those files. The gate detects the divergence and rejects the plan as stale.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ gate_history[0].outcome: "failed"
→ gate_history[0].checks: [{name: "create_targets_exist", passed: false, ...}]
```

Also check if other tasks are active:
```
mcp__maestro__get_scheduler_state()
→ active_sessions: [...tasks currently modifying files...]
```

**Fix:** Demote the task back to PLANNING so it re-surveys the codebase with the current state:
```
mcp__maestro__demote_task(task_id, target_stage="planning")
mcp__maestro__trigger_planning_run(task_id)
```

**Prevention:** Use prerequisite wiring (Theme 1.2) to prevent concurrent modification of the same files. See Theme 2.2 (File Claim Registry) for long-term prevention.

---

### 4. Stuck in Subdividing — No Children Written

**Symptom:** Task `type == "subdividing"` for an extended period. `diagnose_task` shows no child tasks despite the task being in a subdivision phase.

**Cause:** The subdivision agent exited without writing any children. This can happen if the agent encounters an error, runs out of budget, or the server restarts mid-subdivision.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ task.type: "subdividing"
→ budget_trace: [...few entries, last one with error...]
```

Check for children:
```
GET /api/tasks/{id}/children
→ [] (empty)
```

**Fix:** Force the task to a recoverable stage. If the task is a Big Idea that should have children, set it to PLANNING to start the planning pipeline on the parent. If it was meant to subdivide into subtasks, set it to IDEA to restart:
```
mcp__maestro__set_task_type(task_id, "planning")    # parent-level planning
# or
mcp__maestro__set_task_type(task_id, "idea")         # restart subdivision
```

---

### 5. Rapid Cycling — `rapid_cycling` Monitor Flag

**Symptom:** `monitor()` returns `pattern_flags: {rapid_cycling: true}` or `rapid_cycling_tasks: [task_id, ...]`.

**Cause:** A task is cycling through stages rapidly — typically PIP resolution failing, causing a demotion, which triggers re-planning, which fails again. Each cycle burns tokens without progress.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ cycle_counts: {"pip_resolution": N, "planning_pipeline": N, ...}  # high counts
→ correction_sessions: [{exit_reason: "max_turns_exceeded", ...}]
→ gate_history: multiple failures on the same check
```

Check the monitor report for the specific threshold breach:
```
mcp__maestro__monitor(duration_seconds=60)
→ pattern_flags: {rapid_cycling: true, rapid_cycling_tasks: [...]}
```

**Fix:** Stop the agent and demote to break the cycle:
```
mcp__maestro__stop_agent(task_id)
mcp__maestro__demote_task(task_id, target_stage="planning")
```
Then investigate the root cause (see patterns 3 or 6 below).

**Prevention:** The `rapid_cycling` monitor flag itself is the prevention — check `monitor()` reports regularly.

---

### 6. Gate Soft-Fail — Interface Completeness

**Symptom:** Planning gate fails on "interface completeness" (a soft-fail, not hard-fail). The gate's fuzzy match didn't find all expected interfaces in the plan's `interface_contracts`.

**Cause:** The planning agent generated an `interface_contracts` JSON that doesn't match the expected interface signatures. This can happen when the agent misreads an existing API or generates a slightly different signature than what consuming components expect.

**Diagnosis:**
```
mcp__maestro__diagnose_task(task_id)
→ gate_history[0].outcome: "failed" (soft)
→ gate_history[0].checks: [{name: "interface_completeness", passed: false, ...}]

mcp__maestro__get_planning_result(task_id)
→ interface_contracts: { ... }  # inspect for mismatches
```

**Fix:** Patch the `interface_contracts` field in the planning result:
```
mcp__maestro__patch_planning_fields(result_id, {
    "interface_contracts": { /* corrected JSON */ }
})
```
Then re-run the planning gate.

If you're not sure what the correct contracts should be, demote and re-plan:
```
mcp__maestro__demote_task(task_id, target_stage="planning")
mcp__maestro__trigger_planning_run(task_id)
```

---

### 7. Research Job Stuck in `running`

**Symptom:** A research job (intake or planning phase) shows `status == "running"` but no budget entries are being created. The task is idle — no active agent session.

**Cause:** Server restarted mid-research. The `IntakePipeline` or planning research agent was running when the server went down. The job row was never updated to `completed` or `failed`.

**Diagnosis:**
```
GET /api/tasks/{id}/research-jobs
→ [{id: N, status: "running", created_at: "...", ...}]

mcp__maestro__diagnose_task(task_id)
→ activity_status: "idle"
```

**Fix:** Reset the job status via the database. There's no MCP tool for this — use the inspect_cards fallback:
```
venv/Scripts/python.exe -c "
import sqlite3
conn = sqlite3.connect('maestro.db')
conn.execute('UPDATE intake_drafts SET status=\"pending\" WHERE task_id=?', (task_id,))
conn.commit()
conn.close()
"
```
Or restart the server (which triggers `_rescue_stale_jobs()`):
```
mcp__maestro__restart_server()
```

---

### 8. Tool Call Storms — `tool_call_storms` Monitor Flag

**Symptom:** `monitor()` returns `pattern_flags: {tool_call_storms: true}`.

**Cause:** The agent is stuck in a read loop — making many tool calls in quick succession with low token cost per call. Typical pattern: repeatedly calling `read_file` or `find_in_files` without making progress. This burns budget without producing output.

**Diagnosis:**
```
mcp__maestro__monitor(duration_seconds=60)
→ pattern_flags: {tool_call_storms: true, tool_call_storm_tasks: [...]}

mcp__maestro__diagnose_task(task_id)
→ budget_trace: [...many entries with low prompt_cost, tool_calls present...]
→ activity_status: "active — last LLM call at ..." (but no meaningful output)
```

**Fix:** Stop the agent and demote:
```
mcp__maestro__stop_agent(task_id)
mcp__maestro__demote_task(task_id, target_stage="planning")
```
Then re-trigger the appropriate pipeline.

---

### 9. Budget Exhausted Mid-Run

**Symptom:** Task is in an error state with no active session. `diagnose_task` shows the last budget entry was near the budget limit.

**Diagnosis:**
```
mcp__maestro__get_capacity_status()
→ budget summary: {spent: X, limit: Y, remaining: 0 or negative}

mcp__maestro__diagnose_task(task_id)
→ budget_trace: [...entries approaching the budget limit...]
```

**Fix options:**

1. **Top up the budget:** Increase `budget_limit` on the task's budget via the UI or API:
   ```
   PUT /api/budgets/{id}  →  {"limit": new_limit}
   ```

2. **Reallocate to a different budget:** Update the task's `budget_id`:
   ```
   PUT /api/tasks/{id}  →  {"budget_id": new_budget_id}
   ```

3. **Use a cheaper LLM:** Switch to a smaller/faster model with lower token cost.

After fixing the budget, re-dispatch the task.

---

### 10. Orphaned Worktree After Crash

**Symptom:** `.maestro-worktrees/{task_id}/` exists but the git branch `maestro/task-{task_id}` is not checked out. The worktree directory is stale from a crashed run.

**Cause:** `prune_orphaned_worktrees()` didn't fire on server startup (e.g., it was disabled, or the project path wasn't in the known-projects list).

**Diagnosis:**
```
ls .maestro-worktrees/
→ task_id directories that shouldn't be there

git worktree list
→ stale entries pointing to non-existent branches
```

**Fix:** Manual cleanup:
```bash
git worktree prune
# or, for a specific task:
rm -rf .maestro-worktrees/{task_id}/
```

Then restart the server to let `prune_orphaned_worktrees()` run on startup:
```
mcp__maestro__restart_server()
```

**Prevention:** The server calls `prune_orphaned_worktrees()` on startup (in `scheduler.py`). If worktrees accumulate, check that the project paths in `maestro.ini` match the actual project paths.

---

## Emergency Procedures

### Server Won't Start
1. Check `maestro.db` is not locked: `lsof maestro.db` (Linux) or check for `maestro.db-wal` / `maestro.db-shm` files
2. Restart: `mcp__maestro__restart_server()`
3. If that fails, kill and restart manually (bypasses session drain — use as last resort)

### Database Corruption
1. Run `migrate.bat reset` — drops everything, re-migrates, re-seeds
2. This is destructive — only do it if data can be recovered from git worktrees

### Mass Stuck Cards
1. Run `mcp__maestro__monitor(duration_seconds=30)` to get a snapshot
2. Run `mcp__maestro__get_project_health()` for stage counts
3. For each zombie: `stop_agent()` + `set_task_type()` to recoverable stage
4. Restart server to clear all stale sessions and jobs
