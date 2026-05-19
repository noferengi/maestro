# Gap 7 — Semantic episodic memory (what was tried, what worked, what didn't)

**Status:** Planning  
**Effort:** Medium-Large  
**Priority:** High — prevents the system from repeating failed approaches across sessions

## Problem

Agents communicate through artifacts: task history, documents, arch cards, budget entries.
What they cannot do is ask "has anyone tried approach X on this class of problem before,
and what happened?" The document store holds conclusions. The budget entries hold raw
prompt/response pairs. Neither is indexed for semantic retrieval of past *process* —
the reasoning, dead ends, and pivots that led to an outcome.

Without episodic memory, the system rediscovers the same failures repeatedly. A task
demoted three times for the same reason will be retried a fourth time by a fresh agent
session that has no memory of the first three attempts beyond the demotion note in the
task history.

## Rough phases

1. Storage backend — vector store selection and integration with existing PostgreSQL
2. What gets embedded — events, summaries, or full sessions
3. Write path — when and what triggers embedding
4. Read path — how agents query, what they receive
5. Staleness and eviction — when memories become misleading

## Open questions

### Storage backend
- `pgvector` extension on the existing PostgreSQL instance is the lowest-friction option
  (no new service, same credentials, same backup). Alternatives: Chroma, Qdrant, or
  Weaviate as a sidecar service. The tradeoff is operational complexity vs. query
  expressiveness (filtering by project, task type, date, etc. alongside cosine distance).
- Is the current PostgreSQL instance available for DDL additions (adding `pgvector`
  extension requires superuser)? If not, a sidecar vector store is the path.
- Should the vector store be a separate table in the same DB, a separate schema, or a
  completely separate service?

### What gets embedded?
- Full budget entry prompt/response pairs — very large, expensive, but maximally rich.
- Per-session summaries generated at session end — smaller, curated, but requires an
  extra LLM call per session.
- Demotion and failure events specifically — small, high-signal, easy to filter.
- Document store writes — captures conclusions but not process.
- A combination: embed failure events immediately (cheap, high signal) and session
  summaries lazily (expensive, comprehensive)?
- What is the embedding model? Local (sentence-transformers via subprocess), or an
  API call to an embedding endpoint? Is there an embedding endpoint already configured
  in the LLM table?

### Write path
- When does embedding happen? Options: synchronous at session end (blocks teardown),
  asynchronous background job (a new job type like FileSummaryJob), or batched nightly.
- What triggers it? Every session completion, only demotions and failures, only when
  a `submit_work(ACCEPTED)` is called?
- Who pays the budget? File summary jobs use the project's budget. Should episodic
  memory embedding use the same budget, a dedicated system budget, or be zero-cost
  if a local embedding model is used?

### Read path
- How does an agent query episodic memory? A new tool (`query_episodes(question, k=5)`)
  that returns the top-K semantically similar past events?
- What does the agent receive? A formatted summary of each retrieved episode:
  task title, stage, approach taken, outcome, date?
- Is the query automatic (injected into every session start like arch context) or
  on-demand (agent calls the tool when it decides it needs context)?
- Should Maestro query episodic memory during its survey phase and inject relevant
  past failures into the Decide prompt?

### Staleness and eviction
- When does an episode become misleading rather than helpful? A failure from 6 months
  ago on a codebase that has since been refactored may steer the agent wrong.
- Should episodes have a decay weight (recency bias in retrieval scoring)?
- Should episodes be invalidated when the relevant file or module is significantly
  rewritten (detected via git diff)?
- What is the retention policy? Keep all episodes forever, evict after N months,
  evict when the project is deleted?

### Relationship to budget_entries
- The 60,000+ existing budget_entries are the richest source of episodic data in the
  system. Should there be a one-time backfill that embeds a curated subset of existing
  entries to bootstrap the memory store?
- How do you select which entries to backfill? Only entries with `finish_reason=stop`
  (completed turns)? Only entries for tasks that eventually reached `completed` stage?
  Only entries where `agent_name` indicates a substantive agent (not file summaries)?
