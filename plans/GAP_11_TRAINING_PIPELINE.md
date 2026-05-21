# Gap 11 — Training data pipeline (closing the weight update loop)

**Status:** Complete  
**Effort:** Medium  
**Priority:** High — this is the mechanism by which the system actually improves the
underlying models, not just the scaffolding around them

---

## Problem

The `budget_entries` table contains 60,000+ full prompt/response/tool-call sequences
from real autonomous work sessions spanning months of home generation. This is some of
the highest-quality training data available anywhere: real multi-turn agentic reasoning,
real tool use under constraint, real failure modes and self-corrections, real formal
proofs, real code that passes tests. It is currently used only for cost tracking and
diagnostic replay.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Extraction signals** | Three quality signals, all required: (1) task eventually reached `completed` stage; (2) at least one session in the task ended with `submit_work(ACCEPTED)` + passing tests; (3) the demotion→retry→success pattern is the highest-value variant — separately tagged and weighted. |
| **Privacy** | Per-project `exclude_from_training` boolean (default `false`). System prompts stripped from all exports — only user/assistant/tool sequences are exported. |
| **Format** | Hugging Face conversational JSONL (`{"messages": [...]}`) for local Qwen LoRA/QLoRA fine-tuning. Tool calls serialized as structured text within assistant turns (see Phase 3). |
| **Triggering** | Threshold-based auto-export: a background job counts qualifying new sessions since the last export. When count ≥ threshold (default 100), the export job runs and produces a new JSONL file. The human manually kicks off the actual training run from that file. |

---

## Implementation plan

### Phase 1 — Per-project opt-out flag

**Migration** (`NNNN_training_flags.py`):

```sql
ALTER TABLE projects ADD COLUMN exclude_from_training BOOLEAN NOT NULL DEFAULT false;
```

**UI** — Project settings panel: "Exclude from training data" toggle. Default off. When
enabled, all sessions from this project are excluded from all future and retroactive
export runs.

**`app/database/crud_projects.py`** — `get_project` serialization includes `exclude_from_training`.

---

### Phase 2 — Session quality scoring

**`app/database/crud_training.py`** — new module:

```python
def score_session(session_id: str, db) -> dict | None:
    """
    Returns a quality score record for a session, or None if it doesn't qualify.
    A session qualifies if all of:
      - Its task eventually reached 'completed'
      - The project is not excluded from training
      - The session has at least one entry with finish_reason = 'stop' (not 'length')
      - The session is not a file-summary or mechanical session (agent_role != 'file_summary')
    Returns: {session_id, task_id, score, tags, qualified}
    Tags: ['accepted', 'failure_recovery', 'proof_verified']
    """
    task = _get_task_for_session(session_id, db)
    if not task or task.type != 'completed':
        return None
    if task.project.exclude_from_training:
        return None

    entries = get_session_entries(session_id, db)
    if any(e.finish_reason == 'length' for e in entries):
        return None  # truncated reasoning — exclude
    if any(e.agent_role == 'file_summary' for e in entries):
        return None  # mechanical, not instructive

    tags = []
    score = 1.0

    if _has_accepted_submit(entries):
        tags.append('accepted')
        score += 0.5

    if _is_failure_recovery(session_id, task.id, db):
        tags.append('failure_recovery')
        score += 1.0  # most valuable — self-correction demonstrated

    if _has_verified_proof(entries):
        tags.append('proof_verified')
        score += 0.5

    return {"session_id": session_id, "task_id": task.id, "score": score, "tags": tags, "qualified": True}

def _is_failure_recovery(session_id, task_id, db) -> bool:
    """True if this session ran after a demotion of the same task and the task ultimately completed."""
    demotions = get_demotion_records(task_id, db)
    if not demotions:
        return False
    # Check if the session started after the most recent demotion
    session_start = get_session_start(session_id, db)
    last_demotion = max(d.created_at for d in demotions)
    return session_start > last_demotion
```

**`training_session_scores` table** (`NNNN_training_session_scores.py`):

```sql
CREATE TABLE training_session_scores (
    session_id   TEXT    PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    score        FLOAT   NOT NULL,
    tags         JSONB   NOT NULL DEFAULT '[]',
    qualified    BOOLEAN NOT NULL,
    scored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    exported_at  TIMESTAMPTZ NULL
);
```

