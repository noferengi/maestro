description = "Add autopilot_objectives table, tasks.autopilot_objective_id FK, project autopilot columns"


def up(conn):
    conn.execute("""
        CREATE TABLE autopilot_objectives (
            id                      SERIAL PRIMARY KEY,
            project_id              INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            description             TEXT    NOT NULL,
            priority                INTEGER NOT NULL DEFAULT 5,
            status                  TEXT    NOT NULL DEFAULT 'active'
                                        CHECK (status IN ('active', 'paused', 'complete')),
            time_box_hours          INTEGER NULL,
            created_at              TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at              TIMESTAMPTZ NULL,
            completed_at            TIMESTAMPTZ NULL,
            last_assessment         TEXT    NULL,
            assessment_tick         INTEGER NULL,
            appears_complete_since  TIMESTAMPTZ NULL
        )
    """)

    conn.execute("""
        ALTER TABLE tasks
            ADD COLUMN autopilot_objective_id INTEGER NULL
                REFERENCES autopilot_objectives(id) ON DELETE SET NULL
    """)

    conn.execute("""
        ALTER TABLE projects
            ADD COLUMN autopilot_budget_id INTEGER NULL REFERENCES budgets(id),
            ADD COLUMN autopilot_max_in_flight INTEGER NOT NULL DEFAULT 10
    """)


def down(conn):
    conn.execute("ALTER TABLE projects DROP COLUMN IF EXISTS autopilot_max_in_flight")
    conn.execute("ALTER TABLE projects DROP COLUMN IF EXISTS autopilot_budget_id")
    conn.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS autopilot_objective_id")
    conn.execute("DROP TABLE IF EXISTS autopilot_objectives")
