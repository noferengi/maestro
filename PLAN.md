# Test Suite Fix Plan

## What was done

### Fixed (confirmed passing)
1. **`app/agent/pip_agent.py`** — Production bug: `response, stats = await call_llm(...)` unpacked a dict as a tuple (getting string keys). Fixed to `response = await call_llm(...)` + `stats = response.get("usage", {})`. Same fix in `_check_single_pip`.

2. **`app/tests/test_pip_agent_unit.py`** — Mock returns were `(json_string, stats)` tuples. Fixed all mocks to return proper `{"choices": [...], "usage": {...}}` dicts.

3. **`app/tests/test_intake_pipeline.py`** — Mock response dicts used `"verdict"` as outer key but `intake._extract_vote()` looks for `"vote"`. Fixed all top-level constants and one inline dict. Added `_patch_static(pipeline)` to `test_static_analysis_fallback_when_no_affected_areas` to prevent filesystem scan + research agent activation.

4. **`app/tests/test_pip_resolution_unit.py`** — Tests patched nonexistent `app.agent.agent_loop.call_llm` / `async_dispatch_tool`. Fixed to `app.agent.pip_resolution.call_llm` and `app.agent.pip_resolution.async_dispatch_tool`.

5. **`app/tests/test_scheduler_unit.py`** — `TestRunOptimizationTask::test_demotes_to_indev_on_exception` hit `_record_demotion_inline` → `asyncio.run(generate_pip(...))` → real HTTP to localhost:8008. Fixed by adding `patch("app.agent.scheduler._record_demotion_inline", MagicMock())`.

### Key operational finding
Bash tool hangs when tests produce large log output (thousands of research agent error lines overflow the pipe buffer). Mitigation: always append `2>&1 | grep -v "^ERROR\|^WARNING\|^INFO" | tail -N` to pytest commands.

---

## Current state

`venv/Scripts/python.exe -m pytest app/tests/ tests/ -q --tb=no --timeout=15` runs in ~20s with no hangs: **785 pass, 33 fail, 15 errors**.

---

## Remaining failures (33 failed, 15 errors)

### Group 1 — `app/tests/test_research_agent_unit.py` (12 failures)
**Symptom**: verdict comes back `NOT_SUITABLE` instead of `LIKELY`. The test mocks `app.agent.research.call_llm` correctly and builds responses that call `submit_work` with `signal="RESEARCH_COMPLETE"`. The agent isn't processing the `submit_work` tool call into a verdict.
**Likely cause**: `dispatch_tool("submit_work", ...)` is not patched, so it runs the real implementation; or the research agent's tool-call handling path changed and `RESEARCH_COMPLETE` signal is no longer extracted this way.
**Files**: `app/tests/test_research_agent_unit.py` (~line 250+), `app/agent/research.py` (tool call / verdict extraction path).

### Group 2 — `app/tests/test_submit_work_terminal.py` (5 failures)
**Symptom**: `AttributeError: 'MaestroLoop' object has no attribute '_dispatch_tools'`. Tests call `await loop._dispatch_tools(tool_calls)` but the method was renamed or removed.
**Fix**: Find current tool-dispatch method name in `app/agent/loop.py` and update the test.
**Files**: `app/agent/loop.py` (search for tool dispatch), `app/tests/test_submit_work_terminal.py`.

### Group 3 — `app/tests/test_e2e_pipeline.py` (2 failures)
**Symptom**: `outcome == "passed"` instead of `"rejected"`. The `MockLLM` intake scenarios in `app/agent/mock_llm.py` use `"verdict"` as the outer key (e.g. `_SCOPE_RESPONSE_REJECTED["verdict"] = {...}`) but `intake._extract_vote()` looks for `"vote"`.
**Fix**: In `mock_llm.py`, rename `"verdict"` → `"vote"` in all `_SCOPE_RESPONSE_*`, `_FEASIBILITY_RESPONSE_*`, `_CONFLICT_RESPONSE_*` dicts (lines ~87–167).

### Group 4 — `app/tests/test_pip_workflow.py` (1 failure)
`test_preflight_all_passed_writes_verification_rows`: likely same call_llm mock format issue as pip_agent_unit. Check mock return values in this test.

### Group 5 — `app/tests/test_planning_unit.py` (1 failure)
`TestFeasibilityRecheck::test_feasibility_recheck_enabled_llm_pass`: likely call_llm mock format or `"verdict"` vs `"vote"` key issue.

### Group 6 — `app/tests/test_survey_orchestrator_integration.py` (1 failure)
Unknown — needs traceback. Run: `pytest app/tests/test_survey_orchestrator_integration.py -v --tb=short --timeout=10 2>&1 | grep -v "^ERROR\|^WARNING" | tail -20`

### Group 7 — `app/tests/test_tools_safety.py` (1 failure)
`TestAssertSafePath::test_contextvar_override_read_allows_outside`: ContextVar test failure. Needs traceback.

### Group 8 — `tests/test_intake_pipeline.py` (2 failures + 12 errors)
Different file from `app/tests/test_intake_pipeline.py`. Located in `tests/` directory.
- 2 failures: `TestTallyVotes` — likely vote-tallying logic, independent of LLM format.
- 12 errors: `TestStageExecutionOrder`, `TestNeedsResearchHandling`, `TestTieHandling`, `TestFullPipelineWithMockLLM` — probably import or fixture errors. Check `tests/conftest.py` and the test file's imports.

### Group 9 — `tests/test_migrations.py` (8 failures + 3 errors)
Migration framework tests — likely pre-existing, unrelated to current work. Low priority unless specifically requested.

---

## Recommended fix order

1. **`app/agent/mock_llm.py`** — `"verdict"` → `"vote"` in intake scenario dicts (fixes Group 3, likely also Group 5)
2. **`app/agent/loop.py`** → **`app/tests/test_submit_work_terminal.py`** — find current method name, fix test (fixes Group 2)
3. **`app/tests/test_research_agent_unit.py`** — diagnose submit_work / RESEARCH_COMPLETE signal path (fixes Group 1, 12 tests)
4. **`app/tests/test_pip_workflow.py`** — fix call_llm mock format (Group 4)
5. **`tests/test_intake_pipeline.py`** — check errors (Group 8)
6. Groups 6, 7, 9 — investigate individually

---

## Resume commands

```bash
# Check overall state (fast)
venv/Scripts/python.exe -m pytest app/tests/ tests/ -q --tb=no --timeout=15 --no-header 2>&1 | grep -v "^ERROR\|^WARNING\|^INFO" | tail -5

# Targeted runs per group
venv/Scripts/python.exe -m pytest app/tests/test_submit_work_terminal.py -q --tb=short --timeout=10 --no-header 2>&1 | grep -v "^ERROR\|^WARNING\|^INFO" | tail -20

venv/Scripts/python.exe -m pytest app/tests/test_research_agent_unit.py::TestResearchAgentRun::test_immediate_verdict_terminates_on_life_1 -v --tb=short --timeout=10 2>&1 | grep -v "^ERROR\|^WARNING\|^INFO" | tail -20

venv/Scripts/python.exe -m pytest app/tests/test_e2e_pipeline.py -v --tb=short --timeout=10 2>&1 | grep -v "^ERROR\|^WARNING\|^INFO" | tail -20
```
