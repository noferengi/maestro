# Gap 7 — Semantic episodic memory (what was tried, what worked, what didn't)

**Status:** COMPLETE ✓ (all 941 tests green, 2026-05-20)  
**Effort:** Medium-Large  
**Priority:** High — prevents the system from repeating failed approaches across sessions

---

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

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Storage** | `pgvector` extension on the existing PostgreSQL instance. Single `episodic_memory` table with a `vector` column. No new service. Same credentials, backup, and connection pool. |
| **What gets embedded** | (1) Failure events (demotions, revert votes, reflection blocking issues) — synchronous at event time. (2) Session-end summaries — LLM-generated 2–4 sentence summary via async job at session teardown. (3) Document store writes — embedded synchronously when `upsert_document` is called. |
| **Read path** | Top-3 episodes auto-injected into every session's system prompt based on task description similarity. On-demand `query_episodes(question, k)` tool available for deeper queries within a session. |
| **Staleness model** | Two independent scores: (1) **Relevance score** = cosine similarity × exponential recency decay — never renewed on access; newer episodes always rank higher for equivalent content. (2) **Keepalive date** starts at `created_at + 5 years`. Each time an episode is included in retrieval results, `expires_at` is extended by 14 days. When `expires_at` passes, the episode is hard-deleted by the nightly cleanup job. Episodes that stop being useful expire; episodes that keep appearing in results stay alive indefinitely. |

---

## Implementation plan

### Phase 1 — pgvector extension and schema

**Prerequisites:** pgvector must be installed in the PostgreSQL instance.

```sql
-- Requires superuser — run via MAESTRO_ADMIN_DATABASE_URL
CREATE EXTENSION IF NOT EXISTS vector;
```

**Migration** (`NNNN_episodic_memory.py`):

```sql
CREATE TABLE episodic_memory (
    id            SERIAL PRIMARY KEY,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    task_id       INTEGER NULL     REFERENCES tasks(id)   ON DELETE SET NULL,
    episode_type  TEXT    NOT NULL
                      CHECK (episode_type IN ('failure', 'session_summary', 'document')),
    content       TEXT    NOT NULL,    -- human-readable summary text (also what was embedded)
    embedding     vector(1536),        -- dimension matches embedding model output
    metadata      JSONB   NOT NULL DEFAULT '{}',
                  -- { stage_key, task_title, outcome, source_key, ... }
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ NOT NULL,  -- DEFAULT: created_at + INTERVAL '5 years'
    last_accessed TIMESTAMPTZ NULL
);

CREATE INDEX ON episodic_memory USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);
CREATE INDEX ON episodic_memory (project_id, expires_at);
CREATE INDEX ON episodic_memory (task_id);
```

The vector dimension (1536) matches OpenAI `text-embedding-3-small` and most comparable
local embedding models. If the configured model outputs a different dimension, the
migration must be updated before `CREATE TABLE` (the dimension is baked in at creation time).

**`app/agent/config.py`** — add embedding config:
```ini
[episodic_memory]
embedding_llm_id =          ; LLM record ID with embedding endpoint; if unset, use sentence-transformers fallback
decay_half_life_days = 90   ; exponential decay half-life for recency weighting
keepalive_extension_days = 14
auto_inject_k = 3           ; top-K episodes injected at session start
```

---

### Phase 2 — Embedding helper

**`app/agent/episodic_memory.py`** — new file:

