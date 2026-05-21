description = "Add parent_id and created_by to autopilot_objectives"


def up(conn):
    conn.execute("""
        ALTER TABLE autopilot_objectives
            ADD COLUMN parent_id  INTEGER NULL
                REFERENCES autopilot_objectives(id) ON DELETE SET NULL,
            ADD COLUMN created_by TEXT NOT NULL DEFAULT 'human'
                CHECK (created_by IN ('human', 'maestro'))
    """)


def down(conn):
    conn.execute("ALTER TABLE autopilot_objectives DROP COLUMN IF EXISTS parent_id")
    conn.execute("ALTER TABLE autopilot_objectives DROP COLUMN IF EXISTS created_by")
