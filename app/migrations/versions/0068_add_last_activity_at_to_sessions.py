description = "add last activity at to sessions"


def up(conn):
    conn.execute("ALTER TABLE agent_sessions ADD COLUMN last_activity_at TEXT")


def down(conn):
    if conn.is_postgres:
        conn.execute("ALTER TABLE agent_sessions DROP COLUMN last_activity_at")
        return

    # SQLite <3.35 has no DROP COLUMN — recreate without last_activity_at.
    conn.execute("""
        CREATE TABLE agent_sessions_old (
            id                  INTEGER  PRIMARY KEY AUTOINCREMENT,
            task_id             TEXT     NOT NULL,
            agent_type          TEXT     NOT NULL,
            started_at          TEXT     NOT NULL,
            ended_at            TEXT,
            turn_count          INTEGER,
            max_turns           INTEGER,
            exit_reason         TEXT,
            exit_summary        TEXT,
            scheduler_reason    TEXT     NOT NULL DEFAULT 'scheduler',
            llm_id              INTEGER,
            budget_id           INTEGER,
            prompt_tokens       INTEGER  NOT NULL DEFAULT 0,
            completion_tokens   INTEGER  NOT NULL DEFAULT 0
        )
    """)
    conn.execute("""
        INSERT INTO agent_sessions_old
            SELECT id, task_id, agent_type, started_at, ended_at, turn_count,
                   max_turns, exit_reason, exit_summary, scheduler_reason,
                   llm_id, budget_id, prompt_tokens, completion_tokens
            FROM agent_sessions
    """)
    conn.execute("DROP TABLE agent_sessions")
    conn.execute("ALTER TABLE agent_sessions_old RENAME TO agent_sessions")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_task_started
        ON agent_sessions(task_id, started_at)
    """)
    conn.commit()
