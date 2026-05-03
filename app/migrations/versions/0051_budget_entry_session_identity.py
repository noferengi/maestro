"""Add session_id and agent_name to budget_entries.

Each LLM call is now tagged with:
  session_id  — UUID shared by all calls within one agent run
  agent_name  — human-readable agent label (e.g. "Subdivision Agent")

This allows the diagnostics page to group entries by session without heuristics.
Old entries have NULL for both columns; the diagnostics page falls back to the
prompt-size multi-stream heuristic for those.
"""


def up(conn):
    conn.execute("""
        ALTER TABLE budget_entries
        ADD COLUMN session_id TEXT NULL
    """)
    conn.execute("""
        ALTER TABLE budget_entries
        ADD COLUMN agent_name TEXT NULL
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_budget_entries_session_id
        ON budget_entries (session_id)
    """)


def down(conn):
    # SQLite does not support DROP COLUMN — recreate without the columns.
    # Drop the view first; SQLite validates views that reference budget_entries
    # during the rename, so it must be absent while the table swap is in progress.
    conn.execute("DROP VIEW IF EXISTS budget_token_totals")
    conn.execute("""
        CREATE TABLE budget_entries_backup AS
        SELECT id, llm_id, budget_id, task_id,
               prompt_cost, generation_cost, tool_calls,
               prompt_data, response_data, created_at
        FROM budget_entries
    """)
    conn.execute("DROP TABLE budget_entries")
    conn.execute("ALTER TABLE budget_entries_backup RENAME TO budget_entries")
    conn.execute("""
        CREATE VIEW IF NOT EXISTS budget_token_totals AS
        SELECT budget_id,
               COUNT(*)                           AS total_entries,
               SUM(prompt_cost)                   AS total_prompt_tokens,
               SUM(generation_cost)               AS total_completion_tokens,
               SUM(prompt_cost + generation_cost) AS total_tokens
        FROM budget_entries GROUP BY budget_id
    """)


description = "Add session_id and agent_name to budget_entries for diagnostics grouping"
