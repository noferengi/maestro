# Gap 6 — Reflection agent (skeptical verification before commit)

**Status:** Planning  
**Effort:** Medium  
**Priority:** High — directly addresses long-chain reasoning drift

## Problem

LLM outputs are generated in a single forward pass with no internal review. An agent
that writes 200 lines of code and calls `submit_work` has never re-read its own output
critically. Tests catch functional failures but not logical errors, wrong assumptions,
missed edge cases, or subtly incorrect reasoning that happens to produce passing output.
The architecture already handles unreliability at the system level (retries, demotions,
PIPs) but not at the output level before it enters the pipeline.

A separate invocation that reads the output skeptically — with a different framing,
possibly a different model — catches a class of errors that neither tests nor human
review reliably catch because both tend to read charitably.

## Rough phases

1. Reflection invocation — when it fires and what it receives
2. Structured uncertainty output — machine-readable confidence alongside prose
3. Scheduler integration — what happens on reflection failure
4. Model selection — same model, different model, or smaller/faster model
5. Reflection scope — code, proofs, plans, or all output types

## Open questions

### When does reflection fire?
- After every `submit_work` call regardless of signal, or only for specific signals
  (ACCEPTED) and not others (REVERT_TO_DESIGN, which is already an admission of failure)?
- Per-stage or only at specific pipeline gates? Reflection on every `indev` output is
  expensive. Reflection only at `final_review` may be too late.
- Should reflection be a stage in the pipeline (a node in the template) or a system-level
  hook that fires outside the pipeline regardless of template configuration?
- Can the pipeline editor enable/disable reflection per stage? If so, what is the default?

### What does the reflection agent receive?
- The full task history including all tool calls, or just the final output?
- The original task description and intent, so it can check whether the output actually
  addresses the goal?
- The test results (pass/fail + output), so it can reason about whether the tests are
  testing the right things?
- Access to tools, or is it read-only? A reflection agent with write access could fix
  what it finds; without write access it can only flag.

### What does the reflection agent produce?
- A boolean pass/fail that blocks or allows stage transition?
- A structured report: `{"confidence": 0.0–1.0, "issues": [...], "uncertain_about": [...]}`
  that the scheduler can query programmatically?
- A free-text finding that's injected into the task history before the next stage?
- All of the above at different severity tiers (minor note vs. blocking finding)?

### Model selection
- Should the reflection agent use the same model that produced the output, a different
  model, or a smaller/faster model for routine reflection with the full model only on
  escalation?
- Cross-model reflection (Qwen reviews Claude's output, Claude reviews Qwen's) may
  catch model-specific blindspots. Is that worth the complexity?
- Should the reflection model be configurable per project or per stage, or is it
  always a system-level setting?

### Failure handling
- If reflection fails (issues found), does the task get demoted, retried at the same
  stage with the reflection findings injected as feedback, or sent to a correction agent?
- How many reflection failures before the task is considered stuck and escalated?
- Should a passing reflection score contribute to the task's confidence record for
  future Maestro decisions?

### Structured uncertainty as a first-class output
- Beyond reflection, should every agent output include a structured confidence block
  as part of the `submit_work` payload?
- What schema? Minimum viable: `{"confidence": float, "blockers": [str], "assumptions": [str]}`.
- How does the scheduler consume this? Route low-confidence outputs to reflection
  automatically, or surface the uncertainty to the human_review stage?
