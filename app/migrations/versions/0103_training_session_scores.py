description = "training session scores"


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS training_session_scores (
            session_id   TEXT        PRIMARY KEY,
            task_id      TEXT        NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            score        FLOAT       NOT NULL,
            tags         JSONB       NOT NULL DEFAULT '[]',
            qualified    BOOLEAN     NOT NULL,
            scored_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            exported_at  TIMESTAMPTZ NULL
        );
        CREATE INDEX IF NOT EXISTS ix_training_session_scores_qualified_unexported
            ON training_session_scores (score DESC)
            WHERE qualified = true AND exported_at IS NULL;
    """)


def down(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS training_session_scores;
    """)
