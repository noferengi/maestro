# WATCH_FOR.md
## Context handoff — what was wrong, what was fixed, what to watch next

This file exists so a fresh session (after `/clear`) can resume monitoring without
re-deriving context from scratch. Update it after each significant intervention.

---

## Most recent session summary (2026-05-07, ~08:00 UTC)

Two things happened this session:

1. **Shell injection hardening** — all `shell=True` subprocess calls in the agent
   tool layer were eliminated. See "Security hardening" section below for details.
   Commit: `b322085`.

2. **Snapshot taken** immediately after server restart to leave a clean handoff.
   See "Current system state" below.

---

## CRITICAL THINGS TO CHECK FIRST (in order)

### 1. Did the 5 EasyProject `final_review` tasks start running?

These 5 tasks were rescued from `full_review` stranding (last session). At restart
they had **no active sessions** — they should dispatch once LLM capacity frees up
from the 4 running StoryOrchestrator planning sessions.

```
mcp__maestro__get_scheduler_state()
# Look for final_review entries — do they have has_active_session: true?
# If still all false after the planning sessions finish, something is wrong.
```

If they are STILL idle after 30+ minutes:
- Run `venv/Scripts/python.exe scripts/inspect_cards.py scheduler`
- Check the `[!] NON-DISPATCHABLE TYPE` section
- If they appear there, the type string is wrong again — use `set_task_type`
- If they don't appear there either, check capacity: `mcp__maestro__get_capacity_status()`

### 2. Did the 4 StoryOrchestrator planning sessions complete or time out again?

At restart, these 4 planning tasks were re-dispatched as zombies:
- `task-1778024927.08974`  — "Fix progress streaming JSON syntax error"
- `task-1778024927.10267`  — "Add integration tests for DAG pipeline"
- `task-1778024927.100124` — "Add unit tests for core services"
- `task-1778024927.097565` — "Implement multi-story dependency scheduling"

**Warning**: multiple recent sessions show `exit_reason: "planning_timeout"` on these
tasks (60-minute wall-clock limit hit). If they time out again on this run, the
planning agent is struggling with these tasks. Next action would be to:
```
mcp__maestro__diagnose_task(task_id="task-1778024927.097565")  # worst offender
# Check budget_trace — is it looping? hitting max_tokens? stalling on tools?
```

### 3. "Fibonacci Dynamic Programming" — 31 demotions, still in final_review

`task-1777182040.748628` ("Fibonacci Dynamic Programming", EasyProject) has been
demoted 31 times. It is in `final_review` with no active session. This task is a
persistent problem child. Once the EasyProject final_review sessions start running:

```
mcp__maestro__diagnose_task(task_id="task-1777182040.748628")
mcp__maestro__get_gate_history(task_id="task-1777182040.748628")
```

If it fails final_review again, look at what the reviewers are objecting to.
It may need a manual `demote_task` → fix → re-run rather than auto-cycling.

### 4. StoryOrchestrator "no Python files" intake rejection — is it recurring?

At 06:53 UTC, `task-1778024927.083731` ("Fix string interpolation bugs") was
**rejected** at intake because "the project directory has no .py files". It was
re-tried and passed at 07:45. But if you see more intake rejections from
StoryOrchestrator tasks citing missing source files, the project path is likely
pointing at an empty or wrong directory.

```
# Check what path StoryOrchestrator is configured with:
mcp__maestro__list_tasks(project="StoryOrchestrator")
# Then verify the path actually has Python files on disk.
```

### 5. Survey task — still idle zombie

`task-1777972562.612366` ("Survey the Architecture System", TheMaestro, INDEV) has
been a zombie for multiple sessions. It has no active session. The scheduler should
auto-dispatch it when LLM capacity frees up. It has been in this state since at
least the previous session — if it is STILL not running 2+ hours from now, it may
have a cooldown or DAG block:

```
mcp__maestro__diagnose_task(task_id="task-1777972562.612366")
```

---

## Current system state (snapshot at 2026-05-07 ~08:00 UTC, after restart)

### Stage distribution
| Stage | Count | Notes |
|-------|-------|-------|
| `idea` | 10 | Queued, waiting for capacity |
| `planning` | 6 | 4 active sessions, 2 waiting (no session) |
| `indev` | 5 | 4 StoryOrchestrator + 1 TheMaestro Survey — none active |
| `final_review` | 5 | All EasyProject — NONE have active sessions |
| `completed` | 5 | |
| `architecture` | 6 | |

### Active sessions at snapshot time
All 4 running sessions are StoryOrchestrator planning agents, started at 07:48:43 UTC
(immediately after server restart from zombie cleanup).

### Pending merges: 5
| Task | Project | Accepted |
|------|---------|---------|
| PRD Master Card for StoryOrchestrator | StoryOrchestrator | 2026-05-06 |
| Implement drag-and-drop | TheMaestro | 2026-04-28 |
| Create database schema | TheMaestro | 2026-04-28 |
| Fibonacci Greenfield | EasyProject | 2026-04-26 |
| Initialize Git repository | TheMaestro | 2026-03-14 |

The TheMaestro and EasyProject merges are very old (weeks). These may be intentional
or forgotten — ask the user if they want them merged or archived.

### LLM topology
- LLM 1: `Qwen3p6-35B-A3B-Q4` on `localhost:8008` — all active sessions on this
- LLM 45: `Qwen3CoderNextBatch` — idle
- LLM 46: `Qwen3p5-Omnicoder-9B-BATCH` — idle
- All tasks pinned to LLM 1; LLMs 45/46 are unused (known issue, no fix attempted)

---

## What happened just before this handoff

### Shell injection hardening (commit b322085, 2026-05-07)

