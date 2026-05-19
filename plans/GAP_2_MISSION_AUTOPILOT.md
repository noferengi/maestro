# Gap 2 — Mission-driven autopilot (closed-loop proactive orchestration)

**Status:** Planning  
**Effort:** Medium  
**Priority:** High — unlocks research, story writing, math exploration autonomously

## Problem

Maestro currently fires only on stalls (no pipeline activity for `MAESTRO_STALL_TICKS`
consecutive ticks). It can propose new cards in survey mode but only reactively. It
cannot initiate a project from scratch, pursue a stated goal across multiple ticks, or
drive work to completion without a human creating the first card.

## Rough phases

1. Mission storage — where and how a mission is defined and edited
2. Trigger mechanism — cron vs. idle detection vs. augmenting the existing stall path
3. Progress evaluation — what signal tells Maestro it is advancing toward the goal
4. Card creation guardrails — preventing board flooding
5. Multi-mission and multi-project coordination

## Open questions

### Mission storage
- Where does the mission live? Options: free-text field on `Project`, a `SystemSetting`
  row, a new `autopilot_objectives` table, or a pinned card of a special type.
- Is the mission per-project or global? Can a single project have multiple concurrent
  missions with priorities?
- How is the mission edited? UI field on the project settings page, a dedicated
  `/api/projects/{name}/mission` endpoint, or only via a Maestro tool call?

### Trigger mechanism
- Should the mission-driven tick replace the existing stall trigger, run in parallel,
  or only activate when no pipeline activity is occurring anyway?
- What cadence? Fixed cron interval per project, adaptive (backs off when the board is
  full, accelerates when it's empty), or triggered by pipeline stage completions?
- Does mission autopilot consume its own LLM budget allocation or share with task agents?
  If the board is already saturated with active tasks, should the mission tick be
  suppressed entirely?

### Progress evaluation
- What signals indicate the mission is advancing? Options: stage distribution shift,
  completed task count, document store content, git commit history, or an explicit
  LLM self-assessment call each tick.
- How does Maestro detect that it is spinning in place (creating cards that always fail
  the same way) vs. making real forward progress?

### Card creation guardrails
- Current maximum: 3 new cards per Maestro run. Should mission mode have a different
  budget, and should it be able to create cards directly at `planning` or `indev` stages
  to fast-path its own ideas, or must everything start at `idea` and go through intake?
- What is the maximum number of in-flight cards before the autopilot suppresses itself?

### Shutdown conditions
- What turns the autopilot off? Options: mission marked complete by Maestro, human
  toggles it off, budget exhausted, board filled with failed cards beyond a threshold.
- If the mission is open-ended ("always improve test coverage"), does it run forever?
  Should missions have an explicit time-box or completion criterion field?
