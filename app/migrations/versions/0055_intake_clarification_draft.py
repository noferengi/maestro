description = "intake clarification draft"


def up(conn):
    conn.executescript("""
        ALTER TABLE tasks ADD COLUMN clarification_status TEXT NOT NULL DEFAULT 'none';
        ALTER TABLE tasks ADD COLUMN description_original TEXT;

        CREATE TABLE IF NOT EXISTS intake_drafts (
            id                      INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id                 TEXT NOT NULL UNIQUE,
            rewritten_description   TEXT,
            design_rationale        TEXT,
            acceptance_criteria     TEXT,
            out_of_scope            TEXT,
            open_questions          TEXT,
            suggested_prerequisites TEXT,
            suggested_subtasks      TEXT,
            conversation_history    TEXT,
            agent_token_cost        INTEGER,
            created_at              TEXT NOT NULL,
            updated_at              TEXT NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id)
        );

        CREATE INDEX IF NOT EXISTS idx_intake_drafts_task_id ON intake_drafts(task_id);

        UPDATE tasks SET clarification_status = 'skipped' WHERE type = 'idea';
    """)


def down(conn):
    conn.executescript("""
        DROP TABLE IF EXISTS intake_drafts;

        CREATE TABLE tasks_new AS SELECT * FROM tasks;
        DROP TABLE tasks;
        CREATE TABLE tasks AS SELECT
            id, title, type, description, owner, tags, content,
            llm_id, budget_id, history, prerequisites, position,
            project_id, parent_task_id, subdivision_generation, is_big_idea,
            interface_contracts, review_notes, demotion_count, demotion_history,
            map_x, map_y, is_active, intake_exhausted_at, cache_mode,
            created_at, updated_at
        FROM tasks_new;
        DROP TABLE tasks_new;
    """)
