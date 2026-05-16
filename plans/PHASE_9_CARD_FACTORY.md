# Phase 9 — Card Factory System

> **Status:** COMPLETE — 2026-05-15  
> **Depends on:** Phase 5; Phase 3 (API for factory node config); Phase 4 (factory node in editor)  
> **Estimated effort:** 4 days  
> **Goal:** A factory node type that ingests external data sources (file lists, folders,
> database queries, manual prompts) and produces batches of cards. Two segmentation
> modes: mechanical (one card per item) and LLM-segmented (agent decides how to split).
> Multiple trigger mechanisms.

---

## Factory Node in the Pipeline

A factory node is a special node type in the Litegraph editor. In the DB it is stored
as a `pipeline_stages` row with `agent_type = "factory_node"`. Its `config` JSON
carries factory-specific fields:

```json
{
  "factory_source_type": "folder",
  "factory_source_config": {
    "path": "/data/research_papers/",
    "file_glob": "*.pdf",
    "recursive": true
  },
  "factory_segmentation_mode": "mechanical",
  "factory_entry_stage": "ingest",
  "factory_trigger": ["manual", "predecessor_complete"],
  "factory_predecessor_condition": "pass",
  "factory_card_template": {
    "title_template": "Process: {filename}",
    "description_template": "Ingest and summarize {filepath}"
  }
}
```

---

## Data Source Types

| `factory_source_type` | What it produces |
|---|---|
| `file_list` | One item per file path listed in a text file (one path per line) |
| `folder` | One item per file matching a glob in a folder (recursive optional) |
| `csv` | One item per row in a CSV file; column values available as template vars |
| `json_array` | One item per element in a JSON array file |
| `sqlite_query` | One item per row returned by a SQL query on an **external** SQLite file (a data source, not the app DB) |
| `manual_prompt` | No external data; LLM segmentation only — agent receives the trigger card's description and decides how to split |
| `maestro_cards` | One item per card in a specified stage of the current project (cross-card batch operations) |

---

## Segmentation Modes

### Mechanical (1:1 mapping)

One card per data item. The card's title and description are interpolated from
`factory_card_template` using the item's fields as template variables.

For a `folder` source:
- `{filename}` — base name of the file
- `{filepath}` — full path
- `{extension}` — file extension
- `{size_bytes}` — file size

For a `csv` source:
- `{column_name}` — any column in the row

Card creation uses `batch_create_cards` internally (Phase 5 tool), so all cards
enter at `factory_entry_stage` and the origin card can be archived if configured.

### LLM-segmented

The factory dispatches `CardFactoryAgent`, which is a `CustomLLMAgent` variant
with access to `batch_create_cards` and a source-reading tool. The agent:
1. Reads the data source (via `read_file`, `list_dir`, or `query_db` depending on
   source type)
2. Decides how to segment the work into cards (the LLM makes this decision based
   on its system prompt + intent)
3. Calls `batch_create_cards` with the card list it produced

The system prompt for `CardFactoryAgent` is the factory node's `intent` + `system_prompt`
fields (same authoring model as any other agent, with ⚡ generation in the editor).

---

## Trigger Mechanisms

### Manual

