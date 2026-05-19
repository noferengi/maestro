description = "Add goal_verification_jobs table for async goal progress checking"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS goal_verification_jobs (
            id                SERIAL       PRIMARY KEY,
            goal_id           INTEGER      NOT NULL REFERENCES maestro_goals(id),
            status            TEXT         NOT NULL DEFAULT 'pending',
            triggered_by      TEXT,
            result            JSONB,
            error_msg         TEXT,
            llm_id            INTEGER      REFERENCES llms(id),
            budget_id         INTEGER      REFERENCES budgets(id),
            priority          FLOAT        NOT NULL DEFAULT 0.0,
            tier              INTEGER      NOT NULL DEFAULT 2,
            prompt_tokens     INTEGER      NOT NULL DEFAULT 0,
            completion_tokens INTEGER      NOT NULL DEFAULT 0,
            retry_count       INTEGER      NOT NULL DEFAULT 0,
            created_at        TIMESTAMPTZ  DEFAULT NOW(),
            completed_at      TIMESTAMPTZ
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_goal_verification_jobs_goal_id
        ON goal_verification_jobs (goal_id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_goal_verification_jobs_status
        ON goal_verification_jobs (status)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS goal_verification_jobs")
