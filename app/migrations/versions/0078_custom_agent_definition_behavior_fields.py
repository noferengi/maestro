description = "add behavior_type, behavior_config, is_builtin to custom_agent_definitions"


def up(conn):
    if conn.is_postgres:
        conn.executescript("""
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS behavior_type VARCHAR;
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS behavior_config JSONB;
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS is_builtin BOOLEAN NOT NULL DEFAULT FALSE;
        """)
    else:
        for stmt in [
            "ALTER TABLE custom_agent_definitions ADD COLUMN behavior_type VARCHAR",
            "ALTER TABLE custom_agent_definitions ADD COLUMN behavior_config TEXT",
            "ALTER TABLE custom_agent_definitions ADD COLUMN is_builtin INTEGER NOT NULL DEFAULT 0",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass


def down(conn):
    if conn.is_postgres:
        conn.executescript("""
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS behavior_type;
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS behavior_config;
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS is_builtin;
        """)
