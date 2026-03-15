"""
Migration 0005: Create llms and budgets reference tables.

- Creates `llms` table (address, port, model, settings JSON) with uniqueness on the triplet.
- Creates `budgets` table (name, settings JSON) for extensible budget config.
- Seeds one default LLM and one default Budget.
- Converts tasks.llm (JSON) → tasks.llm_id (INTEGER FK) and
  tasks.budget (TEXT) → tasks.budget_id (INTEGER FK), pointing all
  existing rows at the newly-seeded defaults.
"""

description = "Create llms/budgets tables, convert task columns to FK references"


def up(conn):
    # ---- reference tables ------------------------------------------------
    conn.execute("""
        CREATE TABLE IF NOT EXISTS llms (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            address  TEXT    NOT NULL DEFAULT 'localhost',
            port     INTEGER NOT NULL DEFAULT 8008,
            model    TEXT    NOT NULL DEFAULT '',
            settings JSON,
            UNIQUE(address, port, model)
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS budgets (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            name     TEXT    NOT NULL UNIQUE,
            settings JSON
        )
    """)
    conn.commit()

    # ---- seed defaults ---------------------------------------------------
    conn.execute("""
        INSERT OR IGNORE INTO llms (address, port, model)
        VALUES ('localhost', 8008, 'Qwen3p5-Omnicoder-9B')
    """)
    conn.execute("""
        INSERT OR IGNORE INTO budgets (name)
        VALUES ('Default Budget')
    """)
    conn.commit()

    # Grab the IDs we just inserted (or that already existed)
    default_llm_id = conn.execute(
        "SELECT id FROM llms WHERE address='localhost' AND port=8008 AND model='Qwen3p5-Omnicoder-9B'"
    ).fetchone()[0]
    default_budget_id = conn.execute(
        "SELECT id FROM budgets WHERE name='Default Budget'"
    ).fetchone()[0]

    # ---- rebuild tasks table with llm_id / budget_id ---------------------
    conn.execute("""
        CREATE TABLE tasks_new (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            type          TEXT NOT NULL,
            description   TEXT,
            owner         TEXT DEFAULT 'user',
            tags          JSON,
            content       JSON,
            llm_id        INTEGER REFERENCES llms(id),
            budget_id     INTEGER REFERENCES budgets(id),
            history       JSON,
            position      INTEGER DEFAULT 0,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            prerequisites JSON,
            project       TEXT DEFAULT 'TheMaestro'
        )
    """)

    conn.execute(f"""
        INSERT INTO tasks_new
            (id, title, type, description, owner, tags, content,
             llm_id, budget_id,
             history, position, created_at, updated_at, prerequisites, project)
        SELECT
            id, title, type, description, owner, tags, content,
            {default_llm_id}, {default_budget_id},
            history, position, created_at, updated_at, prerequisites, project
        FROM tasks
    """)

    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.commit()


def down(conn):
    """Revert: rebuild tasks with old llm/budget columns, drop reference tables."""
    conn.execute("""
        CREATE TABLE tasks_new (
            id            TEXT PRIMARY KEY,
            title         TEXT NOT NULL,
            type          TEXT NOT NULL,
            description   TEXT,
            owner         TEXT DEFAULT 'user',
            tags          JSON,
            content       JSON,
            llm           JSON,
            budget        TEXT DEFAULT '',
            history       JSON,
            position      INTEGER DEFAULT 0,
            created_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at    DATETIME DEFAULT CURRENT_TIMESTAMP,
            prerequisites JSON,
            project       TEXT DEFAULT 'TheMaestro'
        )
    """)
    conn.execute("""
        INSERT INTO tasks_new
            (id, title, type, description, owner, tags, content,
             llm, budget,
             history, position, created_at, updated_at, prerequisites, project)
        SELECT
            id, title, type, description, owner, tags, content,
            NULL, '',
            history, position, created_at, updated_at, prerequisites, project
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_new RENAME TO tasks")
    conn.execute("DROP TABLE IF EXISTS budgets")
    conn.execute("DROP TABLE IF EXISTS llms")
    conn.commit()
