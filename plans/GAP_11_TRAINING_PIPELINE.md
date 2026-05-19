# Gap 11 — Training data pipeline (closing the weight update loop)

**Status:** Planning  
**Effort:** Medium  
**Priority:** High — this is the mechanism by which the system actually improves the
underlying models, not just the scaffolding around them

## Problem

The `budget_entries` table contains 60,000+ full prompt/response/tool-call sequences
from real autonomous work sessions spanning months of home generation. This is some
of the highest-quality training data available anywhere: real multi-turn agentic
reasoning, real tool use under constraint, real failure modes and self-corrections,
real formal proofs, real code that passes tests. It is currently used only for cost
tracking and diagnostic replay.

Model weights are not frozen. Every major lab ships new versions weekly. The question
is not whether the data can influence future model training — it can — but how to
extract, filter, format, and route it so that the feedback loop is tight and the
quality signal is clear.

## Rough phases

1. Extraction — selecting which entries are training-worthy
2. Formatting — converting budget_entry records to training formats
3. Quality filtering — separating signal from noise
4. Routing — which providers receive which data and how
5. Feedback loop closure — measuring whether model improvements appear in future sessions

## Open questions

### Extraction
- Which entries are worth extracting? Not all 60,000 are equal. High-signal candidates:
  - Sessions that ended in `submit_work(ACCEPTED)` with a passing test suite
  - Sessions that demonstrated successful failure recovery (demotion → retry → success)
  - Sessions that produced formal proofs verified by SymPy or Lean4
  - Planning sessions that generated interface contracts later validated by implementation
  - Research sessions whose findings were cited in downstream task resolutions
- Low-signal or harmful candidates to exclude: sessions with `finish_reason=length`
  (truncated, incomplete reasoning), sessions that hallucinated file paths or function
  names that don't exist, sessions that passed tests by deleting them.
- How is "high-signal" detected automatically? Options: join with task outcomes (did
  the task eventually reach `completed`?), finish_reason filtering, manual annotation,
  or a classifier model that scores session quality.

### Formatting
- Anthropic's fine-tuning format vs. OpenAI's vs. Hugging Face conversational format —
  which is the target? All three have different schemas for multi-turn tool-use
  conversations.
- `budget_entries` stores delta prompt messages (since migration 0076) — the full
  conversation must be reconstructed via `reconstruct_messages_for_entry()`. Does the
  export pipeline handle this reconstruction, or does it operate at the session level
  (grouping all entries with the same `session_id`)?
- Tool call sequences (assistant message with tool_calls → tool result message → next
  assistant message) are the most valuable part. How are these formatted in the target
  schema, especially for providers whose fine-tuning format doesn't natively support
  tool use?
- Should the exported data include the full system prompt, or is the system prompt
  stripped and only the user/assistant/tool sequence retained? The system prompt
  contains TheMaestro-specific context that may not generalize.

### Quality filtering
- Beyond selecting high-signal sessions, what content filters are needed?
  - Personally identifying information: are there any project names, file paths, or
    content in the sessions that should not be in training data?
  - Repetitive content: the same file summary prompt generated 10,000 times is noise.
    How is near-duplicate detection applied?
  - Length filtering: very short sessions (1-2 turns) and very long sessions (100+ turns
    with context degradation) may both be low-value. What are the bounds?
- Should filtered-out entries be marked in the DB so the same entries aren't evaluated
  again on the next export run?

### Routing
- Anthropic: is there a fine-tuning API or data submission pathway available? If not,
  the data contributes indirectly by being in public datasets or shared explicitly.
- Local models (Qwen 3.6 on the local inference hardware): Hugging Face training
  scripts exist. What hardware is available for a fine-tuning run? LoRA/QLoRA on the
  existing GPU, or does this require cloud compute?
- How often does a training run happen? After every N new high-quality sessions, on a
  weekly schedule, or manually triggered?
- Should the export be fully automated (scheduled job that exports, formats, and pushes
  to a training pipeline) or human-reviewed before each training run?

### Feedback loop closure
- How do you measure whether a training run actually improved model performance on
  Maestro tasks? Options: hold out a set of benchmark tasks and compare success rates
  before and after, track `finish_reason=length` rate (decreasing is good), track
  demotion frequency, or track tokens-to-completion on comparable tasks.
- Should benchmark tasks be synthetic (designed to test specific capabilities) or
  drawn from the same real task distribution as the training data?
- If a training run makes performance worse (regression), how is that detected and
  how is the previous model version restored?

### Privacy and data governance
- The budget_entries content includes code, descriptions, and reasoning from user
  projects. If Maestro is used for multiple users or projects that contain proprietary
  or sensitive information, what consent and anonymization is required before that
  data enters a training pipeline?
- Even for single-user home use: are there project contents in the sessions that the
  user would not want in a training dataset regardless?
- Should there be a per-project or per-session opt-out flag that prevents those entries
  from ever being included in exports?
