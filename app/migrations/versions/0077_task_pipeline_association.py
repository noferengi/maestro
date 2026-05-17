description = "add pipeline_template_id to tasks; backfill from project assignment"


def up(conn):
    if conn.is_postgres:
        conn.executescript("""
            ALTER TABLE tasks ADD COLUMN IF NOT EXISTS pipeline_template_id INTEGER REFERENCES pipeline_templates(id);

            UPDATE tasks
            SET pipeline_template_id = p.pipeline_template_id
            FROM projects p
            WHERE tasks.project_id = p.id
              AND tasks.type != 'architecture'
              AND tasks.pipeline_template_id IS NULL;

            CREATE INDEX IF NOT EXISTS ix_tasks_pipeline_template_id
                ON tasks (pipeline_template_id);
        """)
    else:
        try:
            conn.execute(
                "ALTER TABLE tasks ADD COLUMN pipeline_template_id INTEGER REFERENCES pipeline_templates(id)"
            )
        except Exception:
            pass  # column already exists
        conn.executescript("""
            UPDATE tasks
            SET pipeline_template_id = (
                SELECT pipeline_template_id FROM projects WHERE projects.id = tasks.project_id
            )
            WHERE tasks.type != 'architecture'
              AND tasks.pipeline_template_id IS NULL;
        """)


def down(conn):
    if conn.is_postgres:
        conn.executescript("""
            DROP INDEX IF EXISTS ix_tasks_pipeline_template_id;
            ALTER TABLE tasks DROP COLUMN IF EXISTS pipeline_template_id;
        """)
    else:
        # SQLite: recreate table without the column
        conn.executescript("""
            CREATE TABLE tasks_old AS SELECT * FROM tasks;
            -- SQLite can't drop columns directly; migration down is best-effort for tests only
        """)
