
def up(conn):
    conn.execute(
        "ALTER TABLE planning_results "
        "ADD COLUMN correction_attempts INTEGER NOT NULL DEFAULT 0"
    )
    conn.commit()


def down(conn):
    # SQLite cannot drop columns; no-op
    pass


description = "add correction_attempts column to planning_results"
