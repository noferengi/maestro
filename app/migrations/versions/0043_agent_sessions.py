
def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS agent_sessions (
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
            completion_tokens   INTEGER  NOT NULL DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_task_started
        ON agent_sessions(task_id, started_at)
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS agent_sessions")
    conn.execute("DROP INDEX IF EXISTS idx_agent_sessions_task_started")
    conn.commit()


description = "Add agent_sessions table for persistent agent invocation records"
