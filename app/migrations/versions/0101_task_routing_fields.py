description = "Add llm_pinned, dispatch_waiting_since, blocked_on_model_id to tasks"


def up(conn):
    conn.execute("""
        ALTER TABLE tasks
            ADD COLUMN llm_pinned             BOOLEAN     NOT NULL DEFAULT false,
            ADD COLUMN dispatch_waiting_since  TIMESTAMPTZ          NULL,
            ADD COLUMN blocked_on_model_id    INTEGER              NULL
                REFERENCES llms(id)
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE tasks
            DROP COLUMN IF EXISTS llm_pinned,
            DROP COLUMN IF EXISTS dispatch_waiting_since,
            DROP COLUMN IF EXISTS blocked_on_model_id
    """)