All `shell=True` subprocess calls in the agent tool layer were replaced with
`shell=False` list-based invocation. This is a **breaking signature change** for
some tools — if agents appear to be getting "rejected" or "unknown tool" errors on
tools they used to call, check whether the schema changed:

| Tool | Old signature | New signature |
|------|--------------|---------------|
| `run_test_unittest` | `args: str` | `module: str, pattern: str` |
| `run_test_npm` | `args: str` | *(no args)* |
| `run_build_make` | `target, args: str` | `target` only |
| `run_build_go` | `args: str` | *(no args)* |
| `run_build_gradle` | `target, args: str` | `target` only |
| `run_build_mvn` | `goal, args: str` | `goal` only |
| `run_deps_npm` | `args: str` | *(no args)* |
| `run_audit_bandit` | `path, args: str` | `path` only |
| `run_shell_security` | `command: str` | `tool: str, path: str` |
| `run_shell_review` | `command: str` | `tool: str, path: str` |

**Security tool names** for `run_shell_security` (security_review.py):
`bandit`, `safety`, `pip-audit`, `detect-secrets`, `semgrep`, `trivy`, `npm-audit`

**Review tool names** for `run_shell_review` (final_review.py):
`pytest`, `ruff`, `mypy`, `black-check`, `npm-test`, `npm-lint`

If a running agent fails with `[security] Unknown security tool` or similar, it is
calling the old string-command API. The session will fail — demote the task and let
it re-run under the new schema.

---

## Recurring patterns to know about

### Planning timeout loop (StoryOrchestrator)

Multiple planning tasks are hitting the 60-minute wall-clock limit and being
re-queued. Recent `planning_timeout` exits:
- `task-1778024927.097565` — timed out at 07:39 UTC, re-queued, now running again
- `task-1778024927.086807` — timed out at 07:27 UTC, now in indev (no session)
- `task-1778024927.094912` — timed out at 07:15 UTC, went through correction agent,
  got `corrected` outcome, now in indev (no session)

If a task times out 3+ times in a row, diagnose it:
```
mcp__maestro__get_budget_trace(task_id="<id>", n=20)
# Look for: looping tool calls? length finish_reason? wall-clock drift?
```

### Stage-rename stranding (historical, watch for recurrence)

When a pipeline stage is renamed in code, tasks already in DB retain the old type
and are silently skipped by the scheduler forever. Fixed for `full_review` → `final_review`
last session. If you see tasks that logically should be dispatching but aren't:

```
venv/Scripts/python.exe scripts/inspect_cards.py scheduler
# Look for [!] NON-DISPATCHABLE TYPE at the bottom
```

Fix: `mcp__maestro__set_task_type(task_id, "correct_type")`

### Research Agent submit-loop (FIXED 2026-05-07)

Was caused by `maestro.ini` `research_agent_tools` omitting `submit_work`. Fixed.
If you see an intake agent looping with `finish_reason: "tool_calls"` while saying
it's trying to submit, check that `submit_work` is still in `[intake] research_agent_tools`.

---

## How to diagnose stuck tasks (quick reference)

```
# Cold start: overall health
mcp__maestro__get_project_health()

# Scheduler view — READY / BLOCKED / NON-DISPATCHABLE
venv/Scripts/python.exe scripts/inspect_cards.py scheduler

# Capacity check — are LLM slots full?
mcp__maestro__get_capacity_status()

# Find sessions idle for 15+ min
mcp__maestro__find_stuck_tasks(idle_minutes=15)

# Full picture for one task
mcp__maestro__diagnose_task(task_id="<id>")

# Raw LLM call history
mcp__maestro__get_budget_trace(task_id="<id>", n=20)

# Planning gate failure history
mcp__maestro__get_gate_history(task_id="<id>")
```

### Key signals

| Signal | Meaning | Action |
|--------|---------|--------|
| `activity_status: "idle"` | Zombie — server restarted mid-run | Wait one tick; scheduler auto-recovers |
| `finish_reason: "length"` | max_tokens too low for model | Increase max_tokens on LLM endpoint |
| `[!] NON-DISPATCHABLE TYPE` in inspect_cards | Old stage name in DB | `set_task_type` to correct name |
| `exit_reason: "planning_timeout"` repeatedly | Planning agent can't finish in 60 min | Diagnose budget trace; may need manual intervention |
| `[security] Unknown security tool` in logs | Agent using old run_shell_security API | Demote task; will re-run with new schema |
| `demotion_count > 10` | Task stuck in failure loop | Diagnose gate history; may need manual plan patch |

---

## Known standing issues (not fixed)

- **Broken test: `TestCheckPlanningTimeouts`** (`app/tests/test_scheduler_unit.py:280`).
  Imports `_check_planning_timeouts` from `app.agent.scheduler` which does not exist —
  the function was removed or renamed at some point. The test also references `_session_ids`
  which may similarly be absent. Pre-existing failure, not introduced by recent changes.
  Fix: either restore the function or rewrite the test for the current timeout mechanism.

- **WAL mode and MCP timeout already fixed**: `session.py` sets `PRAGMA journal_mode=WAL`
  via an `event.listen` hook; `mcp_tools/helpers.py` passes `timeout=30` to `sqlite3.connect`.
  The WATCH_FOR note below is outdated — both are resolved.

- **LLMs 45/46 underutilised**: all tasks pinned to LLM 1. 9 idle slots on 45/46.
  No fix attempted — task LLM assignment is a UI/config concern.

- **Merge test race**: concurrent completions on same file → second merge hits conflict
  → demotes to `human_review`. Handled correctly, requires manual re-review.

- **Old pending merges**: TheMaestro and EasyProject merges have been sitting unmerged
  for weeks. Ask user whether to action them.
