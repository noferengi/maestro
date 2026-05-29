description = "Add tags JSONB to pipeline_templates"


def up(conn):
    conn.execute("""
        ALTER TABLE pipeline_templates
        ADD COLUMN IF NOT EXISTS tags JSONB NOT NULL DEFAULT '[]'
    """)


def down(conn):
    conn.execute("ALTER TABLE pipeline_templates DROP COLUMN IF EXISTS tags")
