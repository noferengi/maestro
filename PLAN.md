# PLAN.md — Implementation Plan (read this after /clear)

Read `SUMMARY.md` first for context. This file is the execution guide.

---

## Status

As of 2026-03-24: all previously planned items are complete. The scheduler-dispatched file
summary system (migration 0022, completion registry, enqueue/execute split, async wait path)
was implemented and fully tested this session. 572 tests passing.

No outstanding blockers. Suggest new priorities from the user.

---

## COMPLETED THIS SESSION — Scheduler-Dispatched File Summary Jobs

> Implemented from plan `pure-wibbling-hummingbird.md`. All steps complete.

### What was built

**Migration `0022_file_summary_jobs.py`** (NEW)
- `file_summary_jobs` table. `priority REAL DEFAULT -1.0` (sorts before research at 0.0).
- Indexes on `(status, priority, created_at)` and `(sha1_hash, file_size_bytes)`.
- `file_content TEXT NOT NULL` — stored in job row so workers need no filesystem access.

**`database.py`** — `FileSummaryJob` model + 5 CRUD functions (after `FileSummary` model):
- `create_file_summary_job(sha1, filesize, path, content, *, static_analysis_json, llm_id, budget_id, task_id, priority)`
- `get_pending_file_summary_jobs(limit=20)` — `ORDER BY priority ASC, created_at ASC`
- `get_file_summary_job_by_sha1(sha1, filesize)` — dedup: finds pending/running jobs
- `update_file_summary_job(job_id, **kwargs)` — auto-sets `completed_at` on terminal status
- `count_pending_file_summary_jobs()` — for scheduler status endpoint

**`scheduler.py`** — completion registry + dispatch:
- Module-level: `_pending_completions: dict[str, threading.Event]` + `_pending_completions_lock`
- `get_or_create_completion_event(key) -> (Event, created: bool)` — thread-safe
- `signal_completion(key)` — pops from dict, calls `.set()`
- `wait_for_completion(key, timeout) -> bool` — returns `True` if key already gone (completed before wait)
- `_dispatch_file_summary_jobs()` — mirrors `_dispatch_research_jobs()` pattern
- `_run_file_summary_job(job, llm)` — own asyncio loop, calls `execute_file_summary()`,
  always calls `signal_completion()` in `finally` (even on failure — no hangs)
- `_tick()` — calls `_dispatch_file_summary_jobs()` FIRST, before `_cleanup_finished()`
- `get_scheduler_status()` — added `pending_file_summary_jobs` count

**`file_summary_agent.py`** — split `run_file_summary()` into two functions:
- `enqueue_file_summary(abs_path, *, task_id, llm_id, budget_id) -> (completion_key, sha1, filesize)`
  - Reads file, computes SHA1+size
  - DB cache hit → returns `("", sha1, filesize)`
  - Calls `get_or_create_completion_event(key)` — if created and no existing job, creates DB job
  - Returns non-empty key for caller to wait on
- `execute_file_summary(*, sha1, filesize, file_path, file_content, ...) -> dict`
  - Builds prompt, calls `call_llm()` (using `base_url=`, `model=` kwargs — NOT `llm_base_url`/`llm_model`)
  - Calls `create_file_summary()` to store result
  - Returns `{"prompt_tokens": int, "completion_tokens": int}`

**`project_snapshot.py`** — `async_build_file_summary()` updated:
```python
completion_key, sha1, filesize = enqueue_file_summary(abs_path, ...)
if completion_key:
    loop = asyncio.get_event_loop()
    completed = await loop.run_in_executor(None, wait_for_completion, completion_key, 120.0)
    if not completed:
        return structural  # timeout fallback
cached = get_file_summary(sha1, filesize)
if cached:
    result = f"## Summary\n{cached.summary}\n\n{structural}"
    _file_summary_cache[session_key] = result
    return result
return structural  # job failed, graceful fallback
```

**Production bug fixed in `project_snapshot.py`:**
Session cache key changed from `(abs_path, mtime, size)` to `("llm", abs_path, mtime, size)`.
Without this, `build_file_summary()` (called at the top of `async_build_file_summary()`) primed
the cache with the structural result, causing the session-cache check to always return
structural before reaching the enqueue logic. Tests found this.

**`test_read_file_redesign.py`** — 14 new tests (29 total in file):
- 3 completion registry tests
- 4 `enqueue_file_summary` unit tests (use `llm_id=None, budget_id=None` to avoid FK errors)
- 7 `async_build_file_summary` integration tests (use `clean_session_cache` fixture, patch
  `app.agent.file_summary_agent.enqueue_file_summary` and `app.database.get_file_summary`)

---

## COMPLETED PREVIOUS SESSION — P0-A/B + P1 + P2 + Diagnostics Enhancements

