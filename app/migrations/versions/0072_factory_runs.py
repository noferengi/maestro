description = "add factory_runs audit table"


def up(conn):
    if conn.is_postgres:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS factory_runs (
                id               SERIAL      PRIMARY KEY,
                factory_stage_id INTEGER     NOT NULL REFERENCES pipeline_stages(id),
                project_id       INTEGER     NOT NULL REFERENCES projects(id),
                trigger_type     TEXT        NOT NULL,
                trigger_card_id  TEXT        REFERENCES tasks(id),
                started_at       TIMESTAMPTZ DEFAULT NOW(),
                completed_at     TIMESTAMPTZ,
                cards_created    INTEGER     DEFAULT 0,
                status           TEXT        NOT NULL DEFAULT 'running'
            )
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_factory_runs_stage
            ON factory_runs (factory_stage_id)
        """)
        conn.execute("""
            CREATE INDEX IF NOT EXISTS ix_factory_runs_trigger_card
            ON factory_runs (factory_stage_id, trigger_card_id)
        """)
    else:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS factory_runs (
                id               INTEGER     PRIMARY KEY AUTOINCREMENT,
                factory_stage_id INTEGER     NOT NULL,
                project_id       INTEGER     NOT NULL,
                trigger_type     TEXT        NOT NULL,
                trigger_card_id  TEXT,
                started_at       DATETIME    DEFAULT CURRENT_TIMESTAMP,
                completed_at     DATETIME,
                cards_created    INTEGER     DEFAULT 0,
                status           TEXT        NOT NULL DEFAULT 'running'
            )
        """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS factory_runs")
