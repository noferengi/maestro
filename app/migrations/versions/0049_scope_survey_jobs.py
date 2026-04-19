"""
0049_scope_survey_jobs.py
-------------------------
Create table for project survey background jobs.
"""

description = "Create scope_survey_jobs table"

def up(conn):
    conn.execute("""
        CREATE TABLE scope_survey_jobs (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name    TEXT NOT NULL,
            scope_type      TEXT NOT NULL,
            scope_key       TEXT NOT NULL,
            action          TEXT NOT NULL DEFAULT 'generate', -- 'generate' | 'staleness_check' | 'edit_summary'
            status          TEXT NOT NULL DEFAULT 'pending',  -- 'pending' | 'running' | 'done' | 'failed'
            priority        REAL NOT NULL DEFAULT 0.0,
            llm_id          INTEGER,
            budget_id       INTEGER,
            prompt_tokens   INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            error_message   TEXT,
            retry_count     INTEGER NOT NULL DEFAULT 0,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            completed_at    DATETIME
        )
    """)
    conn.commit()

def down(conn):
    conn.execute("DROP TABLE scope_survey_jobs")
    conn.commit()
