"""
Migration 0019 — Budget dollar limits + LLM cost tracking.

Adds:
  - dollar_amount column to budgets (REAL, default -1 = infinite)
  - cost_per_million_prompt_tokens / cost_per_million_completion_tokens to llms
  - expenses table — one row per LLM call with microcent costs
  - budget_token_totals view — aggregate token counts per budget
  - idx_expenses_budget_id index
"""

description = "Add dollar_amount to budgets, cost rates to llms, expenses table, token totals view"


def up(conn):
    conn.execute("ALTER TABLE budgets ADD COLUMN dollar_amount REAL DEFAULT -1")
    conn.execute("ALTER TABLE llms ADD COLUMN cost_per_million_prompt_tokens REAL DEFAULT 0.0")
    conn.execute("ALTER TABLE llms ADD COLUMN cost_per_million_completion_tokens REAL DEFAULT 0.0")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS expenses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_entry_id INTEGER REFERENCES budget_entries(id),
            budget_id INTEGER REFERENCES budgets(id),
            llm_id INTEGER REFERENCES llms(id),
            task_id TEXT REFERENCES tasks(id),
            prompt_tokens INTEGER NOT NULL DEFAULT 0,
            completion_tokens INTEGER NOT NULL DEFAULT 0,
            prompt_cost_microcents INTEGER NOT NULL DEFAULT 0,
            completion_cost_microcents INTEGER NOT NULL DEFAULT 0,
            total_cost_microcents INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_expenses_budget_id ON expenses(budget_id)")
    conn.execute("""
        CREATE VIEW IF NOT EXISTS budget_token_totals AS
        SELECT budget_id,
               COUNT(*)                           AS total_entries,
               SUM(prompt_cost)                   AS total_prompt_tokens,
               SUM(generation_cost)               AS total_completion_tokens,
               SUM(prompt_cost + generation_cost) AS total_tokens
        FROM budget_entries GROUP BY budget_id
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP VIEW IF EXISTS budget_token_totals")
    conn.execute("DROP TABLE IF EXISTS expenses")
    conn.commit()
