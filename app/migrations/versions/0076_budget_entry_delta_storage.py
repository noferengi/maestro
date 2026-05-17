"""Add prompt_message_count to budget_entries for delta-only storage."""

description = "add prompt_message_count to budget_entries"


def up(conn):
    conn.execute(
        "ALTER TABLE budget_entries ADD COLUMN prompt_message_count INTEGER"
    )


def down(conn):
    # PostgreSQL does not support DROP COLUMN in all versions without VACUUM;
    # for safety just zero it out rather than drop.
    conn.execute(
        "ALTER TABLE budget_entries DROP COLUMN IF EXISTS prompt_message_count"
    )
