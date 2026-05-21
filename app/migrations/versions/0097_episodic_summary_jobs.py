description = "Add episodic_summary_jobs table for async session-end LLM summarisation"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodic_summary_jobs (
            id           SERIAL PRIMARY KEY,
            task_id      TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            final_status TEXT    NOT NULL,
            status       TEXT    NOT NULL DEFAULT 'pending',
            priority     FLOAT   NOT NULL DEFAULT 0.5,
            tier         INTEGER NOT NULL DEFAULT 2,
            llm_id       INTEGER NULL REFERENCES llms(id) ON DELETE SET NULL,
            budget_id    INTEGER NULL REFERENCES budgets(id) ON DELETE SET NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
            completed_at TIMESTAMPTZ NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS episodic_summary_jobs_status_idx
        ON episodic_summary_jobs (status, tier, priority, created_at)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS episodic_summary_jobs")
