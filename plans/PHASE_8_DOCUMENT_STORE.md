# Phase 8 — Project Document Store

> **Status:** SUBSTANTIALLY COMPLETE — 2026-05-15 ⚠️ (no UI document viewer, no test_document_store.py; see audit)  
> **Depends on:** Phase 1 (project_documents table); Phase 2 (tool allowlist system)  
> **Estimated effort:** 2 days  
> **Goal:** A per-project shared document store that agents can write named artifacts
> to and retrieve by exact key or fuzzy key match. This is the shared memory layer
> for cross-card knowledge — lemmas proved by one math card, character profiles
> written by a planning card, research summaries surfaced to a drafting card.

---

## Design Decisions (from session Q&A)

- **Project-scoped, fully shared** — one store per project; all agents in the project
  can read and write any document; writes are tagged with the writing card's ID for
  provenance
- **Last-write-wins per key** — `UNIQUE(project_id, key)` in the DB; inserting a
  document with an existing key updates it in place (timestamp and written_by updated)
- **Retrieval: exact key + fuzzy key (low edit distance)** — no embeddings, no RAG,
  no vector DB; fuzzy matching is pure string distance (Levenshtein / trigram)
- **Not a RAG system** — documents are named artifacts, not chunks of a larger corpus;
  agents write specific things under specific names and retrieve them by name

---

## Deliverables

1. `project_documents` table (Phase 1 handles DDL)
2. `app/agent/doc_store.py` — Python API for agents
3. Agent tool wrappers: `store_document`, `get_document`, `list_documents`,
   `search_documents`
4. REST API for human inspection and manual edits
5. Document viewer in the UI (simple modal, not a full page)

---

## `doc_store.py` — Python API

```python
# app/agent/doc_store.py

def store_document(project_id: int, key: str, content: str,
                   tags: list[str] | None, written_by_task_id: str) -> None:
    """
    Write content under key. Creates a new row or updates existing.
    Key is normalized to lowercase on write so that retrieval is always
    case-insensitive without needing ILIKE or function-based indexes.
    (e.g. "Characters/Elara" is stored and retrieved as "characters/elara")
    """
    key = key.lower().strip()

def get_document(project_id: int, key: str) -> str | None:
    """
    Exact key lookup. Key is normalized to lowercase before querying,
    matching the normalization applied at write time.
    """
    key = key.lower().strip()

def fuzzy_get_document(project_id: int, key: str, max_distance: int = 3) -> list[FuzzyResult]:
    """
    Returns documents whose key is within `max_distance` edit distance of `key`.
    Results sorted by distance ascending.
    FuzzyResult: {key, content, distance, written_by_task_id, updated_at}
    Used when agents misspell a key or use a close variant.
    """

def list_documents(project_id: int, tag: str | None = None) -> list[DocumentMeta]:
    """
    List all documents in the project (key, tags, written_by_task_id, updated_at).
    Optional tag filter. Returns metadata only, not content.
    """

def delete_document(project_id: int, key: str, deleted_by_task_id: str) -> bool:
    """
    Soft-delete: sets a deleted_at timestamp. Document no longer appears in
    list_documents or fuzzy search. Exact get still returns None.
    Returns True if the document existed and was deleted.
    """
```

### Fuzzy matching implementation

PostgreSQL with `pg_trgm` (enabled in the Phase 1 migration) provides native
trigram similarity search. No Python-side distance computation, no external
dependency, and the GIN index on `project_documents.key` makes it fast even with
many documents.

```python
def fuzzy_get_document(project_id: int, key: str,
                       threshold: float = 0.3) -> list[FuzzyResult]:
    """
    Uses pg_trgm similarity() to find keys close to `key`.
    similarity() returns 0.0–1.0; higher = more similar.
    threshold=0.3 catches typos and close variants without too many false hits.
    """
    with SessionLocal() as db:
        rows = db.execute(
            text("""
                SELECT key, content, similarity(key, :q) AS sim,
                       written_by_task_id, updated_at
                FROM project_documents
                WHERE project_id = :pid
                  AND deleted_at IS NULL
                  AND similarity(key, :q) >= :thresh
                ORDER BY sim DESC
                LIMIT 10
            """),
            {"pid": project_id, "q": key, "thresh": threshold}
        ).fetchall()
    return [FuzzyResult(**row._mapping) for row in rows]
```

The `%` operator (`key % :q`) also works and uses the GIN index automatically.
The `similarity()` function is used here instead so we can filter by threshold
and sort by score in one query. The GIN index created in Phase 1 ensures this
is an index scan, not a sequential scan.

---

## Agent Tool Wrappers

These are the tool schema definitions that agents see. Added to `config.py` tool registry
and available in a stage's `tool_allowlist` under the key `"document_store"` (grants
all four operations) or individually as `"store_document"`, `"get_document"`,
`"search_documents"`.

```json
{
  "name": "store_document",
  "description": "Write a named document to the project's shared document store. Other agents in this project can read it by key.",
  "parameters": {
    "key":     { "type": "string", "description": "Unique name for this document, e.g. 'proofs/lemma_3' or 'characters/elara'" },
    "content": { "type": "string" },
    "tags":    { "type": "array", "items": { "type": "string" }, "description": "Optional tags for categorization" }
  }
}

{
  "name": "get_document",
  "description": "Retrieve a document from the project's shared document store by exact key. Returns null if not found.",
  "parameters": {
    "key": { "type": "string" }
  }
}

{
  "name": "search_documents",
  "description": "Find documents whose key is close to the query (fuzzy match on key name). Returns up to 5 results sorted by similarity. Use when you are not sure of the exact key.",
  "parameters": {
    "query":        { "type": "string" },
    "max_distance": { "type": "integer", "default": 3 }
  }
}

{
  "name": "list_documents",
  "description": "List all document keys in the project store, optionally filtered by tag.",
  "parameters": {
    "tag": { "type": "string", "description": "Optional tag filter" }
  }
}
```

