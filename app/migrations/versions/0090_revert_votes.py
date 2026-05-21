description = "Add revert_votes table for self-modification rollback voting"


def up(conn):
    conn.execute("""
        CREATE TABLE revert_votes (
            id           SERIAL PRIMARY KEY,
            task_id      TEXT    NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            merge_commit TEXT    NOT NULL,
            reason       TEXT    NOT NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)
    conn.execute("CREATE INDEX ON revert_votes (merge_commit)")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS revert_votes")
