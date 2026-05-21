# Gap 8 — Real-time inter-agent messaging

**Status:** Complete ✓  
**Effort:** Small-Medium  
**Priority:** Medium — enables genuine collaborative reasoning between concurrent sessions

---

## Problem

Agent sessions communicate through shared database state: task history, documents,
PIPs, arch cards. Two agents working on related problems in parallel have no way to
consult each other directly. An agent that has solved a problem cannot share what it
learned with a sibling agent about to attempt the same thing.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Transport** | Synchronous sub-session call stack — no queue, no DB table, no sidecar. `ask_agent` spins up a fresh sub-session inline, awaits its completion, and returns the result as a tool result. Same pattern as ConsultAgent. |
| **LLM slot** | The sub-session uses the same LLM slot/priority as the parent. The parent is mid-turn; no other task can preempt it. From the parent's perspective the call is instantaneous (within the same logical turn). |
| **KV cache** | Before spawning any sub-session: `# TODO KV cache checkpoint` comment in code. Aspirational — someday the KV state at this point could be serialized to disk and restored after the sub-session completes, avoiding re-encoding the parent's context. |
| **Discovery** | `list_active_sessions()` tool returns all currently running sessions across all projects (session_id, task_id, task_title, agent_type, project_name). Agents address targets by session_id or task_id. |
| **Deadlock prevention** | `_ask_depth` counter in session state. Each hop increments it. When `_ask_depth >= ask_max_depth` (default 3), `ask_agent` returns an error: "Max inter-agent ask depth reached — make your best judgment." No DFS cycle detection needed; depth cap makes cycles terminate. |
| **Message persistence** | Sub-session budget entries are charged to the calling task's budget, tagged `role=inter_agent`. Messages are not stored separately — they appear in the budget trace naturally. |

---

## Implementation plan

### Phase 1 — `ask_agent` tool

**`app/agent/tools.py`** — register:

```python
"ask_agent": {
    "fn": handle_ask_agent,
    "schema": {
        "name": "ask_agent",
        "description": (
            "Ask another running agent session a question and receive its answer inline. "
            "The other agent runs a fresh reasoning session with your question and returns "
            "a direct answer. This is a blocking call — your session waits for the reply. "
            "Use list_active_sessions() first to find the right target."
        ),
        "parameters": {
            "target_session_id": {
                "type": "string",
                "description": "Session ID of the agent to ask. Use list_active_sessions() to find it."
            },
            "question": {
                "type": "string",
                "description": "The question or request to send to the other agent."
            }
        },
        "required": ["target_session_id", "question"]
    }
}
```

**`handle_ask_agent(target_session_id, question, session_id, task_id, ask_depth, db, settings)`:**

```python
def handle_ask_agent(target_session_id, question, session_id, task_id, ask_depth, db, settings):
    if ask_depth >= settings.ask_max_depth:
        return (
            f"Max inter-agent ask depth ({settings.ask_max_depth}) reached. "
            "Make your best judgment with the information available."
        )

    target = _resolve_target_session(target_session_id, db)
    if target is None:
        return f"Session '{target_session_id}' is not active. Use list_active_sessions() to see current sessions."

    # TODO KV cache checkpoint: if KV serialization were implemented,
    # the parent session's context would be written to disk here, allowing
    # the parent's prompt prefix to be restored from cache after this call.

    answer = InterAgentSession(
        question=question,
        target_task_id=target.task_id,
        calling_task_id=task_id,
        calling_session_id=session_id,
        ask_depth=ask_depth + 1,
        db=db,
        settings=settings,
    ).run()

    return answer
```

---

### Phase 2 — InterAgentSession

**`app/agent/inter_agent_session.py`** — new file:

```python
class InterAgentSession:
    """
    A slim, single-purpose LLM session that answers one question from a peer agent.
    Runs synchronously inline with the calling session's LLM slot.
    """

    def __init__(self, question, target_task_id, calling_task_id, calling_session_id,
                 ask_depth, db, settings):
        self.question = question
        self.target_task_id = target_task_id
        self.calling_task_id = calling_task_id
        self.calling_session_id = calling_session_id
        self.ask_depth = ask_depth  # passed through so recursion cap applies
        self.db = db
        self.settings = settings

    def run(self) -> str:
        context = self._build_context()
        # Short multi-turn session: max 5 turns (same as ConsultAgent)
        # ask_depth is threaded through session state so nested ask_agent calls
        # see the correct depth.
        answer = self._run_llm_session(context)
        return answer

    def _build_context(self) -> dict:
        target_task = get_task(self.target_task_id, self.db)
        return {
            "target_task_description": target_task.description,
            "target_task_stage": target_task.type,
            "calling_task_id": self.calling_task_id,
            "question": self.question,
        }

    def _system_prompt(self) -> str:
        return (
            "A peer agent has asked you a question about your current work. "
            "Answer concisely and directly. If you need to look at your work product "
            "to answer accurately, use your read tools. Do not take actions or modify "
            "state — only answer the question."
        )
```

