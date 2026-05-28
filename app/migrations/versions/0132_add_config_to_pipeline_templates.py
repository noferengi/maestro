description = "add config jsonb to pipeline_templates for kanban column band definitions"


def up(conn):
    conn.execute("""
        ALTER TABLE pipeline_templates
        ADD COLUMN IF NOT EXISTS config JSONB NOT NULL DEFAULT '{}'
    """)


def down(conn):
    conn.execute("ALTER TABLE pipeline_templates DROP COLUMN IF EXISTS config")
