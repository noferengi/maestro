description = "add max_turns, max_tokens, user_prompt_template to custom_agent_definitions"


def up(conn):
    if conn.is_postgres:
        conn.executescript("""
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS max_turns INTEGER;
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS max_tokens INTEGER;
            ALTER TABLE custom_agent_definitions ADD COLUMN IF NOT EXISTS user_prompt_template TEXT;
        """)
    else:
        # SQLite does not support ADD COLUMN IF NOT EXISTS; use separate statements
        # wrapped in try/except at the conn level. The runner uses executescript which
        # stops on first error, so we use individual execute() calls.
        for stmt in [
            "ALTER TABLE custom_agent_definitions ADD COLUMN max_turns INTEGER",
            "ALTER TABLE custom_agent_definitions ADD COLUMN max_tokens INTEGER",
            "ALTER TABLE custom_agent_definitions ADD COLUMN user_prompt_template TEXT",
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # column already exists


def down(conn):
    if conn.is_postgres:
        conn.executescript("""
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS max_turns;
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS max_tokens;
            ALTER TABLE custom_agent_definitions DROP COLUMN IF EXISTS user_prompt_template;
        """)
    else:
        # SQLite requires table recreation to drop columns
        conn.executescript("""
            CREATE TABLE custom_agent_definitions_old AS
                SELECT id, name, display_name, description, intent,
                       system_prompt, allowed_tools, gate_type, verifier,
                       verifier_cmd, created_at
                FROM custom_agent_definitions;
            DROP TABLE custom_agent_definitions;
            ALTER TABLE custom_agent_definitions_old RENAME TO custom_agent_definitions;
        """)
