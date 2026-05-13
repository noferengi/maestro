description = "Maestro evolution: maestro_runs, project_decisions, and task consultation_payload"


def up(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS maestro_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            started_at TEXT NOT NULL,
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            stall_reason TEXT,
            actions_taken TEXT,
            new_task_ids TEXT,
            budget_id INTEGER,
            llm_id INTEGER,
            FOREIGN KEY (budget_id) REFERENCES budgets (id),
            FOREIGN KEY (llm_id) REFERENCES llms (id)
        );

        INSERT INTO maestro_runs (id, project_name, started_at, finished_at, status, stall_reason, actions_taken, new_task_ids, budget_id, llm_id)
        SELECT id, project_name, started_at, finished_at, status, stall_reason, actions_taken, new_task_ids, budget_id, llm_id
        FROM dreamer_runs;

        DROP INDEX IF EXISTS idx_dreamer_runs_project;
        DROP TABLE IF EXISTS dreamer_runs;

        CREATE TABLE IF NOT EXISTS project_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id INTEGER NOT NULL,
            topic TEXT NOT NULL,
            decision TEXT NOT NULL,
            rationale TEXT,
            is_binding INTEGER DEFAULT 1,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (project_id) REFERENCES projects (id)
        );

        ALTER TABLE tasks ADD COLUMN consultation_payload TEXT;
    """)


def down(conn):
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS dreamer_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT NOT NULL,
            started_at TEXT NOT NULL DEFAULT (datetime('now')),
            finished_at TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            stall_reason TEXT,
            actions_taken TEXT,
            new_task_ids TEXT,
            budget_id INTEGER REFERENCES budgets(id),
            llm_id INTEGER REFERENCES llms(id)
        );

        CREATE INDEX IF NOT EXISTS idx_dreamer_runs_project ON dreamer_runs(project_name);

        INSERT INTO dreamer_runs (id, project_name, started_at, finished_at, status, stall_reason, actions_taken, new_task_ids, budget_id, llm_id)
        SELECT id, project_name, started_at, finished_at, status, stall_reason, actions_taken, new_task_ids, budget_id, llm_id
        FROM maestro_runs;

        DROP TABLE IF EXISTS maestro_runs;
        DROP TABLE IF EXISTS project_decisions;
    """)