---

## REST API

```
GET    /api/projects/{name}/documents             — list all (metadata only)
GET    /api/projects/{name}/documents/{key}       — get one document (full content)
PUT    /api/projects/{name}/documents/{key}       — create or update (human edit)
DELETE /api/projects/{name}/documents/{key}       — soft-delete
GET    /api/tasks/{task_id}/documents             — list documents written by this task
```

---

## UI: Document Viewer

A "Store" tab added to the project's diagnostics panel (or a new button in the
project header). Shows a searchable table of all project documents:

```
┌─────────────────────────────────────────────────────────────┐
│ Project Documents — Twin Prime Conjecture      [Search key] │
├──────────────────┬──────────┬──────────────┬────────────────┤
│ Key              │ Tags     │ Written by   │ Updated        │
├──────────────────┼──────────┼──────────────┼────────────────┤
│ proofs/lemma_1   │ math     │ card #42     │ 2 hours ago    │
│ proofs/lemma_2   │ math     │ card #43     │ 1 hour ago     │
│ approach/angle_3 │ strategy │ card #38     │ 3 hours ago    │
└──────────────────┴──────────┴──────────────┴────────────────┘
```

Clicking a row opens the full document content in a side panel. Human editors can
edit the content directly via the REST API.

---

## Math Pipeline Use Case

For the twin prime conjecture pipeline, the expected document store usage:

- **Planning card** writes `approach/overall_strategy` — the high-level decomposition
- **Each subdivision card** writes `approaches/{angle_name}` — its specific attack angle
- **Each proof attempt card** writes `proofs/{lemma_name}` — proved lemmas
- **A synthesis card** reads all `proofs/*` documents, checks for consistency and
  coverage, writes `proofs/final_synthesis`
- **A verification card** reads `proofs/final_synthesis`, runs the pluggable verifier
  (Phase 5), writes `verification/result`

All of this is coordinated by the pipeline topology (edges) and the document store
(shared memory). No hardcoded cross-card communication needed.

---

## Test Criteria

- `store_document(project_id, "test/doc", "content", ["math"])` → row in DB;
  `get_document(project_id, "test/doc")` → returns "content"
- Second `store_document` with same key → row updated (updated_at changes), not duplicated
- `fuzzy_get_document(project_id, "test/dox", max_distance=2)` → returns the
  `"test/doc"` document with distance=1
- `list_documents(project_id, tag="math")` → returns only tagged docs
- Agent with `store_document` in its tool allowlist calls the tool → document
  appears in DB with correct `written_by_task_id`
- `DELETE /api/projects/{name}/documents/{key}` → `get_document` returns None;
  document still in DB with `deleted_at` set

---

## Risk Factors

**Key namespace collisions** — multiple agents writing to the same key means the
last writer wins with no merge or conflict detection. For structured data (like proof
attempts), agents should use unique keys (`proofs/attempt_1`, `proofs/attempt_2`)
rather than a shared key. Document this in the tool description so LLMs understand
the convention. PostgreSQL MVCC means concurrent writes to *different* keys never
block each other; only a simultaneous write to the exact same `(project_id, key)`
pair could conflict, resolved by the `UNIQUE` constraint raising a conflict that the
application handles with an `ON CONFLICT DO UPDATE`.

**Case sensitivity** — keys are normalized to lowercase in `store_document()` and
`get_document()` before any DB interaction. `pg_trgm similarity()` also operates on
lowercase trigrams. No agent can create a key that differs only by case from an
existing one.

**Content size** — the `content_size_bytes` column is a PostgreSQL generated column
(`GENERATED ALWAYS AS (octet_length(content)) STORED`) defined in Phase 1, so size
is always accurate without application-layer bookkeeping. `list_documents` returns
the size column but not content, keeping the response payload small even for
projects with many large documents.

---

## Implementation Audit (2026-05-15)

### What was delivered

`app/agent/doc_store.py` (106 lines) and `app/database/crud_documents.py` (316 lines)
are fully implemented. Key normalization to lowercase at the agent API boundary, upsert
semantics, pg_trgm fuzzy matching via native PostgreSQL `similarity()`, soft-delete,
and tag filtering are all correct. Both project-name and project-ID wrappers exist.

Agent tool wrappers (`store_document`, `get_document`, `search_documents`,
`list_documents`) exist in `tools.py` and can be added to a stage's `tool_allowlist`.

REST API endpoints for human inspection and editing are implemented in `main.py`.

### Gaps

**No UI document viewer** — The spec called for a "Store" tab or modal in the project
header with a searchable table of documents. Not found in `index.html`. Human
inspection requires direct API calls.

**No test file** — There is no `test_document_store.py`. None of the plan's test
criteria (upsert, fuzzy match, tag filter, soft-delete, tool call via agent) are
covered by automated tests. This is the largest testing gap across the project.

**`content_size_bytes` generated column** — Omitted from Phase 1 migration (see
Phase 1 audit). `list_documents` still works but cannot return the size field.
Can be added as a non-generated computed column or omitted until needed.
