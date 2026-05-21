description = "training flags"


def up(conn):
    conn.executescript("""
        ALTER TABLE projects
            ADD COLUMN IF NOT EXISTS exclude_from_training BOOLEAN NOT NULL DEFAULT false;
    """)


def down(conn):
    conn.executescript("""
        ALTER TABLE projects DROP COLUMN IF EXISTS exclude_from_training;
    """)
