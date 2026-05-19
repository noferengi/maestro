# Gap 10 — Multi-model routing by task type

**Status:** Planning  
**Effort:** Small-Medium  
**Priority:** Medium — reduces cost and latency; enables model specialization

## Problem

The scheduler dispatches tasks to LLM endpoints based on capacity (free slots) and
project configuration (project.llm_id). There is no dispatch logic based on what kind
of task it is. A file summary job and a formal proof attempt both go to the same
endpoint. A simple formatting correction and a complex architectural decision consume
the same model. 

As the model ecosystem diversifies — local Qwen for fast cheap work, Claude Sonnet for
deep reasoning, Claude Opus for novel problem solving, a code-specialized model for
pure implementation — routing decisions matter both for quality and for cost.

## Rough phases

1. Capability declarations on LLM endpoints
2. Task classification — determining what kind of work a task represents
3. Routing policy — the rules that map task type to model requirements
4. Fallback behavior — what happens when the preferred model is unavailable
5. Cost accounting — tracking spend by model type across task types

## Open questions

### Capability declarations
- The `llms` table currently has: address, port, model name, cost rates, context window,
  compute_node_id. What capability fields need to be added? Candidates: `strengths`
  (JSONB list of tags: "reasoning", "code", "math", "fast", "long_context"),
  `max_reasoning_tokens`, `supports_tools` (bool), `supports_vision` (bool).
- Should capabilities be declared manually by the operator, auto-detected by probing
  the endpoint at startup, or derived from the model name via a lookup table?
- Should cost weighting be part of routing (prefer cheaper models when quality
  requirements are lower), or should routing be purely quality-based with cost tracked
  separately?

### Task classification
- How is a task classified at dispatch time? Options: stage_key (all `planning` tasks
  need reasoning, all `indev` tasks need code), agent_type from the pipeline template,
  a classifier LLM call (expensive), or tags on the task card.
- Should classification be done once when the task is created, or re-evaluated at each
  stage transition (a task that starts as a novel research problem may become a
  straightforward implementation task)?
- Who sets classification tags? Human when creating the card, Maestro during intake,
  or inferred automatically from the pipeline stage config?

### Routing policy
- Should the routing policy be a declarative config (in `maestro.ini` or the DB), a
  per-pipeline-template setting, or a per-project setting?
- Example policy questions: does a `security` stage always prefer a specific model?
  Does a `human_review` stage (which needs no model) correctly short-circuit routing?
  Do file summary jobs always use the cheapest available model?
- Should routing be hard (task waits for the preferred model to free up) or soft
  (prefer the best model but fall back immediately if it's busy)?

### Fallback behavior
- If the preferred model endpoint is down or at capacity, what are the fallback options?
  Accept any available endpoint? Accept only endpoints tagged with overlapping capabilities?
  Queue and wait?
- The current endpoint backoff system (`_EndpointState` in `llm_client.py`) tracks
  failure states. Should routing-by-capability interact with backoff state, or are
  they independent?
- Should there be a "minimum acceptable model" concept — a task that requires reasoning
  would rather wait than be dispatched to a model that can't do it?

### Multi-model within a single task
- Should different *stages* of the same task be allowed to use different models? A task
  could use a fast cheap model for research, a reasoning model for planning, and a
  code-specialized model for implementation.
- If yes: does the pipeline template declare per-stage model preferences, or is it
  handled dynamically by the routing policy at dispatch time?
- If a task's `llm_id` is set at creation (project default), does that override
  routing policy or does routing policy override the task-level setting?
