# Gap 1 — Fix `consult_maestro` (broken escalation tool)

**Status:** Planning  
**Effort:** Small  
**Priority:** Highest — blocks every other capability

## Problem

`consult_maestro` exists in `TOOL_SCHEMAS` and `TOOL_CATEGORIES` but is absent from
`TOOL_REGISTRY`. Any agent that calls it receives `ERROR: Unknown tool`. Inner agents
have no working path to escalate architectural ambiguity or repeated failures upward
to the orchestrator.

## Rough phases

1. Register `consult_maestro` in `TOOL_REGISTRY`
2. Define what the signal does when received by the loop
3. Define the response path back into the agent session
4. Persistence and restart survival
5. Decide whether it should be terminal or blocking-resumable

## Open questions

### Signal semantics
- The function currently returns `{"__maestro_terminal__": True, "signal": "CONSULT", ...}`.
  Does `MaestroLoop` intercept that and pause, or does it treat it as any terminal and
  exit the session? If the loop exits, who reads the question and how does the answer
  get back?
- Should `consult_maestro` be a **non-terminal blocking tool** — agent parks, Maestro
  answers, agent resumes with the answer inline — or always a terminal escalation that
  ends the session?

### Who answers?
- Option A: lands in the inbox, human responds via UI, answer is injected as task history
  and the task is re-dispatched.
- Option B: Maestro orchestrator picks it up on its next tick and answers autonomously.
- Option C: a dedicated `ConsultAgent` fires immediately (same LLM slot) to answer
  before the session dies.
- Which model, and does it vary by the severity of the question?

### Timeout and fallback
- If no answer arrives within N minutes, what happens? Options: retry from scratch,
  demote the task, leave it in a `waiting_consult` state, or just wait indefinitely.
- Should there be a maximum number of consult calls per session to prevent loops?

### Persistence
- If the server restarts mid-consultation, should the unanswered question survive?
- Is the task history sufficient for this, or does it need a dedicated `consultations`
  table with status tracking?

### Scope of the registry fix
- Is the fix just adding one line to `TOOL_REGISTRY`, or does `consult_maestro` also
  need to be added to the per-stage `allowed_tools` lists in `custom_agent_definitions`
  before inner agents can actually call it?
- Should it appear in the pipeline editor UI's tool picker?
