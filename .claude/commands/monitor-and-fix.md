---
description: Monitor Maestro for 5 minutes, identify stuck/looping cards, diagnose root causes, fix code, restart server, then loop back.
---

You are running a "Monitor and Fix" loop for Project Maestro. Your job is:
1. Monitor for 5 minutes
2. Identify any cards stuck in loops or not progressing
3. Diagnose root causes in the code
4. Fix the code
5. Restart the server
6. Loop back to step 1

Work autonomously. Don't ask for confirmation unless a fix is ambiguous. Be surgical — fix only what's broken.

---

## Step 1 — Run a monitoring window

Call the MCP monitor tool for a 5-minute window:

```
mcp__maestro__monitor(duration_seconds=300)
```

Read the full report. Note:
- Any pattern flags fired: `rapid_cycling`, `token_limited`, `zombie_sessions`, `stage_thrash`, `tool_call_storms`
- Tasks that haven't advanced stages
- Budget entries with `finish_reason: "length"`
- Sessions that started but never completed

---

## Step 2 — Identify stuck cards

After reading the monitor report, also check:

```
mcp__maestro__find_stuck_tasks(idle_minutes=10)
mcp__maestro__get_scheduler_state()
```

For each stuck or looping card, run:

```
mcp__maestro__diagnose_task(task_id="<id>")
```

Build a list of distinct failure modes. Group cards by root cause, not by symptom. Common patterns to look for:

| Pattern | Signal |
|---|---|
| Zombie session | `activity_status: "idle"` with `ended_at=None` in agent_sessions |
| max_tokens too low | `finish_reason: "length"` + empty content, non-empty reasoning_content |
| Gate loop | `correction_attempts > 0` + gate keeps failing same check |
| Interface completeness loop | Gate fails `interface_completeness` repeatedly |
| Tool call storm | Rapid small budget entries (< 700 prompt_cost) cycling every 30s |
| Branch/dispatch stall | Task has `type=indev` or later but no active session and no budget entries |

If nothing is stuck and all cards are progressing normally, skip to the end and report status. Do NOT restart a healthy server.

---

## Step 3 — Research the root cause in code

For each distinct failure mode, search the relevant source file(s):

- Gate failures → `app/agent/planning_gate.py`, `app/agent/planning.py`
- Zombie sessions → `app/agent/scheduler.py` (look for `_active_sessions`, alive-check logic)
- Token limits → `maestro.ini` (raise `*_max_tokens` values), `app/agent/llm_client.py`
- Tool call storms → `app/agent/loop.py`, `app/agent/verdicts.py`
- Interface completeness → `app/agent/planning_correction.py`, `app/agent/planning_gate.py`
- Stage dispatch stalls → `app/agent/scheduler.py` (`SCHEDULER_DISPATCHABLE_TYPES`, `_dispatch_*` methods)

Read the relevant code sections. Understand the exact line that causes the failure before writing any fix.

---

## Step 4 — Fix the code

Apply targeted fixes:
- Edit only the lines that cause the failure. No refactoring.
- For `maestro.ini` token limit increases: raise the relevant `*_max_tokens` value.
- For gate logic bugs: fix the predicate, not the caller.
- For zombie recovery: ensure the scheduler's alive-check correctly detects dead threads.
- After each edit, re-read the changed section to confirm correctness.

Do NOT:
- Restart the server mid-fix (wait until all fixes are applied)
- Change unrelated code
- Add comments explaining the fix
- Introduce new abstractions

---

## Step 5 — Restart the server

Once all code changes are applied, restart the Maestro server so the new code takes effect:

```
mcp__maestro__restart_server()
```

Wait for confirmation that the server restarted. Then verify the scheduler is running:

```
mcp__maestro__get_scheduler_api_status()
```

---

## Step 6 — Re-dispatch any zombie tasks

After restart, check if previously-zombie tasks are now dispatched:

```
mcp__maestro__get_scheduler_state()
```

If any tasks are still idle that should be active, use:

```
mcp__maestro__run_pipeline_stage(task_id="<id>", stage="<stage>")
```

or for planning tasks:

```
mcp__maestro__trigger_planning_run(task_id="<id>")
```

---

## Step 7 — Report and loop

Print a concise summary:
- What patterns were found
- What files were changed (file:line)
- Whether the server was restarted
- Current state of previously-stuck cards

Then immediately start the next monitoring window from **Step 1** again. Keep looping until the user says to stop.

If no issues were found in a window, still loop — report "all clear" and start the next window.
