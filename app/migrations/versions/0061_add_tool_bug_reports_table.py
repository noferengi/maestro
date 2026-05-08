description = "Add tool_bug_reports table for agent-filed tool failure reports"


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS tool_bug_reports (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id     TEXT NOT NULL,
            session_id  INTEGER,
            tool_name   TEXT NOT NULL,
            trying_to   TEXT NOT NULL,
            expected    TEXT NOT NULL,
            actual      TEXT NOT NULL,
            created_at  DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES agent_sessions(id)
        );
        CREATE INDEX IF NOT EXISTS idx_tbr_task_id  ON tool_bug_reports(task_id);
        CREATE INDEX IF NOT EXISTS idx_tbr_tool     ON tool_bug_reports(tool_name);
        CREATE INDEX IF NOT EXISTS idx_tbr_created  ON tool_bug_reports(created_at);
    """)


def down(conn):
    conn.executescript("""
        DROP INDEX IF EXISTS idx_tbr_created;
        DROP INDEX IF EXISTS idx_tbr_tool;
        DROP INDEX IF EXISTS idx_tbr_task_id;
        DROP TABLE IF EXISTS tool_bug_reports;
    """)
