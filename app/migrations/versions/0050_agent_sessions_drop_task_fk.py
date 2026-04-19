
def up(conn):
    # SQLite cannot ALTER COLUMN to drop a FK constraint — requires table rebuild.
    # Recreate agent_sessions without the FOREIGN KEY (task_id) REFERENCES tasks(id)
    # so that survey jobs (task_id='survey-N') and other background agents that are
    # not tied to a real task row can record sessions without integrity errors.
    conn.execute("""
        CREATE TABLE agent_sessions_new (
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
        INSERT INTO agent_sessions_new
            SELECT id, task_id, agent_type, started_at, ended_at, turn_count,
                   max_turns, exit_reason, exit_summary, scheduler_reason,
                   llm_id, budget_id, prompt_tokens, completion_tokens
            FROM agent_sessions
    """)
    conn.execute("DROP TABLE agent_sessions")
    conn.execute("ALTER TABLE agent_sessions_new RENAME TO agent_sessions")
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_agent_sessions_task_started
        ON agent_sessions(task_id, started_at)
    """)
    conn.commit()


def down(conn):
    # Restore with FK constraint (re-adds it; existing rows with synthetic IDs
    # may violate the constraint, so this down migration is best-effort only).
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
            completion_tokens   INTEGER  NOT NULL DEFAULT 0,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        )
    """)
    conn.execute("""
        INSERT OR IGNORE INTO agent_sessions_old
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


description = "Drop FK constraint on agent_sessions.task_id to allow synthetic task IDs (survey-N etc)"