A background job (runs on the scheduler tick, once per hour) scores all sessions not
yet in this table:
```python
def score_new_sessions(db, settings):
    unscored = get_unscored_sessions(db)
    for session_id in unscored:
        result = score_session(session_id, db)
        if result:
            upsert_training_score(result, db)
```

---

### Phase 3 — Hugging Face JSONL export

**`app/agent/training_exporter.py`** — new file:

The HF conversational format per session:
```json
{"messages": [
  {"role": "user",      "content": "Task: Implement auth middleware..."},
  {"role": "assistant", "content": "I'll start by reading the existing middleware setup.\n<tool_call>\n{\"name\": \"read_file\", \"parameters\": {\"path\": \"app/middleware.py\"}}\n</tool_call>"},
  {"role": "tool",      "content": "<tool_response>\n{file content...}\n</tool_response>"},
  {"role": "assistant", "content": "The existing middleware uses session cookies. I'll extend it to support JWT..."}
]}
```

**System prompt handling:** Stripped entirely. The first turn is always the task description
as a `user` message, derived from `task.description + task.planning_result.interface_contracts`.

**Tool call serialization:** Tool calls and tool results are embedded as structured text blocks
within the assistant and tool turns respectively. This maximises compatibility across
different local model chat templates. The XML-like tags (`<tool_call>`, `<tool_response>`)
are a natural format that the model can be trained to reproduce without requiring
native tool-call support in the training framework.

```python
def export_session_to_hf(session_id: str, task, db) -> dict | None:
    """
    Reconstructs the full message history for a session and formats it
    as a Hugging Face conversational record.
    Returns None if reconstruction fails or the session has no valid turns.
    """
    # Reconstruct full message history from delta entries
    entries = get_session_entries_ordered(session_id, db)
    messages = []

    # Inject task description as first user turn (system prompt stripped)
    messages.append({"role": "user", "content": _format_task_preamble(task)})

    for entry in entries:
        full = reconstruct_messages_for_entry(entry.id, db)
        for msg in full:
            if msg["role"] == "system":
                continue  # strip system prompt
            if msg["role"] == "assistant" and msg.get("tool_calls"):
                content = msg.get("content") or ""
                for tc in msg["tool_calls"]:
                    content += f"\n<tool_call>\n{json.dumps(tc['function'], indent=2)}\n</tool_call>"
                messages.append({"role": "assistant", "content": content.strip()})
            elif msg["role"] == "tool":
                messages.append({"role": "tool", "content": f"<tool_response>\n{msg['content']}\n</tool_response>"})
            else:
                messages.append({"role": msg["role"], "content": msg["content"]})

    if len(messages) < 3:  # preamble + at least one assistant + one more turn
        return None

    return {"messages": messages}
```

**Near-duplicate filtering:** Before export, compute a session fingerprint (SHA256 of the
task description). If 10+ sessions share the same fingerprint (same task description, e.g.
from repeated testing), include only the top 3 by score.

---

### Phase 4 — Threshold-based auto-export job

**Background job** (runs in the scheduler tick loop, daily):

```python
def check_training_export_threshold(db, settings):
    new_qualified = count_qualified_unexported(db)
    if new_qualified < settings.training_export_threshold:
        return

    # Produce the export
    sessions = get_qualified_unexported_sessions(db, limit=settings.training_export_max_per_run)
    records = []
    for s in sessions:
        task = get_task(s.task_id, db)
        record = export_session_to_hf(s.session_id, task, db)
        if record:
            records.append(record)

    if not records:
        return

    # Write JSONL file
    export_path = settings.training_export_dir / f"training_{datetime.utcnow():%Y%m%d_%H%M%S}.jsonl"
    export_path.parent.mkdir(parents=True, exist_ok=True)
    with open(export_path, "w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # Mark exported
    mark_sessions_exported([s.session_id for s in sessions], db)

    log(f"Training export: {len(records)} sessions → {export_path}")
```

**`maestro.ini`** — new `[training]` section:
```ini
[training]
export_threshold = 100         ; minimum new qualified sessions before auto-export
export_max_per_run = 1000      ; max sessions per export file
export_dir = data/training_exports
dedup_fingerprint_max = 3      ; max sessions with same task fingerprint per export
```

