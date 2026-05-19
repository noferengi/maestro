# Gap 9 — Event-driven triggers (world hooks, reactive dispatch)

**Status:** Planning  
**Effort:** Medium  
**Priority:** Medium — required for Maestro to respond to the world rather than just its own state

## Problem

The scheduler operates on a fixed tick cycle. Maestro fires when stall ticks accumulate.
Everything is poll-based and internally driven. The system cannot react to external
events: a git push, a failing CI run, a new arXiv paper matching a watched query, a
user message in Slack, a file appearing in a watched directory, a webhook from GitHub.

A system that can only observe its own DB state is fundamentally limited in what it
can autonomously pursue. Research tasks that need fresh data, software tasks triggered
by external PRs, story seeds triggered by news events — none of these are reachable
from a pure polling architecture.

## Rough phases

1. Event source taxonomy — what kinds of events are worth supporting
2. Registration model — how Maestro declares what it's watching for
3. Dispatch model — what happens when an event fires
4. Deduplication and rate limiting — preventing event storms
5. Failure handling — what happens when an event source becomes unavailable

## Open questions

### Event source taxonomy
- Which event sources are worth supporting first? Candidates by implementation cost:
  - **File system watcher** (low cost, local only) — a path is watched; when a file
    appears or changes, a Maestro session fires
  - **Git webhook** (medium cost) — push to a branch triggers a pipeline run on the
    affected project
  - **HTTP webhook receiver** (medium cost) — a generic inbound endpoint that maps
    payload fields to Maestro task creation
  - **Cron/scheduled** (already partially exists via `FactoryRun` cron triggers) —
    extend the existing cron mechanism to trigger Maestro directly
  - **API polling** (medium cost) — periodically fetch a URL and fire if content changed
    (arXiv search, RSS feed, GitHub issue list)
  - **Slack/Discord** (high cost, external dependency) — message in a channel creates
    a task
- Should the first implementation cover only one source type as a proof of concept, or
  build a generic event routing layer that all source types plug into?

### Registration model
- How does Maestro declare what it's watching? Options: a `watched_events` table in
  the DB (event_type, filter_config, target_project, action_type), a config section in
  `maestro.ini`, or a Maestro tool call (`register_watch(event_type, config)`) during
  a session.
- Should watches be scoped to a project, a pipeline template, or system-wide?
- Who creates watches? Human via UI, Maestro autonomously (a session registers a watch
  and exits, the watch fires future sessions), or both?
- Should watches have expiry conditions (fire N times, expire after date, cancel when
  a task completes)?

### Dispatch model
- When an event fires, what does it create? Options: a new idea card in a specified
  project, a direct Maestro session invocation (bypassing the stall trigger), a
  research job, or a custom pipeline stage trigger.
- Should the event payload be injected into the created card's description, the research
  question, or the Maestro system prompt for that session?
- Can one event fire multiple actions (create a card AND trigger a Maestro survey)?

### Deduplication and rate limiting
- If a file watcher fires 50 times in a second (a git checkout touching many files),
  how is that collapsed to one event?
- If an arXiv poll finds 12 new papers matching a query, does it create 12 cards or one
  card with a summary of all 12?
- Should there be a minimum interval between firings of the same watch regardless of
  how many underlying events occurred?

### Failure handling
- If a watched URL becomes unavailable, does the watch fail silently, log an error,
  or create an inbox notification?
- Should failed event sources be automatically retried with backoff, or should a human
  be notified to review the watch configuration?
- If the event dispatcher itself crashes (process restart), are in-flight events
  replayed or dropped? Does this require a persistent event queue?
