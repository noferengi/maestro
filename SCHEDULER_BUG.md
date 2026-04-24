# Scheduler Bug: The "Demotion Trap"

## Description
A logic error in the scheduler's task dispatch loop causes tasks that have been demoted back to the `idea` stage (either manually or by the planning circuit breaker) to become permanently stuck.

## Technical Details
In `app/agent/scheduler.py`, the scheduler checks if an `idea` task should be dispatched for intake. It explicitly skips any task that has a previous "successful" intake result:

```python
# app/agent/scheduler.py (Lines ~925-933)
existing = get_transition_results(task_id, transition="idea_to_planning")
if existing:
    latest_outcome = existing[0].outcome
    if latest_outcome in ("passed", "subdivide"):
        continue  # already handled - don't re-run intake
```

### Why this is a bug:
When a task is demoted from `planning` or `indev` back to `idea`, it retains its history in the `transition_results` table. The scheduler sees the previous `passed` or `subdivide` outcome and concludes the task has "already been handled," skipping it indefinitely.

## Impact
- **Circuit Breaker Failure**: Tasks demoted by the planning circuit breaker (after 5 rejections) appear in the `idea` column but never re-enter the pipeline.
- **Manual Demotion**: Users who move a task back to `idea` to refine the description find the task "dead" and unresponsive to the autonomous agent.

## Current Workaround
Manually trigger the intake pipeline via the API or UI:
`POST /api/tasks/{task_id}/advance`

This bypasses the scheduler's skip-logic and forces a new intake session.

## Proposed Fix
The logic should check if the task has been demoted since its last intake. A simple fix would be to modify the check in `scheduler.py` to only skip if the task is **not** currently in the `idea` stage, or more accurately, to check if a new intake is required because the task is in the `idea` stage regardless of history (unless it's in a rejection cooldown).

```python
# Proposed logic
if task_type == "idea":
    # ... existing exhausted check ...
    
    # Only skip if a session is ALREADY running (checked later)
    # or if we are in a REJECTION cooldown.
    if task_id in _rejection_cooldowns:
        if time.time() - _rejection_cooldowns[task_id] < _REJECTION_RETRY_COOLDOWN:
            continue
    
    # Remove the check for previous 'passed'/'subdivide' outcomes
    # because if it's currently an IDEA, it needs to get out.
```
