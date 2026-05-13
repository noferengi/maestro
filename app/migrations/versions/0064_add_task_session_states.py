description = "Add task_session_states table for consultative pause/resume"


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS task_session_states (
            task_id TEXT PRIMARY KEY,
            session_id INTEGER NOT NULL,
            turn_count INTEGER NOT NULL,
            messages TEXT NOT NULL,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (task_id) REFERENCES tasks (id),
            FOREIGN KEY (session_id) REFERENCES agent_sessions (id)
        );
    """)


def down(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS task_session_states;
    """)
