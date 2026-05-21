description = "Per-project routing table: map pipeline stage keys to specific LLM IDs"


def up(conn):
    conn.execute("""
        CREATE TABLE project_llm_routing (
            id         SERIAL  PRIMARY KEY,
            project_id INTEGER NOT NULL REFERENCES projects(id)  ON DELETE CASCADE,
            stage_key  TEXT    NOT NULL,
            llm_id     INTEGER NOT NULL REFERENCES llms(id)      ON DELETE CASCADE,
            UNIQUE (project_id, stage_key)
        )
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS project_llm_routing")
