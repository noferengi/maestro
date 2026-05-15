# Phase 1 — Data Model & Migration

> **Status:** Ready to implement  
> **Depends on:** Phase 0 ✅ done  
> **Estimated effort:** 3 days  
> **Goal:** Add all new tables to the DB, run backward-compatible migrations, and leave
> the system behaviorally identical to before. No UI changes, no scheduler changes.

---

## Inputs Required

- Zombie fix is merged (✅ done, commit `0126374`)
- Existing test suite is green
- `migrate.bat status` shows all migrations applied

---

## Deliverables

1. All new tables created via a single migration file
2. `tasks.stage_key` populated from `tasks.type` for all existing rows
3. `projects.pipeline_template_id` FK added (nullable, populated for existing projects)
4. "Software Development" pipeline template seeded from `maestro.ini [pipeline] column_order`
5. All existing tests still pass with zero behavior change

---

## New Tables

All DDL is PostgreSQL. New migrations (0068+) are PostgreSQL-only — no SQLite
branch required. The test suite uses SQLite via `MAESTRO_TEST_DB`; new tables
do not need to be created in the test DB because integration tests covering
these features should run against a PostgreSQL test instance.

### Pipeline topology

```sql
CREATE TABLE pipeline_templates (
    id          SERIAL      PRIMARY KEY,
    name        TEXT        NOT NULL UNIQUE,
    description TEXT,
    is_default  BOOLEAN     NOT NULL DEFAULT FALSE,
    is_builtin  BOOLEAN     NOT NULL DEFAULT FALSE,
    version     INTEGER     NOT NULL DEFAULT 1,
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

CREATE TABLE pipeline_stage_groups (
    id          SERIAL  PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
    name        TEXT    NOT NULL,
    color       TEXT,
    position    INTEGER NOT NULL
);

CREATE TABLE pipeline_stages (
    id           SERIAL  PRIMARY KEY,
    template_id  INTEGER NOT NULL REFERENCES pipeline_templates(id),
    stage_key    TEXT    NOT NULL,
    label        TEXT    NOT NULL,
    agent_type   TEXT    NOT NULL,
    position     INTEGER NOT NULL,
    group_id     INTEGER REFERENCES pipeline_stage_groups(id),
    -- JSONB: {gate, retries, llm_override, verifier, required_input_keys,
    --         output_keys, intent, system_prompt, tool_allowlist,
    --         arch_category_keys, upstream_task_gate}
    config       JSONB,
    color        TEXT,
    UNIQUE(template_id, stage_key)
);

CREATE TABLE pipeline_transitions (
    id            SERIAL  PRIMARY KEY,
    template_id   INTEGER NOT NULL REFERENCES pipeline_templates(id),
    from_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
    to_stage_id   INTEGER NOT NULL REFERENCES pipeline_stages(id),
    -- CHECK preferred over a native ENUM: ALTER TYPE cannot run inside a
    -- transaction in PostgreSQL, making ENUM extension migration-unsafe.
    -- Adding a new condition (e.g. 'paused') is a single ALTER TABLE here.
    condition     TEXT    NOT NULL CHECK(condition IN
                      ('pass','fail','reject','always','skip')),
    priority      INTEGER NOT NULL DEFAULT 0
);
```

### Arch categories (per template)

```sql
CREATE TABLE pipeline_arch_categories (
    id          SERIAL  PRIMARY KEY,
    template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
    key         TEXT    NOT NULL,
    label       TEXT    NOT NULL,
    color       TEXT,
    position    INTEGER NOT NULL,
    UNIQUE(template_id, key)
);
```

### Document store

```sql
CREATE TABLE project_documents (
    id                 SERIAL      PRIMARY KEY,
    project_id         INTEGER     NOT NULL REFERENCES projects(id),
    key                TEXT        NOT NULL,
    content            TEXT        NOT NULL,
    content_size_bytes INTEGER     GENERATED ALWAYS AS (octet_length(content)) STORED,
    tags               JSONB,
    written_by_task_id TEXT        REFERENCES tasks(id),
    created_at         TIMESTAMPTZ DEFAULT NOW(),
    updated_at         TIMESTAMPTZ DEFAULT NOW(),
    deleted_at         TIMESTAMPTZ,
    UNIQUE(project_id, key)        -- last write wins per key
);

-- pg_trgm enables fast fuzzy key search (similarity / % operator)
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX project_documents_key_trgm ON project_documents
    USING GIN (key gin_trgm_ops);
```

### Workspace / deletion protection

```sql
CREATE TABLE archived_files (
    id             SERIAL      PRIMARY KEY,
    task_id        TEXT        NOT NULL REFERENCES tasks(id),
    original_path  TEXT        NOT NULL,
    archive_path   TEXT        NOT NULL UNIQUE,
    deleted_at     TIMESTAMPTZ DEFAULT NOW(),
    restored_at    TIMESTAMPTZ
);
```

