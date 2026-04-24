# Planning Pipeline Fix Plan

## Context

This document is a briefing for a fresh context. It describes structural problems in
`app/agent/planning.py` discovered while watching a trivial greenfield task ("write a
recursive Fibonacci function") spend hours cycling through the planning stage.

---

## What the pipeline does today

`PlanningPipeline.run()` runs unconditionally for every task, regardless of complexity:

1. **Codebase survey** — agentic loop, up to 100 turns, reads project files.
2. **Best-of-N design generation** — 5 parallel LLM calls, each using a different
   architect persona (Correctness, Security, Clarity, Performance, Architecture).
3. **Judge** — one LLM call picks the best of the 5 designs.
4. **Design review panel** — 5 sequential LLM calls, each a different reviewer
   (coupling, interface, testability, security, performance). Any single `REJECTED`
   vote immediately triggers a full retry of stages 2–4.
5. **Pitfall detection** — one LLM call for edge-case analysis.
6. **Plan consolidation** — one LLM call to fold pitfall mitigations into the design.

Stages 2–4 retry up to `PLANNING_MAX_DESIGN_RETRIES = 3` times before falling through.

---

## Problems found

### 1. No complexity fast-path (unfixed)

The pipeline is calibrated for large, ambiguous tasks. A 3-line greenfield script goes
through exactly the same 5-design + 5-reviewer gauntlet as a database migration. For
simple tasks this is wasteful and error-prone: more parallel LLM calls means more
timeout exposure, more reviewer calls means more veto surface.

**Root cause:** No complexity classifier. `PLANNING_BEST_OF_N` and the reviewer list
are global constants, not per-task decisions.

**Fix needed:** Before the retry loop, estimate task complexity from the survey result
and task description (file count, step count, description length, keywords like
"greenfield", "simple"). For simple tasks, reduce `best_of_n` to 2–3 and skip the
performance/security reviewers (they have nothing to say about a single-file script).
This is analogous to `_is_unit_test_task()` which already skips reviewers for test
tasks — the same pattern needs generalising.

---

### 2. Hard single-reviewer veto on opinionated tradeoffs (fixed in this branch)

`tally_votes()` in `app/agent/verdicts.py` applies Rule 1: **any single `REJECTED`
vote immediately causes `outcome="rejected"`**. This is correct for security violations.
It is wrong for opinionated performance preferences.

The performance reviewer's prompt says: _"If the design has fundamental scalability
flaws, vote REJECTED."_ For a task that explicitly asks for naive O(2^n) recursion the
reviewer correctly identifies the flaw but wrongly treats it as a hard veto instead of
an advisory warning.

**Fix applied:**
- Added `WARN` verdict to `Verdict` enum in `verdicts.py`. Range `(0, 100)` —
  categorical signal like `SUBDIVIDE_IDEA`.
- `tally_votes` treats `WARN` as pass-ish (same side as `POSSIBLE`/`LIKELY`).
  Produces `outcome="warned"` which the retry loop treats as a pass. Warnings land in
  `conditional_pass_notes` and surface to the user.
- All reviewer prompts now include:
  - The full task description (previously only the title was injected).
  - An explicit instruction: _"If the task description specifies an algorithm or
    approach, treat that as a binding constraint. Vote WARN, not REJECTED."_
- The retry loop (`planning.py`) now treats `"warned"` as a pass outcome alongside
  `"passed"` and `"conditional_pass"`.

---

### 3. Reviewer panel fires on obviously-failed design artifacts (fixed in this branch)

When all 5 parallel design calls fail (timeout or error), `_stage_judge_designs`
returns a synthetic dummy:

```python
dummy = {
    "design_rationale": "CRITICAL FAILURE: ...",
    "file_manifest": [],
    "implementation_steps": [],
    "failed_generation": True,
}
```

The caller (`run()`) previously passed this directly into `_stage_design_review` with
no intervening check. Five reviewers each spent several minutes correctly concluding
"this is garbage" and voting `REJECTED`. The retry loop then treated this as a normal
rejected design and appended the rejection reasons to the survey context before
regenerating — even though the rejection contained zero design information.

On a loaded batch LLM endpoint 3 of 5 parallel calls commonly time out (the endpoint
queues them serially). This means the failure case is not exotic; it happens regularly.

**Fix applied:** Immediately after `_stage_judge_designs`, check for `failed_generation`:

```python
if winning_design.get("failed_generation"):
    logger.warning("All designs failed — skipping reviewer panel, retrying.")
    survey_summary += "\n\n[DESIGN GENERATION FAILED ...]: ..."
    review_votes = [Vote("design_generation", Verdict.REJECTED, 5, "...")]
    continue   # ← skip reviewers, go to next attempt
```

This saves 5 × ~3 min = ~15 minutes of wasted LLM time per failure event.

---

### 4. Partial design-generation failures silently degrade quality (unfixed)

When only _some_ of the 5 parallel calls fail, the surviving designs are used and the
judge picks from a smaller pool. This is handled correctly by the `valid` filter in
`_stage_judge_designs`. However, there is no log entry or warning in the planning
result indicating that, say, only 2 of 5 designs were available to the judge.

The reviewer panel then evaluates a winner that may have been selected from 2 mediocre
designs rather than 5 strong ones, with no signal to the user that quality was degraded.

**Fix needed:** Count `valid` designs before calling the judge. If `len(valid) <
PLANNING_BEST_OF_N`, emit a warning into `survey_summary` and into the stored
`PlanningResult` (e.g. a `generation_warnings` field). This gives the gate and the user
visibility into why a plan might be weaker than expected.

---

### 5. 60-minute wall-clock session timeout does not carry state forward (unfixed)

`scheduler.py` enforces a 60-minute wall-clock limit per planning session. When hit,
the session is marked `planning_timeout` and re-queued. The new session starts from
scratch: new survey, new designs, new review — no memory of what the previous session
already completed.

For a task with a slow batch LLM, a single complete pass through the pipeline (survey +
5 designs + judge + 5 reviewers + pitfalls + consolidation) can approach 60 minutes on
its own. A timeout mid-review discards all previous work.

**Fix needed:** Checkpoint completed sub-stages into the `planning_results` row as they
finish (e.g. `survey_summary`, `best_of_n_designs`, `review_votes`). On re-dispatch,
detect which sub-stages are already complete and skip them. This is a larger refactor
but eliminates the restart-from-zero failure mode.

---

## Files changed

| File | Change |
|------|--------|
| `app/agent/verdicts.py` | Added `WARN` verdict + `(0,100)` range; `tally_votes` treats WARN as pass-ish, produces `"warned"` outcome, surfaces notes in `conditional_pass_notes` |
| `app/agent/planning.py` | Reviewer prompts now include full task description + intent-awareness instruction + `WARN` in output format; retry loop treats `"warned"` as pass; `failed_generation` short-circuit before reviewer panel |

## Files that still need changes

| File | Change needed |
|------|---------------|
| `app/agent/planning.py` | Complexity classifier → reduce `best_of_n` + reviewer subset for simple tasks; partial-generation warning in result |
| `app/agent/planning.py` | Sub-stage checkpointing so timeout + re-dispatch resumes rather than restarts |
| `app/agent/scheduler.py` | On re-dispatch of a timed-out planning session, load existing checkpoint and pass to pipeline |
| `app/database/crud_tasks.py` (or new migration) | `planning_results` table: add `generation_warnings` text column; add checkpoint columns for each sub-stage |

---

## Test coverage needed

- `test_planning_unit.py`: add case for `failed_generation` dummy → assert reviewer
  panel is skipped and loop continues to next attempt.
- `test_verdicts.py`: already covers `WARN` tally behaviour (pass-ish, surfaces notes,
  does not trigger retry).
- No test yet for complexity classifier (does not exist yet).