### P0-A — TOO_LARGE verdict for context overflow ✓

Research agent detects 400 responses as context overflow, immediately terminates the life,
and emits a `TOO_LARGE` verdict that propagates through intake to trigger subdivision.

### Step 1 — `app/agent/verdicts.py`

Add `TOO_LARGE` to the verdict enum and confidence ranges:

```python
# In the Verdict enum (or wherever verdicts are defined):
TOO_LARGE = "TOO_LARGE"

# In VERDICT_RANGES / _VERDICT_CONFIDENCE_RANGES:
"TOO_LARGE": (100, 100),   # always 100% confident it's too large
```

Also add to `_FORCED_VERDICT_GRAMMAR` verdict alternatives if it ever needs to be grammar-
constrained (low priority — TOO_LARGE is synthesised internally, not emitted by the LLM).

### Step 2 — `app/agent/research.py`

In `_run_life()`, replace the generic exception handler with context-overflow detection:

```python
try:
    response = await self._call_llm(messages)
except Exception as exc:
    exc_str = str(exc)
    # 400 = context overflow — terminate this life immediately
    if "400" in exc_str or "Bad Request" in exc_str:
        logger.warning(
            "Research agent context overflow (life %d, turn %d) — emitting TOO_LARGE",
            life_num, turns_used,
        )
        return LifeResult(
            findings=f"Life {life_num} hit context window limit at turn {turns_used}.",
            vote={"verdict": "TOO_LARGE", "confidence": 100,
                  "justification": "Context window exceeded — task scope is too large for a single research life.",
                  "findings": f"Context overflowed at turn {turns_used} of life {life_num}."},
            turns_used=turns_used,
        )
    # Other errors: log and nudge as before
    logger.error("Research agent LLM call failed (life %d, turn %d): %s", life_num, turns_used, exc)
    messages.append({"role": "user", "content": f"[SYSTEM] LLM call failed: {exc}. Try a different approach or render your verdict now."})
    continue
```

In `run()`, handle TOO_LARGE verdict propagation:

```python
if life_result.vote and life_result.vote.get("verdict") == "TOO_LARGE":
    # Context overflow — don't retry more lives, surface immediately
    logger.info("Research agent TOO_LARGE — task scope exceeds context budget")
    return ResearchResult(
        vote=life_result.vote,
        lives_used=life_num,
        total_turns=total_turns,
        findings="\n\n".join(self._accumulated_findings),
        prompt_tokens=self._total_prompt_tokens,
        completion_tokens=self._total_completion_tokens,
    )
```

### Step 3 — `app/agent/intake.py`

Find where research agent result votes are processed. When `verdict == "TOO_LARGE"`, return
a special stage result that causes the intake pipeline to emit a SUBDIVIDE_IDEA outcome (or
a dedicated TOO_LARGE outcome that `tally_votes()` maps to subdivision). Check how
`SUBDIVIDE_IDEA` is currently triggered and mirror that path.

### Step 4 — Tests

Add to `test_research_agent_unit.py`:
- `test_context_overflow_400_emits_too_large`: mock call_llm to raise
  `httpx.HTTPStatusError` with status 400; assert LifeResult.vote["verdict"] == "TOO_LARGE"
- `test_too_large_terminates_without_more_lives`: assert run() returns immediately, lives_used=1
- `test_non_400_error_still_nudges`: mock call_llm to raise a non-400 error; assert loop
  continues (existing behaviour preserved)

---

## P0-B — GBNF epilogue 500 fallback retry ✓

### Problem
`_forced_verdict_call()` in `app/agent/research.py` sends a `grammar=` field with a GBNF
string. The current llama.cpp build rejects it with 500 ("Failed to parse input at pos 657").
The exception handler immediately falls back to NOT_SUITABLE/confidence 40 — wasting all
accumulated findings.

### Fix — `app/agent/research.py`, `_forced_verdict_call()`

After the 500, retry without grammar constraint:

```python
async def _forced_verdict_call(self) -> dict:
    # ... build system_prompt, user_msg as before ...

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    # Attempt 1: grammar-constrained
    for use_grammar in (True, False):
        try:
            kwargs = dict(
                base_url=self.llm_base_url, model=self.llm_model,
                temperature=0.1, max_tokens=512,
                task_id=self.task_id, llm_id=self.llm_id, budget_id=self.budget_id,
            )
            if use_grammar:
                kwargs["grammar"] = _FORCED_VERDICT_GRAMMAR
            response = await call_llm(messages, **kwargs)
            # ... parse grade/verdict/confidence as before ...
            # on success, return the parsed dict
        except Exception as exc:
            logger.warning(
                "Forced verdict epilogue attempt (grammar=%s) failed: %s", use_grammar, exc
            )
            continue  # try without grammar

    # Ultimate fallback (both attempts failed)
    return { "verdict": "NOT_SUITABLE", "confidence": 40, "grade": 4000, ... }
```

