# Plan: Migrate Agent Terminal Signaling to `submit_work` Tool Call

## Problem Statement

The active codebase currently uses **raw JSON signal emission in text content** for terminal signaling (agents output JSON blocks as free-form text). This is fragile and hard to parse reliably. The intended approach is for agents to use the **`submit_work` tool call** mechanism, which returns a JSON response with a `__maestro_terminal__` marker that the loops can detect unambiguously.

The `submit_work` tool was not defined anywhere in the active codebase (it only existed in `tempbackup/`). This plan migrates all signaling from JSON text blocks to the `submit_work` tool call.

---

## What Has Already Been Done (Before This Session)

1. **`app/agent/tools.py`**: Added `submit_work()` function definition that returns JSON with `__maestro_terminal__` marker. Registered in `TOOL_REGISTRY` and added full schema to `TOOL_SCHEMAS` with description emphasizing tool call over JSON text.
2. **`app/agent/config.py`**: Added `submit_work` to `INDEV_AGENT_TOOLS` list (controls which tools are available to the LLM).
3. **`app/agent/loop.py`** (MaestroLoop):
   - Added `_terminal_signal: dict | None = None` to `__init__`
   - Added `__maestro_terminal__` detection in `_handle_tool_calls()` with `logger.INFO` on successful submit_work
   - Added `logger.WARNING` when JSON signals are detected in text content (to flag agents not using the tool)
   - Updated nudge message to reference `submit_work` tool call
   - Added check in `_loop()` to return `_handle_terminal(self._terminal_signal)` when `_terminal_signal` is set after tool calls

---

## What Remains To Be Done

### 1. Update ComponentLoop (app/agent/component_loop.py)

ComponentLoop uses **substring-based signal detection** (`'"signal": "ACCEPTED"' in content`) instead of JSON parsing or tool call detection. It needs the same `submit_work` tool call detection as MaestroLoop.

**Changes needed:**
- Add `_terminal_signal: dict | None = None` to `__init__` (if not already present)
- In `_handle_tool_calls` (or equivalent), detect `submit_work` tool calls by checking if `name == "submit_work"` and then parsing the result content for `__maestro_terminal__`
- After tool call processing, check `self._terminal_signal` and call the loop's terminal handler (e.g., `_handle_accepted` or similar)
- Update the nudge message to reference `submit_work` instead of "output a JSON block"
- Consider keeping the substring-based detection as a fallback with a warning log (for agents that haven't been updated yet)

### 2. Update System Prompt (app/agent/system_prompt.py)

Currently contains instructions in **Section 5** and **Section 6** that tell agents to output JSON signal blocks as free-form text. Replace these with instructions to use the `submit_work` tool call.

**Example replacement:**
```
# Old (Section 5 - Terminal Action)
When you are ready to complete, output this JSON block:
{"signal": "ACCEPTED", "summary": "..."}

# New (Section 5 - Terminal Action)
When you are ready to complete, call the submit_work tool:
submit_work(signal="ACCEPTED", summary="...")
```

### 3. Update Agent-Specific Prompts

Each agent prompt file has instructions telling agents to "output ONLY JSON" or similar. These need to reference `submit_work` tool calls instead.

**Files to update:**
- **`app/agent/intake.py`** — Has "Respond ONLY with JSON" instructions for 3 stages. Replace with submit_work tool call instructions.
- **`app/agent/planning.py`** — Multiple "Output JSON" instructions throughout. Replace with submit_work tool call instructions.
- **`app/agent/research.py`** — Has "output ONLY this JSON" instructions. Replace with submit_work tool call instructions.
- **`app/agent/subdivide.py`** — Has "output ONLY this JSON object" instructions. Replace with submit_work tool call instructions.
- **`app/agent/dreamer.py`** — Has "Output JSON only" instructions. Replace with submit_work tool call instructions.
- **`app/agent/full_review.py`** — Has "Output JSON" instructions. Replace with submit_work tool call instructions.
- **`app/agent/conceptual_review.py`** — Has "Output JSON" instructions. Replace with submit_work tool call instructions.
- **`app/agent/security_review.py`** — Has "Output JSON" instructions. Replace with submit_work tool call instructions.

**Important:** Not all agents use `submit_work` for terminal signaling. Some agents (like intake, planning, research) may produce intermediate results that feed into other stages. Only agents that can *terminate* their loop iteration should use `submit_work(signal="ACCEPTED")`. Review each agent to determine if it's a terminal agent or an intermediate stage agent.

### 4. Run Tests

After all changes are complete, run the test suite to verify:
```
D:\workspace\TheMaestro\venv\Scripts\python.exe app\tests\test_submit_work_terminal.py
```

---

## Key Patterns to Follow

### Tool Call Detection (in loops)
```python
if name == "submit_work":
    try:
        terminal_data = json.loads(result_content)
        if terminal_data.get("__maestro_terminal__") is True:
            logger.info("submit_work tool call — signal=%s, summary='%s'",
                        terminal_data.get("signal"),
                        terminal_data.get("summary", "")[:120])
            self._terminal_signal = terminal_data
    except (json.JSONDecodeError, ValueError):
        pass
```

### Terminal Signal Check (in loop body, after tool calls)
```python
if self._terminal_signal is not None:
    sig = self._terminal_signal.get("signal")
    if sig in (SIGNAL_ACCEPTED, SIGNAL_REVERT):
        return self._handle_terminal(self._terminal_signal)
```

### Prompt Instructions (in agent prompts)
```
# Old
Output this JSON:
{"signal": "ACCEPTED", "summary": "Done"}

# New
Call the submit_work tool to complete:
submit_work(signal="ACCEPTED", summary="Done")
```

---

## File System State (Current)

- **MODIFIED**: `app/agent/tools.py` — Added `submit_work()` function, registered in TOOL_REGISTRY and TOOL_SCHEMAS
- **MODIFIED**: `app/agent/config.py` — Added `submit_work` to INDEV_AGENT_TOOLS
- **MODIFIED**: `app/agent/loop.py` — Added `_terminal_signal` tracking, detection in `_handle_tool_calls`, warning logs for JSON signals in text, updated nudge, added terminal signal check after tool calls
- **NOT YET MODIFIED**: `app/agent/component_loop.py`
- **NOT YET MODIFIED**: `app/agent/system_prompt.py`
- **NOT YET MODIFIED**: `app/agent/intake.py`, `planning.py`, `research.py`, `subdivide.py`, `dreamer.py`
- **NOT YET MODIFIED**: `app/agent/full_review.py`, `conceptual_review.py`, `security_review.py`
