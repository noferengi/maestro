description = "Add partial index on agent_sessions(last_activity_at) WHERE ended_at IS NULL"


def up(conn) -> None:
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_agent_sessions_last_activity_open "
        "ON agent_sessions (last_activity_at) "
        "WHERE ended_at IS NULL"
    )


def down(conn) -> None:
    conn.execute("DROP INDEX IF EXISTS idx_agent_sessions_last_activity_open")
