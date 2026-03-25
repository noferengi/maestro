"""
Migration 0023 — file_summary_jobs.previous_summary + file_summaries index.

Two changes:
- ALTER TABLE file_summary_jobs ADD COLUMN previous_summary TEXT
  Allows update-aware re-summarization: the scheduler passes the old summary
  to the LLM so it can decide whether the changes are significant.

- CREATE INDEX idx_file_summaries_path ON file_summaries (file_path)
  Enables O(1) lookup by absolute path (used by snapshot builder and
  list_directory).
"""

description = "Add previous_summary column to file_summary_jobs and index file_summaries by path"


def up(conn):
    conn.execute(
        "ALTER TABLE file_summary_jobs ADD COLUMN previous_summary TEXT"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_file_summaries_path "
        "ON file_summaries (file_path)"
    )
    conn.commit()


def down(conn):
    # SQLite does not support DROP COLUMN — recreate without it
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_summary_jobs_new (
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
    conn.execute("""
        INSERT INTO file_summary_jobs_new
        SELECT id, sha1_hash, file_size_bytes, file_path, file_content,
               static_analysis_json, status, priority, llm_id, budget_id,
               task_id, prompt_tokens, completion_tokens, error_message,
               created_at, completed_at
        FROM file_summary_jobs
    """)
    conn.execute("DROP TABLE file_summary_jobs")
    conn.execute("ALTER TABLE file_summary_jobs_new RENAME TO file_summary_jobs")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fsj_status_priority "
        "ON file_summary_jobs(status, priority, created_at)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_fsj_sha1_size "
        "ON file_summary_jobs(sha1_hash, file_size_bytes)"
    )
    conn.execute("DROP INDEX IF EXISTS idx_file_summaries_path")
    conn.commit()
