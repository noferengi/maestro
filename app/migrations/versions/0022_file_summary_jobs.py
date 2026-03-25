"""
Migration 0022 — file_summary_jobs table.

Adds a job queue for scheduler-dispatched file summary LLM calls.
Agents enqueue a job and block on a threading.Event; the scheduler
dispatches the job, calls the LLM, stores the result in file_summaries,
then signals the event so the agent wakes up.

Priority is negative (default -1.0) so file summary jobs sort before
research jobs (priority >= 0.0) — agents are blocked waiting for these.
"""

description = "Add file_summary_jobs table for scheduler-dispatched LLM file summaries"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_summary_jobs (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            sha1_hash            TEXT    NOT NULL,
            file_size_bytes      INTEGER NOT NULL,
            file_path            TEXT    NOT NULL,
            file_content         TEXT    NOT NULL,
            static_analysis_json TEXT,
            status               TEXT    NOT NULL DEFAULT 'pending',
            priority             REAL    NOT NULL DEFAULT -1.0,
            llm_id               INTEGER REFERENCES llms(id),
            budget_id            INTEGER REFERENCES budgets(id),
            task_id              TEXT,
            prompt_tokens        INTEGER DEFAULT 0,
            completion_tokens    INTEGER DEFAULT 0,
            error_message        TEXT,
            created_at           DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at         DATETIME
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fsj_status_priority "
        "ON file_summary_jobs(status, priority, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fsj_sha1_size "
        "ON file_summary_jobs(sha1_hash, file_size_bytes)"
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS file_summary_jobs")
    conn.commit()
