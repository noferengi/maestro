description = "Add capabilities, supports_tools, supports_vision columns to llms"


def up(conn):
    conn.execute("""
        ALTER TABLE llms
            ADD COLUMN capabilities   JSONB   NOT NULL DEFAULT '[]',
            ADD COLUMN supports_tools  BOOLEAN NOT NULL DEFAULT true,
            ADD COLUMN supports_vision BOOLEAN NOT NULL DEFAULT false
    """)


def down(conn):
    conn.execute("""
        ALTER TABLE llms
            DROP COLUMN IF EXISTS capabilities,
            DROP COLUMN IF EXISTS supports_tools,
            DROP COLUMN IF EXISTS supports_vision
    """)
