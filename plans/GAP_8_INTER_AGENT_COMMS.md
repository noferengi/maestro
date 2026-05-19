# Gap 8 — Real-time inter-agent messaging

**Status:** Planning  
**Effort:** Medium  
**Priority:** Medium — enables genuine collaborative reasoning between concurrent sessions

## Problem

Agent sessions communicate through shared database state: task history, documents,
PIPs, arch cards. This is asynchronous, coarse-grained, and mediated by the scheduler.
Two agents working on related problems in parallel have no way to consult each other
directly. An agent that has solved a problem cannot proactively share what it learned
with a sibling agent that is about to attempt the same thing.

The park/unpark mechanism (`launch_research_agent`) approximates blocking inter-agent
communication but requires going through the scheduler, creating a new DB job, and
waiting for the scheduler's dispatch cycle. It is not suitable for the kind of rapid
back-and-forth that produces useful collaborative reasoning.

## Rough phases

1. Transport layer — how messages move between sessions
2. Message schema — what a message contains and what responses look like
3. Agent tool API — how an agent sends a message and receives a reply
4. Session discovery — how an agent finds out what other sessions are running
5. Backpressure and safety — preventing message storms and deadlocks

## Open questions

### Transport layer
- Options in roughly increasing complexity: (a) a new `agent_messages` DB table polled
  every N seconds, (b) an in-process queue using Python `threading.Queue` keyed by
  session ID since all agents run in the same process, (c) Redis pub/sub as a sidecar,
  (d) a lightweight Unix domain socket server.
- The in-process queue is the lowest friction option given the single-process deployment.
  Is there a deployment scenario (multiple workers, multi-host) where in-process queuing
  would break?
- Should messages survive server restarts? If the server restarts mid-conversation
  between agents, should pending messages be replayed or dropped?

### Message schema
- Minimum viable: `{from_session_id, to_session_id, message_type, content, timestamp}`.
- What message types are needed initially? Candidates: `question` (async ask),
  `answer` (reply to question), `broadcast` (one-to-many notification),
  `yield` (one agent tells another it's releasing a resource or finding).
- Should message content be free-text, structured JSON, or both?
- Should messages be persisted to `budget_entries` so they appear in the budget history
  and training data pipeline?

### Agent tool API
- Two models: (a) `send_message(to_session_id, content)` is fire-and-forget, agent
  later calls `check_messages()` to poll; (b) `ask_agent(to_session_id, question)` is
  a blocking call that parks this session's LLM slot and resumes with the answer.
- The blocking model is much more useful for collaborative reasoning but requires
  careful deadlock prevention (agent A blocking on agent B which is blocking on agent A).
- Should agents be able to message by session ID only, or also by task ID or agent type
  (e.g., "send to whoever is currently working on the security review of task X")?

### Session discovery
- How does an agent know what other sessions are running? A new tool
  `list_active_sessions()` that returns current session IDs, task IDs, agent types,
  and project names?
- Should session visibility be scoped to the same project, or can agents across
  projects message each other?
- The `_active_sessions` dict in the scheduler already tracks running sessions. Can
  the tool layer read from it safely without holding the scheduler lock?

### Backpressure and safety
- What prevents an agent from flooding another with messages? Rate limit per sender?
  Max queue depth per recipient before messages are dropped?
- Deadlock: agent A asks agent B, which asks agent A. Detection and prevention strategy?
- Should the Maestro orchestrator be able to intercept or moderate messages between
  inner agents, or is it peer-to-peer without oversight?
- If a recipient session ends before it processes a message, what happens to the
  sender that is blocked waiting for a reply?
