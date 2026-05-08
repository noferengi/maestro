description = "Add viewed_at to tool_bug_reports for unread tracking"


def up(conn):
    conn.executescript("""
        ALTER TABLE tool_bug_reports ADD COLUMN viewed_at DATETIME;
    """)


def down(conn):
    # SQLite <3.35 has no DROP COLUMN — recreate without viewed_at.
    conn.executescript("""
        CREATE TABLE tool_bug_reports_old AS SELECT
            id, task_id, session_id, tool_name, trying_to, expected, actual, created_at
        FROM tool_bug_reports;
        DROP TABLE tool_bug_reports;
        ALTER TABLE tool_bug_reports_old RENAME TO tool_bug_reports;
    """)
