"""
Migration 0020 — Correct expenses table schema + backfill from budget_entries.

0019 created expenses with the wrong shape (nullable budget_id, no remote_call_id,
no total_tokens).  This migration drops and recreates it correctly, then backfills
one expense row for every existing budget_entry that has a budget_id.

Schema:
    budget_entry_id  — FK to our internal call log (1:1 with budget_entries)
    budget_id        — which budget is charged
    llm_id           — which endpoint served the call
    remote_call_id   — the API response "id" field (e.g. chatcmpl-abc123)
    task_id          — task context for per-task cost queries
    prompt_tokens    — exact from API usage.prompt_tokens
    completion_tokens — exact from API usage.completion_tokens
    total_tokens     — stored sum (prompt + completion) for easy aggregation
    prompt_cost_microcents       — 0 when LLM rate = $0.00/M
    completion_cost_microcents   — 0 when LLM rate = $0.00/M
    total_cost_microcents        — stored sum; main value for budget enforcement

Backfill uses json_extract() to pull the remote call ID out of the stored
response_data JSON blob in budget_entries.  Requires SQLite >= 3.38 (Python
3.13 ships 3.45+).
"""

description = "Correct expenses schema (remote_call_id, total_tokens, NOT NULL budget) and backfill"


def up(conn):
    # Drop the 0019 version — wrong schema, no data worth keeping
    conn.execute("DROP TABLE IF EXISTS expenses")

    conn.execute("""
        CREATE TABLE expenses (
            id                          INTEGER PRIMARY KEY AUTOINCREMENT,
            budget_entry_id             INTEGER REFERENCES budget_entries(id),
            budget_id                   INTEGER REFERENCES budgets(id),
            llm_id                      INTEGER REFERENCES llms(id),
            remote_call_id              TEXT,
            task_id                     TEXT REFERENCES tasks(id),
            prompt_tokens               INTEGER NOT NULL DEFAULT 0,
            completion_tokens           INTEGER NOT NULL DEFAULT 0,
            total_tokens                INTEGER NOT NULL DEFAULT 0,
            prompt_cost_microcents      INTEGER NOT NULL DEFAULT 0,
            completion_cost_microcents  INTEGER NOT NULL DEFAULT 0,
            total_cost_microcents       INTEGER NOT NULL DEFAULT 0,
            created_at                  DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expenses_budget_id    ON expenses(budget_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_expenses_entry_id     ON expenses(budget_entry_id)"
    )

    # Backfill: one expense row per budget_entry that has a budget_id.
    # Token counts come from budget_entries.prompt_cost / generation_cost (exact API values).
    # remote_call_id is extracted from the stored response_data JSON blob.
    # Cost rates come from the llms table (0.0 by default = local LLMs).
    conn.execute("""
        INSERT INTO expenses (
            budget_entry_id, budget_id, llm_id, task_id, remote_call_id,
            prompt_tokens, completion_tokens, total_tokens,
            prompt_cost_microcents, completion_cost_microcents, total_cost_microcents,
            created_at
        )
        SELECT
            be.id,
            be.budget_id,
            be.llm_id,
            be.task_id,
            json_extract(be.response_data, '$.id'),
            be.prompt_cost,
            be.generation_cost,
            be.prompt_cost + be.generation_cost,
            CAST(be.prompt_cost     * COALESCE(l.cost_per_million_prompt_tokens, 0.0)     * 100 AS INTEGER),
            CAST(be.generation_cost * COALESCE(l.cost_per_million_completion_tokens, 0.0) * 100 AS INTEGER),
            CAST(
                be.prompt_cost     * COALESCE(l.cost_per_million_prompt_tokens, 0.0)     * 100 +
                be.generation_cost * COALESCE(l.cost_per_million_completion_tokens, 0.0) * 100
            AS INTEGER),
            be.created_at
        FROM budget_entries be
        LEFT JOIN llms l ON l.id = be.llm_id
        WHERE be.budget_id IS NOT NULL
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS expenses")
    conn.commit()
