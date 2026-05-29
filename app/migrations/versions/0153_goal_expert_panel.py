description = "Add max_iterations/iteration_count/achieved_at to maestro_goals; add goal_expert_votes table"


def up(conn):
    conn.execute("""
        ALTER TABLE maestro_goals
        ADD COLUMN IF NOT EXISTS max_iterations INTEGER NOT NULL DEFAULT 10
    """)
    conn.execute("""
        ALTER TABLE maestro_goals
        ADD COLUMN IF NOT EXISTS iteration_count INTEGER NOT NULL DEFAULT 0
    """)
    conn.execute("""
        ALTER TABLE maestro_goals
        ADD COLUMN IF NOT EXISTS achieved_at TIMESTAMP
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS goal_expert_votes (
            id                  SERIAL PRIMARY KEY,
            goal_id             INTEGER NOT NULL REFERENCES maestro_goals(id),
            iteration           INTEGER NOT NULL,
            judge_index         INTEGER NOT NULL,
            judge_persona       TEXT NOT NULL,
            verdict             TEXT NOT NULL,
            justification       TEXT NOT NULL,
            model               TEXT,
            prompt_tokens       INTEGER DEFAULT 0,
            completion_tokens   INTEGER DEFAULT 0,
            created_at          TIMESTAMP NOT NULL DEFAULT NOW()
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_goal_expert_votes_goal_id
        ON goal_expert_votes (goal_id)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS goal_expert_votes")
    conn.execute("ALTER TABLE maestro_goals DROP COLUMN IF EXISTS achieved_at")
    conn.execute("ALTER TABLE maestro_goals DROP COLUMN IF EXISTS iteration_count")
    conn.execute("ALTER TABLE maestro_goals DROP COLUMN IF EXISTS max_iterations")