```python
def embed_text(text: str, settings) -> list[float]:
    """
    Produce an embedding vector for text.
    Resolution: embedding_llm_id LLM endpoint → sentence-transformers fallback.
    Returns a float list of length matching the configured dimension.
    """

def insert_episode(
    project_id: int,
    task_id: int | None,
    episode_type: str,
    content: str,
    metadata: dict,
    db,
    settings,
) -> int:
    """Embeds content and inserts into episodic_memory. Returns episode id."""
    vec = embed_text(content, settings)
    expires_at = datetime.utcnow() + timedelta(days=5 * 365)
    # INSERT INTO episodic_memory ...
    return episode_id

def query_episodes(
    project_id: int,
    question: str,
    k: int,
    db,
    settings,
) -> list[dict]:
    """
    Returns top-K episodes by relevance score = cosine_similarity * recency_decay.
    Updates expires_at for each returned episode: expires_at = max(expires_at, now() + keepalive_extension).
    """
    vec = embed_text(question, settings)
    # SELECT ..., 1 - (embedding <=> %s) AS cosine_sim FROM episodic_memory
    # WHERE project_id = %s AND expires_at > now()
    # ORDER BY cosine_sim * recency_weight DESC
    # LIMIT %s
    rows = ...
    _extend_keepalive(row["id"] for row in rows, db, settings)
    return rows

def _recency_weight(created_at: datetime, half_life_days: int) -> float:
    age_days = (datetime.utcnow() - created_at).days
    return 2 ** (-age_days / half_life_days)
```

**Sentence-transformers fallback:** `from sentence_transformers import SentenceTransformer`
loaded lazily on first call. Model: `all-MiniLM-L6-v2` (384-dim; if this is used, the
migration dimension must be 384, not 1536). The ini should document this clearly.

---

### Phase 3 — Write paths

#### 3a — Failure events (synchronous)

`app/database/crud_tasks.py` — in `demote_task()`, after recording the demotion:
```python
insert_episode(
    project_id=task.project_id,
    task_id=task.id,
    episode_type="failure",
    content=f"Task '{task.title}' demoted from {from_stage} to {to_stage}. Reason: {demotion_note}",
    metadata={"stage_key": from_stage, "task_title": task.title, "outcome": "demotion"},
    db=db, settings=settings
)
```

Similarly in `handle_vote_to_revert` (Gap 5) and in `ReflectionAgent` when a blocking
issue is found (Gap 6).

#### 3b — Session-end summaries (async job)

New job type `EpisodicSummaryJob` in `app/database/crud_jobs.py`. Enqueued by
`MaestroLoop` on session teardown (after `submit_work` is processed and the session is
closing normally). The job runner:

1. Reads the task's last N budget entries (cap: 30 turns via `get_task_history_recent`).
2. Calls an LLM (using `maestro_llm_id`) with the prompt:
   ```
   Summarise what this agent session attempted, what worked, and what failed.
   Be specific about approaches and outcomes. Write 2-4 sentences only.
   Focus on information that would help a future agent avoid the same mistakes
   or recognise promising directions.
   ```
3. Calls `insert_episode` with `episode_type="session_summary"`.

Budget for summary generation is charged to the project's autopilot budget (or system
budget if no autopilot budget is configured).

#### 3c — Document store writes (synchronous)

`app/database/crud_documents.py` — in `upsert_document()`:
```python
# Only embed if document is non-trivial (len > 100 chars)
if len(value) > 100:
    insert_episode(
        project_id=project_id,
        task_id=None,
        episode_type="document",
        content=f"[{key}] {value[:2000]}",  # cap at 2000 chars for embedding
        metadata={"source_key": key, "project_id": project_id},
        db=db, settings=settings
    )
```

Documents are overwritten frequently. The prior embedding is not deleted — the new one
is inserted. The older copy decays and eventually expires. This means a document's history
is naturally preserved in episodic memory without explicit versioning.

---

### Phase 4 — Read path

#### Auto-inject at session start

`app/agent/maestro_loop.py` — in the system prompt builder, after arch context injection:

```python
if settings.episodic_memory_auto_inject_k > 0:
    episodes = query_episodes(
        project_id=task.project_id,
        question=task.description,
        k=settings.episodic_memory_auto_inject_k,
        db=db, settings=settings
    )
    if episodes:
        system_prompt += "\n\n## Relevant past experience\n"
        for ep in episodes:
            system_prompt += f"- [{ep['episode_type']} | {ep['created_at']:%Y-%m-%d}] {ep['content']}\n"
```

This section is labelled "Relevant past experience" so agents understand its origin.

#### On-demand tool

**`app/agent/tools.py`** — register `query_episodes`:

