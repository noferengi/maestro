# Plan: Replace JSON text signals with submit_work tool calls

## Problem
Agents are instructed to emit raw JSON text blocks (e.g. `{"signal": "ACCEPTED"}`) in their text response as terminal actions. The loops parse these from text content. The test file `test_submit_work_terminal.py` expects `submit_work` to be a proper tool call that returns a `__maestro_terminal__` marker, but the tool was never implemented.

## Goal
Replace JSON text-signal emission with `submit_work` tool calls throughout the codebase.

---

## COMPLETED

### 1. Implemented `submit_work()` in `app/agent/tools.py`
- Added `def submit_work(signal, summary, payload=None)` that returns JSON with `__maestro_terminal__: true`
- Registered in `TOOL_REGISTRY["submit_work"]`
- Added full OpenAI-format schema to `TOOL_SCHEMAS` with `signal` (enum: ACCEPTED/REVERT_TO_DESIGN), `summary`, and `payload` params
- Schema description explicitly tells agents: "This is the PREFERRED way to signal completion — do NOT output raw JSON blocks"

### 2. Added `submit_work` to `INDEV_AGENT_TOOLS` in `app/agent/config.py`
- Appended `"submit_work"` to the tool list so it's sent as a tool schema to the LLM

### 3. Updated `MaestroLoop._handle_tool_calls()` in `app/agent/loop.py`
- After each tool dispatch, checks if `name == "submit_work"`
- Parses the result JSON for `__maestro_terminal__` marker
- Sets `self._terminal_signal` when marker is found
- Logs `logger.info()` for successful submit_work (normal activity, not a warning)

### 4. Updated `MaestroLoop._loop()` to check `_terminal_signal`
- After tool calls complete, checks `self._terminal_signal`
- If signal is ACCEPTED or REVERT_TO_DESIGN, calls `self._handle_terminal()` and returns immediately
- Loop also initializes `self._terminal_signal = None` in `__init__`

### 5. Updated MaestroLoop nudge message
- Changed from "emit your final JSON report" to "call `submit_work(signal='ACCEPTED', summary='...')`"
- Explicitly says: "Do not output free-form prose or raw JSON as a terminal action — use the submit_work tool call."

### 6. Added logger.WARNING when JSON signals detected in text
- In `_loop()`, when `_extract_signal(content)` finds a signal in text content, logs:
  `"agent emitted a JSON signal in text content (signal=X) instead of calling submit_work tool"`
- This is a WARNING because it means the agent ignored instructions to use the tool call

---

## REMAINING

### 7. Update `ComponentLoop.run()` in `app/agent/component_loop.py`
- Currently detects terminal signals via substring match on text content:
  `'"signal": "ACCEPTED"' in content`
- Need to add: detect `submit_work` tool calls, parse `__maestro_terminal__` marker, exit loop
- Need to add `_terminal_signal` attribute to ComponentLoop
- Need to update the signal detection to also check tool results, not just text content

### 8. Update ComponentLoop nudge message
- Currently says: `emit your final signal: {"signal": "ACCEPTED"}`
- Should say: `call submit_work(signal='ACCEPTED', summary='...')`

### 9. Update `system_prompt.py` (MaestroLoop system prompt)
- Sections 5 (FAILURE PROTOCOL) and 6 (OUTPUT FORMAT - TERMINAL ACTIONS) instruct agent to emit JSON blocks
- Replace all "Emit this exact JSON" / "Your final action must ALWAYS be one of the two JSON structures" with instructions to call `submit_work` tool
- Keep NEEDS_RESEARCH as JSON text (it's non-terminal and handled differently)

### 10. Update `app/agent/intake.py`
- Three stage prompts (Scope, Feasibility, Conflict) say "Respond ONLY with the JSON object. No markdown fences."
- Replace with "Call submit_work with payload={...}"

### 11. Update `app/agent/planning.py`
- Multiple prompts say "Output JSON" / "Output only JSON"
- These are pipeline stages (design generation, judge, review, etc.) — need to determine if they should use submit_work or if JSON text is acceptable for non-terminal pipeline stages

### 12. Update `app/agent/research.py`
- Research agent says "output ONLY this JSON (no other text)" with a JSON verdict block
- Research agents are non-terminal — they feed findings back to the loop. May not need submit_work.

### 13. Update `app/agent/subdivide.py`
- Says "output ONLY this JSON object (no markdown fences)"
- Non-terminal pipeline stage — may not need submit_work.

### 14. Update `app/agent/dreamer.py`
- Says "Output JSON only. No prose before or after."
- Non-terminal pipeline stage — may not need submit_work.

### 15. Update review prompts (`full_review.py`, `conceptual_review.py`, `security_review.py`)
- Say "Output your verdict as JSON"
- Non-terminal pipeline stages — may not need submit_work.

### 16. Run `test_submit_work_terminal.py` to verify
- The test file expects `submit_work` to exist and work as described
- Tests cover: tool function, MaestroLoop detection, ComponentLoop detection, nudge messages

---

## Key Design Decisions

1. **Terminal vs non-terminal**: `submit_work` is for TERMINAL actions (agent is done with its task). Non-terminal pipeline stages (planning, research, review, subdivision) that produce JSON output for the orchestrator to parse can keep JSON text output — they're not the agent loop's final action.

2. **Backward compatibility**: The loops still parse JSON signals from text content (with a WARNING logged). This ensures existing behavior doesn't break if agents haven't updated yet.

3. **ComponentLoop vs MaestroLoop**: ComponentLoop currently uses substring matching (`'"signal": "ACCEPTED"' in content`). It needs the same tool-call-based detection as MaestroLoop.

4. **Signal reset**: `_terminal_signal` is initialized to `None` in `__init__`. It's set once and never reset during the loop, which is correct — once a terminal signal is received, the loop exits immediately.

---

## Files to Edit

| File | Change |
|------|--------|
| `app/agent/tools.py` | DONE: added `submit_work()` function, registry entry, schema |
| `app/agent/config.py` | DONE: added `submit_work` to `INDEV_AGENT_TOOLS` |
| `app/agent/loop.py` | DONE: added `_terminal_signal`, detection in `_handle_tool_calls`, check in `_loop`, nudge update, WARNING on JSON text |
| `app/agent/component_loop.py` | TODO: add `_terminal_signal`, detect `submit_work` tool calls, update nudge |
| `app/agent/system_prompt.py` | TODO: replace JSON signal instructions with `submit_work` tool instructions |
| `app/agent/intake.py` | TODO: replace "Respond ONLY with JSON" with `submit_work` tool calls |
| `app/agent/planning.py` | MAYBE: non-terminal pipeline stage, assess if changes needed |
| `app/agent/research.py` | MAYBE: non-terminal, assess if changes needed |
| `app/agent/subdivide.py` | MAYBE: non-terminal, assess if changes needed |
| `app/agent/dreamer.py` | MAYBE: non-terminal, assess if changes needed |
| `app/agent/full_review.py` | MAYBE: non-terminal, assess if changes needed |
| `app/agent/conceptual_review.py` | MAYBE: non-terminal, assess if changes needed |
| `app/agent/security_review.py` | MAYBE: non-terminal, assess if changes needed |
| `app/tests/test_submit_work_terminal.py` | TODO: run tests to verify |