A "Run Factory" button appears on the factory node in the Litegraph canvas (drawn
as a small play button ▶ in the node's canvas area). Clicking it calls
`POST /api/pipelines/stages/{stage_id}/trigger-factory` with the target project.

Also available as a button in the project's task list view (for factories in the
project's pipeline).

### Predecessor card reaches COMPLETED

When the factory's `factory_trigger` includes `"predecessor_complete"`:
- After any card in the stage immediately preceding the factory reaches `completed`
  (with condition matching `factory_predecessor_condition`)
- The scheduler checks: has this card already triggered a factory run? (checked via
  a `factory_runs` audit table keyed on `(factory_stage_id, trigger_card_id)`)
- If not, dispatches `CardFactoryAgent` for the factory stage, passing the completed
  card's ID and content blob as the data source context

This enables chained pipelines: "Stage 1 produces a list of topics → Factory fires →
Stage 2 processes each topic as its own card."

### Cron / scheduled

Factory nodes with `"cron"` in `factory_trigger` have a cron expression stored in
`factory_source_config.cron_schedule` (e.g. `"0 23 * * *"` = 11pm daily).

The scheduler checks scheduled factories on each tick:
```python
for factory_stage in get_cron_factory_stages():
    if cron_is_due(factory_stage.config['cron_schedule'], last_run_at):
        dispatch_factory(factory_stage, project)
```

`last_run_at` is stored in a `factory_runs` table. The cron expression is evaluated
with a lightweight pure-Python parser (no external cron library required for simple
expressions; add `croniter` as a dependency if complex expressions are needed).

---

## `factory_runs` Audit Table

```sql
CREATE TABLE factory_runs (
    id               SERIAL      PRIMARY KEY,
    factory_stage_id INTEGER     NOT NULL REFERENCES pipeline_stages(id),
    project_id       INTEGER     NOT NULL REFERENCES projects(id),
    trigger_type     TEXT        NOT NULL,  -- manual | predecessor_complete | cron
    trigger_card_id  TEXT        REFERENCES tasks(id),
    started_at       TIMESTAMPTZ DEFAULT NOW(),
    completed_at     TIMESTAMPTZ,
    cards_created    INTEGER     DEFAULT 0,
    status           TEXT        NOT NULL DEFAULT 'running'  -- running | completed | failed
);
```

---

## Data Source Adapters

```python
# app/agent/factory_sources.py

class DataSourceAdapter(ABC):
    @abstractmethod
    def items(self) -> Iterator[dict]:
        """Yield one dict per item. Keys depend on source type."""

class FolderAdapter(DataSourceAdapter):
    def __init__(self, path: str, glob: str = "*", recursive: bool = False): ...
    def items(self):
        for filepath in glob_files(self.path, self.glob, self.recursive):
            yield {
                "filepath": str(filepath),
                "filename": filepath.name,
                "extension": filepath.suffix,
                "size_bytes": filepath.stat().st_size,
            }

class CSVAdapter(DataSourceAdapter):
    def __init__(self, filepath: str): ...
    def items(self):
        with open(self.filepath) as f:
            for row in csv.DictReader(f):
                yield dict(row)

class SQLiteQueryAdapter(DataSourceAdapter):
    """Reads from an *external* SQLite file as a data source — not the app DB."""
    def __init__(self, db_path: str, query: str): ...
    def items(self):
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        for row in conn.execute(self.query):
            yield dict(row)

class ManualPromptAdapter(DataSourceAdapter):
    def __init__(self, trigger_card_content: dict): ...
    def items(self):
        yield {"content": self.trigger_card_content}  # single item; LLM does segmentation

ADAPTERS = {
    "folder":        FolderAdapter,
    "file_list":     FileListAdapter,
    "csv":           CSVAdapter,
    "json_array":    JSONArrayAdapter,
    "sqlite_query":  SQLiteQueryAdapter,
    "manual_prompt": ManualPromptAdapter,
    "maestro_cards": MaestroCardsAdapter,
}
```

---

## Mechanical Card Creation Flow

```python
def run_mechanical_factory(factory_stage: PipelineStage, project: Project,
                            trigger_card_id: str | None = None) -> int:
    cfg = factory_stage.config
    adapter_cls = ADAPTERS[cfg['factory_source_type']]
    adapter = adapter_cls(**cfg['factory_source_config'])
    template = cfg['factory_card_template']
    cards = []
    for item in adapter.items():
        title = template['title_template'].format(**item)
        description = template.get('description_template', '').format(**item)
        cards.append(CardSpec(
            title=title,
            description=description,
            entry_stage=cfg['factory_entry_stage'],
        ))
    batch_create_cards(
        task_id=trigger_card_id or f"factory_{factory_stage.id}",
        cards=cards,
        new_parent=None,
        archive_origin=False,
    )
    return len(cards)
```

---

## Factory Node in the Litegraph Editor

Factory nodes render with a distinct icon (drawn in the node's canvas area with a
user-assignable color background). The property panel shows a factory-specific
template with source configuration fields:

```
┌──────────────────────────────────────────────────────────┐
│ Factory Node Properties                          [Close] │
├──────────────────────────────────────────────────────────┤
│ Label            [Research Loader          ]  ⚡        │
│ Color            [████ #7c3aed              ]            │
│                                                          │
│ Source type      [folder                   ▼]            │
│ Folder path      [/data/papers/             ]            │
│ File glob        [*.pdf                     ]            │
│ ☐ Recursive                                             │
│                                                          │
│ Segmentation     [Mechanical (1 per file)  ▼]            │
│ Entry stage      [ingest                   ▼]            │
│                                                          │
│ Card title       [Process: {filename}       ]  ⚡       │
│ Card description [Summarize {filepath}      ]  ⚡       │
│                                                          │
│ Triggers                                                 │
│   ☑ Manual button    ☑ Predecessor complete             │
│   ☐ Cron schedule    [                      ]            │
│                                                          │
│                              [Run Now] [Save] [Revert]   │
└──────────────────────────────────────────────────────────┘
```

"Run Now" is the manual trigger button. It POSTs to
`/api/pipelines/stages/{stage_id}/trigger-factory?project={name}` immediately,
without saving first (useful for testing the factory config).

---

## Test Criteria

- Create a folder factory node pointing at a test directory with 5 files →
  "Run Now" → 5 cards created in the DB at the specified entry stage
- CSV factory with a 10-row file → 10 cards created, each with columns
  interpolated into title/description
- SQLite query factory with a 3-row result → 3 cards created
- Predecessor-complete trigger: mark a card COMPLETED in the preceding stage →
  scheduler detects it next tick → factory fires → cards created →
  `factory_runs` row shows `status=completed, cards_created=N`
- Cron factory with `"cron_schedule": "* * * * *"` (every minute) → fires on
  next scheduler tick after the minute boundary

---

## Risk Factors

**Path security** — `FolderAdapter` and `SQLiteQueryAdapter` (which reads external SQLite data files, not the app DB) accept arbitrary paths.
For the current single-user local deployment this is acceptable. For a multi-user
deployment, paths must be validated against the project root or an allowlist. Add a
`FACTORY_ALLOWED_ROOTS` config in `maestro.ini` and validate in the adapter
constructor; leave it unconfigured (no restriction) by default.

**Large datasets** — a CSV with 100k rows will create 100k cards. The batch_create_cards
tool currently creates cards synchronously. For large factories, stream the creation
in batches of 100 rows per DB transaction to avoid a single giant write lock.

**Cron precision** — the scheduler tick interval (default ~10 seconds) limits cron
precision to ~10 second granularity. This is fine for minute-level or hourly cron jobs.
Document the limitation in `maestro.ini`.

---

## Implementation Audit (2026-05-15)

### What was delivered

`app/agent/card_factory.py`, `app/agent/factory_sources.py`, and
`app/database/crud_factory.py` are fully implemented. All seven adapter types exist
(`FolderAdapter`, `FileListAdapter`, `CSVAdapter`, `JSONArrayAdapter`,
`SQLiteQueryAdapter`, `ManualPromptAdapter`, `MaestroCardsAdapter`) with a
`build_adapter()` dispatch function. Both mechanical (`_run_mechanical`) and
LLM-segmented (`_run_llm_segmented`) modes work. All three trigger mechanisms —
manual button, predecessor-complete, and cron — are implemented. The `factory_runs`
audit table (migration 0072) and CRUD helpers are correct. `test_card_factory.py`
(487 lines) covers adapters, interpolation, CRUD, and cron timing.

Template interpolation uses `_DefaultDict` so missing keys degrade gracefully
(left as `{key}` literal) rather than raising `KeyError`.

Cron evaluation includes a fallback minimal parser when `croniter` is not installed.

### Gaps

**No test for `_run_llm_segmented`** — The LLM-segmented factory path dispatches
`CardFactoryAgent` with mocked LLM interactions. No unit test covers this path.

**No integration tests for trigger mechanisms** — `test_card_factory.py` tests the
cron-timing helper in isolation, but there is no test verifying that the scheduler
tick calls `check_predecessor_triggers()` or `check_cron_triggers()` and that those
functions create factory runs and dispatch correctly.

**Path security validation not implemented** — The spec warned about `FolderAdapter`
and `SQLiteQueryAdapter` accepting arbitrary paths. `FACTORY_ALLOWED_ROOTS` config and
validation in the adapter constructor were not added. For the current single-user
deployment this is acceptable; document the limitation in `maestro.ini`.

**Large dataset batching** — The spec suggested streaming creation in batches of 100
rows for CSVs with 100k+ rows. `_run_mechanical` creates all cards in a single
synchronous write. Fine for current usage; add batching when needed.