```python
"query_episodes": {
    "fn": handle_query_episodes,
    "schema": {
        "name": "query_episodes",
        "description": (
            "Search episodic memory for past attempts, failures, or conclusions "
            "related to a question or approach. Returns up to k semantically similar "
            "past episodes with their outcomes."
        ),
        "parameters": {
            "question":   {"type": "string", "description": "What to search for."},
            "k":          {"type": "integer", "description": "Max results. Default 5, max 20."},
            "episode_type": {"type": "string", "description": "Filter by type: 'failure', 'session_summary', 'document', or omit for all."}
        },
        "required": ["question"]
    }
}
```

`handle_query_episodes` clamps `k` to `[1, 20]`, calls `query_episodes`, returns formatted
list with `content`, `episode_type`, `created_at`, and `metadata` fields.

---

### Phase 5 — Keepalive and nightly cleanup

**Keepalive extension** — applied inside `query_episodes` for every episode returned:
```python
db.execute(
    "UPDATE episodic_memory SET expires_at = GREATEST(expires_at, now() + INTERVAL '%s days'), "
    "last_accessed = now() WHERE id = ANY(%s)",
    (settings.keepalive_extension_days, [ep["id"] for ep in rows])
)
```

**Nightly cleanup job** — added to the existing scheduler's tick loop (runs once per day):
```python
db.execute(
    "DELETE FROM episodic_memory WHERE expires_at < now()"
)
```

Deletion is permanent. There is no soft-delete for episodic memory — a deleted episode
had enough time to be useful and wasn't retrieved.

---

### Phase 6 — Tests

1. **Unit** — `insert_episode`: correct `expires_at` on insert (5 years from now); embedding stored in vector column.
2. **Unit** — `query_episodes`: returns results ordered by relevance score (cosine × recency); keepalive extended for returned episodes.
3. **Unit** — recency decay: episode from 90 days ago has weight 0.5 relative to a today episode for the same cosine distance; from 180 days ago has weight 0.25.
4. **Unit** — keepalive extension: episode at expires_at + 1 day does not appear in queries; episode retrieved 14 days before expiry gets 14 more days.
5. **Unit** — failure event write: demotion triggers `insert_episode` with correct `episode_type="failure"` and metadata.
6. **Unit** — document write: `upsert_document` with `len > 100` triggers embedding; `len <= 100` does not.
7. **Integration** — session-end summary job: `EpisodicSummaryJob` runs, calls LLM, inserts summary episode.
8. **Integration** — auto-inject: system prompt for a new session contains relevant past episodes when they exist.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_episodic_memory.py` | `CREATE EXTENSION vector` + `episodic_memory` table + IVFFlat index |
| `app/agent/episodic_memory.py` | **New file** — `embed_text`, `insert_episode`, `query_episodes`, `_recency_weight`, `_extend_keepalive` |
| `app/agent/tools.py` | Register `query_episodes` |
| `app/agent/maestro_loop.py` | Auto-inject at session start; enqueue `EpisodicSummaryJob` on teardown |
| `app/database/crud_tasks.py` | `demote_task` calls `insert_episode` for failure events |
| `app/database/crud_documents.py` | `upsert_document` calls `insert_episode` for document episodes |
| `app/database/crud_jobs.py` | `EpisodicSummaryJob` type added |
| `app/agent/config.py` | `[episodic_memory]` ini section read into settings |
| `maestro.ini` | `[episodic_memory]` section: `embedding_llm_id`, `decay_half_life_days`, `keepalive_extension_days`, `auto_inject_k` |
| `app/tests/test_episodic_memory.py` | **New file** — all tests for this gap |

---

## Acceptance criteria

- [x] `pgvector` extension enabled; `episodic_memory` table created with HNSW index (migrations 0096, 0097).
- [x] Demotion events, reflection blocking issues, and document writes produce episodic memory entries.
- [x] Session-end summaries generated asynchronously via `EpisodicSummaryJob` (enqueued on teardown, dispatcher in scheduler).
- [x] `query_episodes` returns results ordered by cosine × recency decay; accessed episodes have `expires_at` extended.
- [x] Episodes older than 5 years that have never been retrieved are deleted by the nightly cleanup job.
- [x] Top-3 episodes are auto-injected into every session's system prompt under "Relevant past experience".
- [x] All 941 tests pass with no regressions.