### Tests
- `test_epilogue_retries_without_grammar_on_500`: first call raises 500, second returns valid
  JSON without grammar constraint; assert result uses the second call's verdict.
- `test_epilogue_fallback_when_both_attempts_fail`: both calls raise; assert NOT_SUITABLE/40.

---

## P1 — Context saturation visualization in diagnostics turn table ✓

### Problem
The session turn table shows absolute prompt/generation token counts per turn. The user
cannot see how quickly context is filling up (critical for catching overflow before it 400s),
or how much of each turn's prompt is "new" vs "inherited context".

### Step 1 — Extend `allDiagLlms` in `diagnostics.js`

Currently `allDiagLlms = {}` maps `id → name`. Extend to `id → {name, max_context}`:

```javascript
// In the fetch block that populates allDiagLlms:
llmsData.forEach(l => {
    allDiagLlms[l.id] = { name: l.model || l.address, max_context: l.max_context || 0 };
});

// Wherever allDiagLlms[fe.llm_id] is used for display, update to:
const llmInfo  = allDiagLlms[fe.llm_id] || {};
const llmName  = llmInfo.name || fe.llm_id || '—';
const maxCtx   = llmInfo.max_context || 0;
```

### Step 2 — Add Δ Prompt and Ctx% columns to `buildSessionSummary()`

Pass the `fullEntries` array index so each row can compute delta vs prev:

```javascript
fullEntries.forEach((fe, i) => {
    const pp       = fe.prompt_cost || 0;
    const prevPp   = i > 0 ? (fullEntries[i-1].prompt_cost || 0) : 0;
    const deltaPp  = i > 0 ? pp - prevPp : pp;   // tokens added this turn
    const llmInfo  = allDiagLlms[fe.llm_id] || {};
    const maxCtx   = llmInfo.max_context || 0;
    const ctxPct   = maxCtx > 0 ? Math.round(pp / maxCtx * 100) : null;
    const ctxClass = ctxPct == null ? '' :
                     ctxPct >= 90 ? 'ctx-critical' :
                     ctxPct >= 75 ? 'ctx-warn' :
                     ctxPct >= 50 ? 'ctx-caution' : '';
    const ctxStr   = ctxPct != null ? `${ctxPct}%` : '—';

    // Add to row HTML:
    // <td class="col-r col-dim">${fmtTokens(deltaPp)}</td>
    // <td class="col-r ${ctxClass}" title="${pp} / ${maxCtx} tokens">${ctxStr}</td>
});
```

Update the `<thead>` to add two columns: `Δ Prompt` and `Ctx%`.
Update `colspan` in `<tfoot>` accordingly.

### Step 3 — `diagnostics.css`

```css
.ctx-caution { color: #ffc107; }
.ctx-warn    { color: #fd7e14; }
.ctx-critical { color: #dc3545; font-weight: 700; }
```

### No backend changes needed.

---

## P2 — Color-coded agent type label in conversation header ✓

### Step 1 — `app/web/diagnostics.css`

Replace flat gray `.diag-conv-type-label` with per-type variants:

```css
.diag-conv-type-label {
    font-size: 0.7rem; font-weight: 700; text-transform: uppercase;
    padding: 0.15rem 0.4rem; border-radius: 3px; color: #fff;
}
.diag-conv-type-label.type-surveyor     { background: #0d6efd; }
.diag-conv-type-label.type-designer     { background: #6f42c1; }
.diag-conv-type-label.type-reviewer     { background: #20c997; }
.diag-conv-type-label.type-judge        { background: #fd7e14; }
.diag-conv-type-label.type-research     { background: #ffc107; color: #212529; }
.diag-conv-type-label.type-pitfall      { background: #e83e8c; }
.diag-conv-type-label.type-maestro_loop { background: #198754; }
.diag-conv-type-label.type-unknown      { background: #6c757d; }
```

### Step 2 — `app/web/diagnostics.js`, `renderConversation()`

```javascript
// Change:
`<span class="diag-conv-type-label">${escapeHtml(entryType)}</span>`
// To:
`<span class="diag-conv-type-label type-${entryType}">${escapeHtml(entryType)}</span>`
```

No backend changes. No tests needed. Verify manually at `http://localhost:8000/diagnostics`.

---

## Execution Order

(All current items complete. Awaiting new priorities from user.)

---

## Running tests after any change

```bash
venv/Scripts/python.exe -m pytest app/tests/ -q
```

572 tests, all should pass. Frontend changes have no automated tests — verify manually.