Budget entries are tagged:
```python
budget_entry.metadata = {
    "role": "inter_agent",
    "calling_session_id": self.calling_session_id,
    "ask_depth": self.ask_depth,
}
```

---

### Phase 3 — `list_active_sessions` tool

**`app/agent/tools.py`** — register:

```python
"list_active_sessions": {
    "fn": handle_list_active_sessions,
    "schema": {
        "name": "list_active_sessions",
        "description": (
            "List all currently running agent sessions across all projects. "
            "Returns session ID, task ID, task title, agent type, and project name. "
            "Use to find a target before calling ask_agent()."
        ),
        "parameters": {
            "project": {
                "type": "string",
                "description": "Optional — filter to sessions in this project only."
            }
        },
        "required": []
    }
}
```

`handle_list_active_sessions` reads from the scheduler's `_active_sessions` dict via a
thread-safe read (the dict is already protected by the scheduler lock; use `RLock`
acquisition for the read). Returns a list of:

```python
{
    "session_id": "...",
    "task_id": 42,
    "task_title": "Implement auth module",
    "agent_type": "custom_llm",
    "project": "Garden",
    "stage_key": "INDEV"
}
```

Excludes the calling session from the results (an agent cannot ask itself).

---

### Phase 4 — `ask_depth` threading through MaestroLoop

**`app/agent/maestro_loop.py`** — add `ask_depth: int = 0` to session state. When
`handle_ask_agent` is called, it receives `ask_depth` from the current session's state.
The `InterAgentSession` passes `ask_depth + 1` into its own session state so that any
`ask_agent` call made within the sub-session sees the correct depth.

**`maestro.ini`** — add under `[orchestration]`:
```ini
ask_max_depth = 3
```

`ask_max_depth = 3` allows: caller → A → B → "depth exceeded", so two hops of useful
collaboration before the cap fires.

---

### Phase 5 — Tool availability

Both `ask_agent` and `list_active_sessions` are:
- Available to all non-Maestro agent types (inner agents, custom_llm, etc.).
- **Not** available to Maestro's own sessions — Maestro already has `consult_maestro`
  for escalation and should not be askable as a peer.
- Not available to `reflection` stage agents (reflection is read-only; asking peers would
  change the scope of its review).
- Excluded from file summary and other mechanical stage agents.

---

### Phase 6 — Tests

1. **Unit** — `handle_ask_agent`: depth cap fires at `ask_max_depth`; returns readable error.
2. **Unit** — `handle_ask_agent`: inactive target_session_id returns clear error without crashing.
3. **Unit** — `handle_list_active_sessions`: calling session excluded from results; project filter works.
4. **Unit** — `InterAgentSession._build_context`: correct task description and stage injected.
5. **Unit** — budget entries from sub-session tagged `role=inter_agent` with correct `calling_session_id`.
6. **Integration** — two-agent ask: agent A calls `ask_agent` targeting a mock agent B session; B's answer appears as A's tool result; budget trace shows both entries.
7. **Integration** — depth cap: chain of 4 ask_agent calls; the 4th returns the cap error without crashing or hanging.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/agent/inter_agent_session.py` | **New file** — InterAgentSession class |
| `app/agent/tools.py` | Register `ask_agent` and `list_active_sessions` |
| `app/agent/maestro_loop.py` | `ask_depth` in session state; thread through to handler |
| `app/agent/config.py` | Read `ask_max_depth` from `[orchestration]` |
| `maestro.ini` | `ask_max_depth = 3` under `[orchestration]` |
| `app/tests/test_inter_agent.py` | **New file** — all tests for this gap |

---

## KV cache note (future work)

The `# TODO KV cache checkpoint` comment marks the exact point in `handle_ask_agent`
where the parent session's KV state would be serialized before branching. If a future
version of `llama.cpp` or an API endpoint exposes KV state save/restore, this is the
integration point. The comment will appear in every git diff that touches this function,
keeping the intent visible as the codebase evolves.

---

## Acceptance criteria

- [x] `ask_agent(target_session_id, question)` runs a sub-session on the same LLM slot and returns the answer as a tool result.
- [x] Depth cap fires at `ask_max_depth` and returns a readable error; no crash, no hang.
- [x] `list_active_sessions()` excludes the calling session and respects the project filter.
- [x] Sub-session budget entries appear in the calling task's budget trace tagged `agent_name="InterAgentSession"`.
- [x] `# TODO KV cache checkpoint` comment is present at the spawn point in `handle_ask_agent`.
- [x] `ask_agent` is excluded from Maestro, reflection, and mechanical stage agents.
- [x] All new code passes existing test suite with no regressions (951 passed).
