description = "Add self_mod_merge_log table for tracking self-modification merges"


def up(conn):
    conn.execute("""
        CREATE TABLE self_mod_merge_log (
            id           SERIAL PRIMARY KEY,
            merge_commit TEXT    NOT NULL UNIQUE,
            task_id      TEXT    NOT NULL REFERENCES tasks(id),
            reverted     BOOLEAN NOT NULL DEFAULT false,
            reverted_at  TIMESTAMPTZ NULL,
            created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
        )
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS self_mod_merge_log")
