description = "Add watched_events and watch_error_log tables for event-driven triggers (GAP 9)"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watched_events (
            id                 SERIAL PRIMARY KEY,
            project_id         INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            event_type         TEXT    NOT NULL CHECK (event_type IN ('webhook', 'file_watch', 'api_poll')),
            label              TEXT    NOT NULL,
            source_config      JSONB   NOT NULL DEFAULT '{}',
            fire_config        JSONB   NOT NULL DEFAULT '{}',
            status             TEXT    NOT NULL DEFAULT 'active'
                                   CHECK (status IN ('active', 'paused', 'expired')),
            last_fired_at      TIMESTAMPTZ NULL,
            last_payload_hash  TEXT    NULL,
            fire_count         INTEGER NOT NULL DEFAULT 0,
            created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
            created_by_session TEXT    NULL
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS watched_events_project_status_idx
        ON watched_events (project_id, status, event_type)
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS watch_error_log (
            id         SERIAL PRIMARY KEY,
            watch_id   INTEGER NOT NULL REFERENCES watched_events(id) ON DELETE CASCADE,
            error      TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS watch_error_log_watch_idx
        ON watch_error_log (watch_id, created_at DESC)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS watch_error_log")
    conn.execute("DROP TABLE IF EXISTS watched_events")
