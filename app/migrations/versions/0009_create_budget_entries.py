"""
Migration 0009 — Create budget_entries table for LLM usage tracking.

Tracks every LLM call: tokens in/out, tool call count, and full
prompt/response payloads so we can build datasets and audit costs.
"""

description = "Create budget_entries table for LLM usage tracking"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS budget_entries (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            llm_id      INTEGER     REFERENCES llms(id),
            budget_id   INTEGER     REFERENCES budgets(id),
            task_id     TEXT        REFERENCES tasks(id),
            prompt_cost     INTEGER NOT NULL DEFAULT 0,
            generation_cost INTEGER NOT NULL DEFAULT 0,
            tool_calls      INTEGER NOT NULL DEFAULT 0,
            prompt_data     TEXT,
            response_data   TEXT,
            created_at  DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_budget_entries_budget ON budget_entries(budget_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_budget_entries_llm    ON budget_entries(llm_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_budget_entries_task   ON budget_entries(task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS budget_entries")
    conn.commit()
