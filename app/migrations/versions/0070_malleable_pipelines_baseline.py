import configparser
import json
from pathlib import Path

description = "malleable pipelines baseline - phase 1 (PostgreSQL)"


def up(conn):
    print("[0070] Starting malleable pipelines migration (Phase 1)...")

    # 1. Create New Tables
    print("[0070] Creating new tables...")
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS pipeline_templates (
            id          SERIAL      PRIMARY KEY,
            name        TEXT        NOT NULL UNIQUE,
            description TEXT,
            is_default  BOOLEAN     NOT NULL DEFAULT FALSE,
            is_builtin  BOOLEAN     NOT NULL DEFAULT FALSE,
            version     INTEGER     NOT NULL DEFAULT 1,
            created_at  TIMESTAMPTZ DEFAULT NOW(),
            updated_at  TIMESTAMPTZ DEFAULT NOW()
        );

        CREATE TABLE IF NOT EXISTS pipeline_stage_groups (
            id          SERIAL  PRIMARY KEY,
            template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
            name        TEXT    NOT NULL,
            color       TEXT,
            position    INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_stages (
            id           SERIAL  PRIMARY KEY,
            template_id  INTEGER NOT NULL REFERENCES pipeline_templates(id),
            stage_key    TEXT    NOT NULL,
            label        TEXT    NOT NULL,
            agent_type   TEXT    NOT NULL,
            position     INTEGER NOT NULL,
            group_id     INTEGER REFERENCES pipeline_stage_groups(id),
            config       JSONB,
            color        TEXT,
            UNIQUE(template_id, stage_key)
        );

        CREATE TABLE IF NOT EXISTS pipeline_transitions (
            id            SERIAL  PRIMARY KEY,
            template_id   INTEGER NOT NULL REFERENCES pipeline_templates(id),
            from_stage_id INTEGER NOT NULL REFERENCES pipeline_stages(id),
            to_stage_id   INTEGER NOT NULL REFERENCES pipeline_stages(id),
            condition     TEXT    NOT NULL CHECK(condition IN ('pass','fail','reject','always','skip')),
            priority      INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS pipeline_arch_categories (
            id          SERIAL  PRIMARY KEY,
            template_id INTEGER NOT NULL REFERENCES pipeline_templates(id),
            key         TEXT    NOT NULL,
            label       TEXT    NOT NULL,
            color       TEXT,
            position    INTEGER NOT NULL,
            UNIQUE(template_id, key)
        );

        CREATE TABLE IF NOT EXISTS project_documents (
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
            UNIQUE(project_id, key)
        );

        CREATE EXTENSION IF NOT EXISTS pg_trgm;

        CREATE INDEX IF NOT EXISTS project_documents_key_trgm ON project_documents USING GIN (key gin_trgm_ops);

        CREATE TABLE IF NOT EXISTS archived_files (
            id             SERIAL      PRIMARY KEY,
            task_id        TEXT        NOT NULL REFERENCES tasks(id),
            original_path  TEXT        NOT NULL,
            archive_path   TEXT        NOT NULL UNIQUE,
            deleted_at     TIMESTAMPTZ DEFAULT NOW(),
            restored_at    TIMESTAMPTZ
        );

        CREATE TABLE IF NOT EXISTS project_settings (
            project_id INTEGER NOT NULL REFERENCES projects(id),
            key        TEXT    NOT NULL,
            value      TEXT    NOT NULL,
            PRIMARY KEY (project_id, key)
        );

        CREATE TABLE IF NOT EXISTS custom_agent_definitions (
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
    """)

    # 2. Add columns to existing tables (Additive, nullable)
    print("[0070] Updating existing tables...")
    try:
        conn.execute("ALTER TABLE tasks ADD COLUMN stage_key TEXT")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("  - tasks.stage_key already exists")
        else:
            raise

    try:
        conn.execute("ALTER TABLE projects ADD COLUMN pipeline_template_id INTEGER REFERENCES pipeline_templates(id)")
    except Exception as e:
        if "already exists" in str(e).lower():
            print("  - projects.pipeline_template_id already exists")
        else:
            raise

    # 3. Seed "Software Development" template
    print("[0070] Seeding Software Development template...")
    conn.execute("""
        INSERT INTO pipeline_templates (name, description, is_default, is_builtin)
        VALUES ('Software Development', 'Standard design-implement-verify pipeline', TRUE, TRUE)
        ON CONFLICT (name) DO NOTHING
    """)

    res = conn.execute("SELECT id FROM pipeline_templates WHERE name = 'Software Development'")
    template_id = res.fetchone()['id']

    # 4. Read column_order from maestro.ini
    column_order = ["architecture", "idea", "planning", "indev", "conceptual_review", "optimization", "security", "final_review", "human_review", "completed"]
    ini_path = Path("maestro.ini")
    if ini_path.exists():
        try:
            config = configparser.ConfigParser()
            config.read(ini_path)
            if config.has_section("pipeline") and config.has_option("pipeline", "column_order"):
                column_order = [s.strip() for s in config.get("pipeline", "column_order").split(",")]
        except Exception as e:
            print(f"  WARNING: Could not read maestro.ini ({e}). Using default column order.")

    # Mapping table for agent_type
    agent_mapping = {
        'idea': 'intake_agent',
        'planning': 'planning_agent',
        'indev': 'implementation_agent',
        'conceptual_review': 'review_agent',
        'optimization': 'optimization_agent',
        'security': 'security_agent',
        'final_review': 'final_review_agent',
        'human_review': 'human_gate',
        'completed': 'terminal',
        'architecture': 'arch_agent',
    }

    # 5. Insert pipeline_stages
    print("[0070] Seeding pipeline stages...")
    for pos, stage_key in enumerate(column_order):
        label = stage_key.replace('_', ' ').title()
        agent_type = agent_mapping.get(stage_key, 'generic_agent')
        conn.execute("""
            INSERT INTO pipeline_stages (template_id, stage_key, label, agent_type, position)
            VALUES (:tid, :key, :label, :agent, :pos)
            ON CONFLICT (template_id, stage_key) DO NOTHING
        """, {"tid": template_id, "key": stage_key, "label": label, "agent": agent_type, "pos": pos})

    # 6. Insert "Optimization + Security" group
    print("[0070] Seeding stage groups...")
    conn.execute("""
        INSERT INTO pipeline_stage_groups (template_id, name, color, position)
        VALUES (:tid, 'Optimization + Security', '#ff9800', 4)
        ON CONFLICT DO NOTHING
    """, {"tid": template_id})

    res = conn.execute("SELECT id FROM pipeline_stage_groups WHERE template_id = :tid AND name = 'Optimization + Security'", {"tid": template_id})
    group_id = res.fetchone()['id']

    # Assign stages to group
    for stage_key in ['optimization', 'security']:
        conn.execute("""
            UPDATE pipeline_stages SET group_id = :gid
            WHERE template_id = :tid AND stage_key = :key
        """, {"gid": group_id, "tid": template_id, "key": stage_key})

    # 7. Insert transitions
    print("[0070] Seeding transitions...")
    # Get all stage IDs for this template
    res = conn.execute("SELECT id, stage_key FROM pipeline_stages WHERE template_id = :tid", {"tid": template_id})
    stage_ids = {row['stage_key']: row['id'] for row in res.fetchall()}

    # Standard forward pass transitions
    for i in range(len(column_order) - 1):
        from_key = column_order[i]
        to_key = column_order[i+1]
        if from_key in stage_ids and to_key in stage_ids:
            conn.execute("""
                INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :from_id, :to_id, 'pass')
                ON CONFLICT DO NOTHING
            """, {"tid": template_id, "from_id": stage_ids[from_key], "to_id": stage_ids[to_key]})

    # Rejection/Fail transitions (simplified backward per current hardcoded logic)
    fail_transitions = {
        'planning': 'idea',
        'indev': 'planning',
        'conceptual_review': 'indev',
        'optimization': 'indev',
        'security': 'indev',
        'final_review': 'indev',
    }
    for from_key, to_key in fail_transitions.items():
        if from_key in stage_ids and to_key in stage_ids:
            for condition in ['fail', 'reject']:
                conn.execute("""
                    INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition)
                    VALUES (:tid, :from_id, :to_id, :cond)
                    ON CONFLICT DO NOTHING
                """, {"tid": template_id, "from_id": stage_ids[from_key], "to_id": stage_ids[to_key], "cond": condition})

    # 8. Insert default arch categories
    print("[0070] Seeding arch categories...")
    arch_categories = [
        "Platform", "Design", "Testing", "Performance", "API", "Data", "Tooling",
        "Security", "DevOps", "Documentation", "Quality", "Cost", "Scalability", "General"
    ]
    for pos, label in enumerate(arch_categories):
        key = label.lower()
        conn.execute("""
            INSERT INTO pipeline_arch_categories (template_id, key, label, position)
            VALUES (:tid, :key, :label, :pos)
            ON CONFLICT (template_id, key) DO NOTHING
        """, {"tid": template_id, "key": key, "label": label, "pos": pos})

    # 9. System settings
    print("[0070] Seeding system settings...")
    for key, val in [('maestro_autopilot', 'off'), ('autopilot_start_hour', '23'), ('autopilot_stop_hour', '7')]:
        json_val = json.dumps(val)
        conn.execute("""
            INSERT INTO system_settings (key, value)
            VALUES (:key, :val)
            ON CONFLICT (key) DO NOTHING
        """, {"key": key, "val": json_val})

    # 10. Backfill existing data
    print("[0070] Backfilling existing data...")
    conn.execute("UPDATE tasks SET stage_key = type WHERE stage_key IS NULL")
    conn.execute("UPDATE projects SET pipeline_template_id = :tid WHERE pipeline_template_id IS NULL", {"tid": template_id})

    print("[0070] Malleable pipelines Phase 1 migration complete.")


def down(conn):
    conn.execute("ALTER TABLE projects DROP COLUMN IF EXISTS pipeline_template_id")
    conn.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS stage_key")
    conn.executescript("""
        DROP TABLE IF EXISTS custom_agent_definitions;
        DROP TABLE IF EXISTS project_settings;
        DROP TABLE IF EXISTS archived_files;
        DROP TABLE IF EXISTS project_documents;
        DROP TABLE IF EXISTS pipeline_arch_categories;
        DROP TABLE IF EXISTS pipeline_transitions;
        DROP TABLE IF EXISTS pipeline_stages;
        DROP TABLE IF EXISTS pipeline_stage_groups;
        DROP TABLE IF EXISTS pipeline_templates;
    """)
