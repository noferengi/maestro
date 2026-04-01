"""
Migration 0035 — arch_gen_jobs table.

Stores scheduler-dispatched jobs that generate architecture category cards
from existing file summaries.  One job per missing category per project.

Schema:
    project         — project name (matches projects.name)
    category        — one of the 14 fixed arch categories
    llm_id          — LLM to use for generation
    budget_id       — budget to charge
    status          — pending | running | completed | failed
    priority        — 1.0 (lower than research 0.0; file summaries are -1.0)
    prompt_tokens / completion_tokens — usage tracking
    error_message   — last error if failed
    created_at / completed_at — timing
"""

description = "Add arch_gen_jobs table for scheduler-dispatched architecture card generation"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS arch_gen_jobs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            project           TEXT    NOT NULL,
            category          TEXT    NOT NULL,
            llm_id            INTEGER REFERENCES llms(id),
            budget_id         INTEGER REFERENCES budgets(id),
            status            TEXT    NOT NULL DEFAULT 'pending',
            priority          REAL    NOT NULL DEFAULT 1.0,
            prompt_tokens     INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            error_message     TEXT,
            created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at      DATETIME
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agj_status_priority
            ON arch_gen_jobs (status, priority, created_at)
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_agj_status_priority")
    conn.execute("DROP TABLE IF EXISTS arch_gen_jobs")
    conn.commit()
