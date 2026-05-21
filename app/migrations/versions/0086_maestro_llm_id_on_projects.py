description = "Add maestro_llm_id FK column to projects table"


def up(conn):
    conn.execute(
        "ALTER TABLE projects ADD COLUMN IF NOT EXISTS maestro_llm_id INTEGER REFERENCES llms(id)"
    )


def down(conn):
    conn.execute("ALTER TABLE projects DROP COLUMN IF EXISTS maestro_llm_id")
