"""
0046_dreamer_runs.py
--------------------
Create the dreamer_runs table.

Dreamer is an autonomous project-resurrection agent that fires when a project
has had no pipeline activity for a configurable number of ticks.  Each run is
recorded here for audit / UI display.
"""

description = "Create dreamer_runs table"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS dreamer_runs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name TEXT    NOT NULL,
            started_at   TEXT    NOT NULL DEFAULT (datetime('now')),
            finished_at  TEXT,
            status       TEXT    NOT NULL DEFAULT 'running',
            stall_reason TEXT,
            actions_taken TEXT,
            new_task_ids  TEXT,
            budget_id    INTEGER REFERENCES budgets(id),
            llm_id       INTEGER REFERENCES llms(id)
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_dreamer_runs_project "
        "ON dreamer_runs(project_name)"
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS dreamer_runs")
    conn.commit()
