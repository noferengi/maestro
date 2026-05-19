description = "Add maestro_goals table for persistent goal memory"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS maestro_goals (
            id           SERIAL       PRIMARY KEY,
            project_id   INTEGER      NOT NULL REFERENCES projects(id),
            title        TEXT         NOT NULL,
            statement    TEXT         NOT NULL,
            criteria     JSONB,
            status       TEXT         NOT NULL DEFAULT 'active',
            evidence     TEXT,
            progress     FLOAT        NOT NULL DEFAULT 0.0,
            last_verdict JSONB,
            parent_id    INTEGER      REFERENCES maestro_goals(id),
            priority     INTEGER      NOT NULL DEFAULT 1,
            color        TEXT,
            created_by   TEXT         NOT NULL DEFAULT 'human',
            arch_card_id TEXT         REFERENCES tasks(id),
            created_at   TIMESTAMPTZ  DEFAULT NOW(),
            updated_at   TIMESTAMPTZ  DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_maestro_goals_project_id
        ON maestro_goals (project_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_maestro_goals_status
        ON maestro_goals (status)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS maestro_goals")
