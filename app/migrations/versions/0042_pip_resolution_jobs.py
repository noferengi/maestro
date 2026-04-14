
def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS pip_resolution_jobs (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id           TEXT    NOT NULL,
            pip_id            INTEGER NOT NULL,
            stage_blocked_at  TEXT    NOT NULL,
            research_findings TEXT,
            status            TEXT    NOT NULL DEFAULT 'pending',
            created_at        TEXT    NOT NULL,
            FOREIGN KEY (task_id) REFERENCES tasks(id),
            FOREIGN KEY (pip_id)  REFERENCES performance_improvement_plans(id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_pip_resolution_jobs_status
        ON pip_resolution_jobs(status, created_at)
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS pip_resolution_jobs")
    conn.execute("DROP INDEX IF EXISTS idx_pip_resolution_jobs_status")
    conn.commit()


description = "Add pip_resolution_jobs table for scheduler-dispatched PIP resolution agents"
