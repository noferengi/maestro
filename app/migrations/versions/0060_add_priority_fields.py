description = "add last_progress_at and is_starred to tasks"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN last_progress_at DATETIME")
    conn.execute("ALTER TABLE tasks ADD COLUMN is_starred INTEGER NOT NULL DEFAULT 0")

    # Backfill last_progress_at: prefer latest budget entry, then updated_at, then created_at
    conn.execute("""
        UPDATE tasks SET last_progress_at = (
            SELECT MAX(created_at) FROM budget_entries WHERE budget_entries.task_id = tasks.id
        ) WHERE EXISTS (
            SELECT 1 FROM budget_entries WHERE budget_entries.task_id = tasks.id
        )
    """)
    conn.execute("""
        UPDATE tasks SET last_progress_at = updated_at
        WHERE last_progress_at IS NULL AND updated_at IS NOT NULL
    """)
    conn.execute("""
        UPDATE tasks SET last_progress_at = created_at
        WHERE last_progress_at IS NULL AND created_at IS NOT NULL
    """)
    conn.execute("""
        UPDATE tasks SET last_progress_at = CURRENT_TIMESTAMP
        WHERE last_progress_at IS NULL
    """)
    conn.commit()


def down(conn):
    # SQLite <3.35 has no DROP COLUMN — recreate table without the two new columns.
    # Columns match schema after migration 0059 (acceptance_criteria present).
    conn.execute("BEGIN TRANSACTION")
    conn.execute("ALTER TABLE tasks RENAME TO tasks_old")
    conn.execute("""
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            type TEXT NOT NULL DEFAULT 'idea',
            description TEXT,
            owner TEXT,
            tags TEXT,
            content TEXT,
            llm_id TEXT,
            budget_id TEXT,
            history TEXT,
            prerequisites TEXT,
            interface_contracts TEXT,
            review_notes TEXT,
            demotion_count INTEGER DEFAULT 0,
            demotion_history TEXT,
            map_x REAL,
            map_y REAL,
            is_active INTEGER DEFAULT 1,
            project_id TEXT,
            parent_task_id TEXT,
            subdivision_generation INTEGER DEFAULT 0,
            is_big_idea INTEGER DEFAULT 0,
            position INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            clarification_status TEXT NOT NULL DEFAULT 'none',
            description_original TEXT,
            intake_exhausted_at TEXT,
            cache_mode VARCHAR NOT NULL DEFAULT 'normal',
            acceptance_criteria TEXT
        )
    """)
    conn.execute("""
        INSERT INTO tasks (
            id, title, type, description, owner, tags, content, llm_id, budget_id,
            history, prerequisites, interface_contracts, review_notes, demotion_count,
            demotion_history, map_x, map_y, is_active, project_id, parent_task_id,
            subdivision_generation, is_big_idea, position, created_at, updated_at,
            clarification_status, description_original, intake_exhausted_at, cache_mode,
            acceptance_criteria
        )
        SELECT
            id, title, type, description, owner, tags, content, llm_id, budget_id,
            history, prerequisites, interface_contracts, review_notes, demotion_count,
            demotion_history, map_x, map_y, is_active, project_id, parent_task_id,
            subdivision_generation, is_big_idea, position, created_at, updated_at,
            clarification_status, description_original, intake_exhausted_at, cache_mode,
            acceptance_criteria
        FROM tasks_old
    """)
    conn.execute("DROP TABLE tasks_old")
    conn.execute("COMMIT")
