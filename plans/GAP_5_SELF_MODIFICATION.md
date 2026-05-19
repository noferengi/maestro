# Gap 5 — Controlled self-modification

**Status:** Planning  
**Effort:** Large  
**Priority:** Low until gaps 1-4 are stable — highest risk item

## Problem

`_assert_safe_write_path()` explicitly blocks writes to `D:/workspace/TheMaestro/`.
Agents are isolated to user project directories by design. Maestro cannot edit its own
source code, pipelines defined in Python, or agent system prompts that live in code
rather than the DB. Self-repair beyond what `update_pipeline_stage` covers (DB-stored
configs) requires human edits.

## Rough phases

1. Designate and isolate the self-improvement project
2. Path guard exemption — minimal and auditable
3. Self-modification pipeline — stages, gates, required test pass
4. Human review integration
5. Permanent off-limits zones and rate limiting

## Open questions

### Project designation
- How is a "self-improvement project" distinguished from regular projects? Options:
  a boolean `is_self_improvement` on the `Project` record, a reserved project name
  (`_maestro_self`), or a system setting. The path guard reads this flag to allow
  writes into `D:/workspace/TheMaestro/`.
- Should only one such project be allowed to exist at a time, or can there be multiple
  (e.g., one for core agent code, one for frontend)?
- Does designating a project as self-improvement require an admin action (separate
  endpoint, requires restart) or can it be set via the normal project edit UI?

### Scope of writeable paths
- Which directories inside `D:/workspace/TheMaestro/` are in scope? Allowing writes to
  `app/agent/`, `app/database/`, and `app/web/` is very different from allowing writes
  to `app/agent/tools.py`, `app/agent/scheduler.py`, or `app/agent/llm_client.py`.
- Should there be a file-level allowlist, a directory-level allowlist, or a blocklist
  of permanently protected files (e.g., `_assert_safe_write_path` itself, `conftest.py`,
  `.env`, all migration files)?
- Are migration files in scope? An agent adding a DB column is a meaningful and useful
  self-modification, but a bad migration can corrupt the database. What gate applies?

### Test gate
- What test command must pass before the task can leave `indev`? Options: full suite
  (`pytest app/tests/ -q`, ~45s, 821 tests), a targeted fast smoke subset, or a
  two-phase gate (fast smoke first, full suite only if smoke passes).
- What if the agent breaks a test that was already failing before it started (a
  pre-existing red test)? Should the gate compare against a baseline pass/fail snapshot,
  or require all tests green regardless?
- Should the gate include a server restart and integration test (verify the server
  starts cleanly after the change), or is unit-test pass sufficient?

### Human review
- What is the review surface? Options: the existing `human_review` pipeline stage
  (reviewer sees task history and a diff link), a GitHub PR (requires remote), or an
  inbox notification with approve/reject buttons in the Maestro UI.
- What information must the reviewer see? At minimum: the git diff, the test results,
  and the agent's explanation of what it changed and why.
- Is the `human_review` stage skippable via any mechanism (auto-approve toggle,
  time-based approval if no action taken in N hours), or is it permanently non-automatable
  for self-modification tasks?

### Branch and merge strategy
- Agents already work on `maestro/task-{id}` branches via git worktrees. For
  self-modification, does that branch target `main` directly after human approval,
  or does it target a `maestro/self-improvement` integration branch that accumulates
  changes before a single human-triggered merge?
- After merge, does the pipeline automatically call `restart_server`, or does the human
  reviewer do that manually? If automatic, what is the window between merge and restart
  during which the server is running stale code?

### Rate limiting and runaway prevention
- If self-modification causes the scheduler to dispatch more work, which triggers more
  stalls, which triggers more self-modification runs — what prevents an acceleration
  loop? Options: maximum N self-modification tasks active at once, minimum interval
  between self-modification merges, human must explicitly re-enable after each merge.
- Should there be a circuit breaker: if three consecutive self-modification attempts
  fail the test gate, the self-improvement project is paused until a human reviews?
