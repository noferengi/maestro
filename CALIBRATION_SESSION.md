# Math Pipeline Calibration — Session Notes (2026-05-21)

## Goal

Run a calibration card through the Mathematics/Proof pipeline end-to-end to verify
the system works. Card: **"Calibrate -- Lean4: infinitely many primes"**
(`task-1779346920.85829`, EasyProject, template 9).

Expected outputs:
1. A `.lean` file using `Nat.infinite_setOf_prime` from Mathlib — no sorry, zero errors
2. A writeup in the document store under `calibration/infinitely_many_primes`
3. Lean4 formal verification gate passes

---

## Bugs Found and Fixed

### Bug 1 — Spin loop: indev task in Math project (scheduler.py)

**Symptom:** `task-1776993760.413454` (Windows Python Platform, `indev` stage) was
dispatched every 7 seconds. Slot 1/4 was consumed and immediately released, leaving
no room for the math task to get a turn.

**Root cause:** The task was a Software Dev task left in EasyProject after the project
was switched to the Math template (template 9). Template 9 has no `indev` stage.
`_run_indev` found no planning result, called `advance_stage(fail, from_stage=indev)`,
which fell back to `_LEGACY_TRANSITIONS["indev"]["fail"] = "indev"` — a self-loop.
The function returned normally, so `_failed_cooldowns` was never set, and the
scheduler re-dispatched on the very next tick.

**Fix:**
- Soft-deleted `task-1776993760.413454` via `DELETE /api/tasks/...`
- Added `_failed_cooldowns[task_id] = time.time()` in `_run_indev` immediately after
  the "no planning result" demotion path, so future occurrences back off for 60s
  instead of spinning

### Bug 2 — Node capacity race: fast-completing thread blocks next dispatch (scheduler.py)

**Symptom:** The math task (`PROOF_ATTEMPT`) was skipped every tick even with 3 free
slots. It sat `blocked_on_model` for 95+ minutes despite LLM 1 being idle.

**Root cause:** The spinning task thread completed in ~100ms, decrementing
`_llm_session_counts[1]` back to 0 before the scheduler's dispatch loop reached the
math task. At that point:
- `llm_already_loaded = _llm_session_counts[1] > 0` → False (thread done)
- `node_active_counts[1] = 1` (incremented in-place earlier that tick for the spinner)
- `not False and 1 >= max_loaded_models(1)` → True → **slot rejected**

The in-tick `node_active_counts` was correctly incremented but `_llm_session_counts`
had already been decremented by the completed thread — creating a phantom "model loaded"
block for the next candidate.

**Fix:** Added `_tick_dispatched_llm_ids: set[int]` — a module-level set cleared at the
start of every `_tick()` and populated by `_check_and_reserve_slot` on each successful
reservation. The `llm_already_loaded` check now reads:

```python
llm_already_loaded = _llm_session_counts[llm_id] > 0 or (llm_id in _tick_dispatched_llm_ids)
```

A fast-completing thread no longer blocks the next candidate in the same tick.

### Bug 3 (minor) — budget trace tool: stale field semantics (mcp_tools/diagnostics.py)

Since migration 0076 `budget_entries` stores per-turn **deltas**, not cumulative totals.
`get_budget_trace` and the budget trace section of `diagnose_task` were still labelling
the column `prompt_cost`, implying it was a total context size. They also omitted
`prompt_message_count` (added in 0076), which is the clean absolute measure of context
depth.

**Fix:** Both tools updated:
- `prompt_cost` → `prompt_cost_delta` in SELECT and output
- `prompt_message_count` added to SELECT and output
- Docstrings updated to explain the delta model

---

## Pipeline Progress at Time of Reboot

The card made it through three stages after the scheduler was unblocked:

| Stage | Result | Time |
|---|---|---|
| PROOF_ATTEMPT (×24) | error (SymPy Docker issues) then **pass** | 09:16–20:28 |
| REFLECTION | **pass** | 20:28–20:41 |
| FORMAL_VERIFICATION | **running** (active at reboot) | 20:41– |

The PROOF_ATTEMPT agent wrote `infinitely_many_primes.lean` using
`Nat.infinite_setOf_prime` and stored the Euclid writeup in the document store.
REFLECTION passed after ~13 minutes of reviewing the proof artefacts.
FORMAL_VERIFICATION had just started and written the file to the worktree when
the machine rebooted (Windows Update).

---

## What Still Needs to Happen

### Immediate (after machine is back up)

- [ ] Restart inference engine (llama.cpp on localhost:8008)
- [ ] Restart Maestro via `Launcher.ps1`
- [ ] Verify server is reachable: `curl http://localhost:8000/api/projects`
- [ ] Check `task-1779346920.85829` — FORMAL_VERIFICATION session was killed mid-run;
      scheduler should re-dispatch it automatically on restart (mid-pipeline recovery)

### FORMAL_VERIFICATION stage

The Lean4 gate (`verifiers.py:run_verifier`) is the critical unknown. It needs to:
1. Spin up the `sympy-lean4-sandbox:latest` Docker container on arcbox
2. Compile `infinitely_many_primes.lean` with `lake build` or `lean --check`
3. Confirm zero errors, zero `sorry` placeholders

The PROOF_ATTEMPT agent noted "SymPy Docker configuration issues" in earlier runs
(tool calls failed 3× before the agent worked around it). The same Docker path is
used for Lean4 verification. If the gate hangs or fails, check:
- `DOCKER_HOST=ssh://freazer@arcbox` is set in `.env`
- `docker pull sympy-lean4-sandbox:latest` resolves on arcbox (image is pre-built,
  should be instant)
- `verifiers.py` subprocess timeout is generous enough for `lake build`

### After FORMAL_VERIFICATION

If the gate passes, the stage advances to WRITEUP (position 9 in template 9), then
to `accepted` (terminal). The pipeline is complete and the calibration card is done.

If the gate fails (Lean4 compile error), the card will stay in FORMAL_VERIFICATION
and the agent will need to fix the `.lean` file. Most likely failure mode: the worktree
was torn down on reboot, so the file may need to be rewritten from scratch.

### Remaining known issues to watch

- `task-1777241870.854011` (Opt: Module-level persistent cache) — also `indev` in the
  Math project. It was running on the Fibonacci sub-task tree at time of reboot. It
  should re-dispatch but will also immediately hit "No planning result → demote" now
  that the cooldown fix is in. Watch for it cycling into a 60s cooldown loop rather
  than a hard spin.
- The other Fibonacci child tasks (`task-1777241870.861412` final_review,
  `task-1777241870.867839` human_review) are Software Dev tasks in a Math project —
  same template mismatch. Consider soft-deleting or migrating EasyProject back to the
  Software Dev template if Fibonacci work needs to continue there.
