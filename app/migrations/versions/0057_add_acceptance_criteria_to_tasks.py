description = "add acceptance criteria to tasks"


def up(conn):
    conn.execute("ALTER TABLE tasks ADD COLUMN acceptance_criteria TEXT")


def down(conn):
    # SQLite has no DROP COLUMN before 3.35.
    # Recreate the table without the acceptance_criteria column.
    # Columns must match the schema as it exists before migration 0057 runs:
    #   base (0001) + intake_exhausted_at (0047) + cache_mode (0054) +
    #   clarification_status, description_original (0055)
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
            cache_mode VARCHAR NOT NULL DEFAULT 'normal'
        )
    """)
    conn.execute("""
        INSERT INTO tasks (id, title, type, description, owner, tags, content, llm_id, budget_id,
            history, prerequisites, interface_contracts, review_notes, demotion_count,
            demotion_history, map_x, map_y, is_active, project_id, parent_task_id,
            subdivision_generation, is_big_idea, position, created_at, updated_at,
            clarification_status, description_original, intake_exhausted_at, cache_mode)
        SELECT id, title, type, description, owner, tags, content, llm_id, budget_id,
            history, prerequisites, interface_contracts, review_notes, demotion_count,
            demotion_history, map_x, map_y, is_active, project_id, parent_task_id,
            subdivision_generation, is_big_idea, position, created_at, updated_at,
            clarification_status, description_original, intake_exhausted_at, cache_mode
        FROM tasks_old
    """)
    conn.execute("DROP TABLE tasks_old")
    conn.execute("COMMIT")