**API route** — `POST /api/training/export` — manual trigger. Returns path to the generated
JSONL file (or `{"count": 0}` if nothing qualifies).

**API route** — `GET /api/training/status`:
```json
{
  "qualified_unexported": 87,
  "threshold": 100,
  "last_export_at": "2026-05-10T03:00:00Z",
  "last_export_count": 312,
  "exports": [
    {"path": "data/training_exports/training_20260510_030000.jsonl", "count": 312, "size_mb": 4.2}
  ]
}
```

Surfaced in the admin panel (or via MCP tool).

---

### Phase 5 — Feedback loop metrics

While the training run itself is human-triggered externally, the system can measure
whether model performance improves after a training run by tracking key metrics over time.

**`training_checkpoints` table** (`NNNN_training_checkpoints.py`):
```sql
CREATE TABLE training_checkpoints (
    id              SERIAL PRIMARY KEY,
    checkpoint_name TEXT    NOT NULL,   -- e.g. "qwen-35b-lora-2026-05-15"
    model_notes     TEXT    NULL,
    recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

When a human deploys a new model version, they record a checkpoint. The metrics API then
segments performance stats before and after each checkpoint:

**`GET /api/training/metrics?after=checkpoint_id`**:
```json
{
  "demotion_rate": 0.12,          // demotions / total stage transitions
  "completion_rate": 0.81,        // tasks reaching 'completed' / total tasks
  "avg_tokens_to_completion": 45000,
  "length_finish_rate": 0.04,     // sessions truncated by context limit
  "reflection_pass_rate": 0.76    // reflection stages with no blocking issues (Gap 6)
}
```

These metrics are computable from existing tables (tasks, budget_entries, demotion records)
with no additional instrumentation. A regression (demotion rate increases, completion rate
drops) is visible within a day of deploying a new model version.

---

### Phase 6 — Tests

1. **Unit** — `score_session`: task not completed → `None`; excluded project → `None`; truncated session → `None`; file summary session → `None`; clean ACCEPTED session → score ≥ 1.0 with `accepted` tag.
2. **Unit** — `_is_failure_recovery`: session before any demotion → `False`; session after demotion, task completed → `True`.
3. **Unit** — `export_session_to_hf`: system prompt stripped; tool calls appear as `<tool_call>` blocks; sessions with < 3 turns return `None`.
4. **Unit** — near-duplicate filter: 12 sessions with same fingerprint → only top 3 by score included.
5. **Unit** — `check_training_export_threshold`: below threshold → no file written; at or above → JSONL file written and sessions marked exported.
6. **Integration** — full pipeline: task completes → session scored → threshold reached → export job runs → JSONL file contains correct HF format records → status endpoint reflects new export.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_training_flags.py` | `projects.exclude_from_training` |
| `app/migrations/versions/NNNN_training_session_scores.py` | `training_session_scores` table |
| `app/migrations/versions/NNNN_training_checkpoints.py` | `training_checkpoints` table |
| `app/database/crud_training.py` | **New file** — scoring, export tracking, checkpoint CRUD |
| `app/agent/training_exporter.py` | **New file** — HF JSONL formatter, near-dedup filter |
| `app/agent/scheduler.py` | Hourly scoring job + daily threshold check job |
| `app/main.py` | `POST /api/training/export`, `GET /api/training/status`, `GET /api/training/metrics` |
| `maestro.ini` | `[training]` section |
| `app/web/` | Training export status panel in admin view; project settings opt-out toggle |
| `app/tests/test_training_pipeline.py` | **New file** — all tests for this gap |

---

## Acceptance criteria

- [x] Projects marked `exclude_from_training = true` produce no entries in any export file.
- [x] System prompts are absent from all exported JSONL records.
- [x] Sessions with `finish_reason = length` are excluded from scoring.
- [x] Failure-recovery sessions (demotion → retry → task completed) are tagged and receive a higher score than plain ACCEPTED sessions.
- [x] When the unexported qualified session count reaches `export_threshold`, a JSONL file is written to `export_dir` automatically.
- [x] Near-duplicate sessions (same task fingerprint) are capped at `dedup_fingerprint_max` per export.
- [x] `GET /api/training/status` shows count, last export time, and file list.
- [x] All new code passes existing test suite with no regressions.
