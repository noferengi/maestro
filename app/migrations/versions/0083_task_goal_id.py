description = "Add goal_id FK column to tasks table"


def up(conn):
    conn.execute("""
        ALTER TABLE tasks
        ADD COLUMN IF NOT EXISTS goal_id INTEGER REFERENCES maestro_goals(id)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS ix_tasks_goal_id ON tasks (goal_id)
    """)


def down(conn):
    conn.execute("DROP INDEX IF EXISTS ix_tasks_goal_id")
    conn.execute("ALTER TABLE tasks DROP COLUMN IF EXISTS goal_id")
