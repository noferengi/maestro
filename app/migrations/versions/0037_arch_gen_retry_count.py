"""
0037_arch_gen_retry_count

Add ``retry_count`` (INTEGER DEFAULT 0) to ``arch_gen_jobs`` so the scheduler
can cap retries and send an inbox notification when a job is abandoned.
"""


description = "Add retry_count to arch_gen_jobs"


def up(conn):
    conn.execute(
        "ALTER TABLE arch_gen_jobs ADD COLUMN retry_count INTEGER NOT NULL DEFAULT 0"
    )


def down(conn):
    # SQLite does not support DROP COLUMN before 3.35.0; recreate the table.
    conn.executescript("""
        CREATE TABLE arch_gen_jobs_backup AS SELECT * FROM arch_gen_jobs;
        DROP TABLE arch_gen_jobs;
        CREATE TABLE arch_gen_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project         TEXT    NOT NULL,
            category        TEXT    NOT NULL,
            llm_id          INTEGER REFERENCES llms(id),
            budget_id       INTEGER REFERENCES budgets(id),
            status          TEXT    NOT NULL DEFAULT 'pending',
            priority        REAL    NOT NULL DEFAULT 1.0,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            error_message   TEXT,
            created_at      DATETIME,
            completed_at    DATETIME
        );
        INSERT INTO arch_gen_jobs
            SELECT id, project, category, llm_id, budget_id, status, priority,
                   prompt_tokens, completion_tokens, error_message, created_at, completed_at
            FROM arch_gen_jobs_backup;
        DROP TABLE arch_gen_jobs_backup;
    """)
