description = "training checkpoints"


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS training_checkpoints (
            id              SERIAL PRIMARY KEY,
            checkpoint_name TEXT    NOT NULL,
            model_notes     TEXT    NULL,
            recorded_at     TIMESTAMPTZ NOT NULL DEFAULT now()
        );
    """)


def down(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS training_checkpoints;
    """)