### Autopilot & settings

```sql
CREATE TABLE system_settings (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

INSERT INTO system_settings VALUES ('maestro_autopilot', 'off')
    ON CONFLICT (key) DO NOTHING;
INSERT INTO system_settings VALUES ('autopilot_start_hour', '23')
    ON CONFLICT (key) DO NOTHING;
INSERT INTO system_settings VALUES ('autopilot_stop_hour', '7')
    ON CONFLICT (key) DO NOTHING;

CREATE TABLE project_settings (
    project_id INTEGER NOT NULL REFERENCES projects(id),
    key        TEXT    NOT NULL,
    value      TEXT    NOT NULL,
    PRIMARY KEY (project_id, key)
);
```

### Custom agent definitions

```sql
CREATE TABLE custom_agent_definitions (
    id            SERIAL      PRIMARY KEY,
    name          TEXT        NOT NULL UNIQUE,
    display_name  TEXT        NOT NULL,
    description   TEXT,
    intent        TEXT,
    system_prompt TEXT        NOT NULL DEFAULT '',
    allowed_tools JSONB       NOT NULL DEFAULT '[]',
    gate_type     TEXT        NOT NULL DEFAULT 'llm_judge',
    verifier      TEXT        NOT NULL DEFAULT 'none',
    verifier_cmd  TEXT,
    created_at    TIMESTAMPTZ DEFAULT NOW()
);
```

---

## Changes to Existing Tables

PostgreSQL supports `ADD COLUMN ... REFERENCES` directly — no workaround needed.

```sql
-- Tasks get a stage_key column (additive, nullable during transition)
ALTER TABLE tasks ADD COLUMN stage_key TEXT;

-- Projects reference a pipeline template
ALTER TABLE projects ADD COLUMN pipeline_template_id INTEGER
    REFERENCES pipeline_templates(id);
```

---

## Migration Seed Logic

The migration script (`app/migrations/versions/NNNN_malleable_pipelines_baseline.py`)
must run this sequence after DDL:

1. Insert "Software Development" template (`is_default=1`).
2. Read `column_order` from `maestro.ini [pipeline]` to get the ordered stage list.
3. Insert one `pipeline_stages` row per stage, mapping the existing `type` values
   to `stage_key` and `agent_type` using a hardcoded mapping table (see below).
4. Insert the "Optimization + Security" group covering `optimization` and `security`.
5. Insert transition edges for the default pipeline (pass edges forward, fail/reject
   edges backward per current behavior).
6. Insert default arch categories for the Software Development template.
7. `UPDATE tasks SET stage_key = type WHERE stage_key IS NULL`.
8. `UPDATE projects SET pipeline_template_id = <software_dev_id>
   WHERE pipeline_template_id IS NULL`.

**Stage → agent_type mapping for seed:**

| stage_key         | agent_type            |
|-------------------|-----------------------|
| idea              | intake_agent          |
| planning          | planning_agent        |
| indev             | implementation_agent  |
| conceptual_review | review_agent          |
| optimization      | optimization_agent    |
| security          | security_agent        |
| final_review      | final_review_agent    |
| human_review      | human_gate            |
| completed         | terminal              |
| architecture      | arch_agent            |

**Default arch categories for Software Development:**
Platform, Design, Testing, Performance, API, Data, Tooling, Security, DevOps,
Documentation, Quality, Cost, Scalability, General

---

## Test Criteria

- `migrate.bat status` shows migration applied
- All existing task rows have `stage_key` populated
- All existing project rows have `pipeline_template_id` set
- "Software Development" template has the correct number of stages and transitions
- Full test suite (`venv/Scripts/python.exe -m pytest app/tests/ -v`) passes green
- `GET /api/projects` still returns all projects; `GET /api/projects/{name}/tasks`
  still returns all tasks with unchanged shape

---

## Risk Factors

**Migration seed idempotency** — use `INSERT ... ON CONFLICT DO NOTHING` for all
seed rows (templates, stages, transitions, settings) so re-running
`migrate.bat migrate` on a partially-applied DB does not create duplicates.

**`pg_trgm` availability** — the `CREATE EXTENSION IF NOT EXISTS pg_trgm` call
requires the extension to be available on the PostgreSQL server. It ships with
standard PostgreSQL distributions (including RDS, Cloud SQL, Supabase). If the
deployment user lacks `CREATE EXTENSION` privilege, have a superuser run it once
before applying the migration.

**`maestro.ini` read during migration** — the migration runner has no config
dependency today. Add a one-time read of `maestro.ini` within the migration function
rather than importing the full app config module, to keep the migration runner
dependency-free.
