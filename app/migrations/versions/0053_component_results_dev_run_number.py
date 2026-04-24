description = "Add dev_run_number to component_results for per-run isolation"


def up(conn):
    conn.execute(
        "ALTER TABLE component_results ADD COLUMN dev_run_number INTEGER NOT NULL DEFAULT 0"
    )


def down(conn):
    # SQLite has no DROP COLUMN before 3.35; recreate the table without the column.
    conn.executescript("""
        CREATE TABLE component_results_old AS
            SELECT id, task_id, component_name, step_order, batch_number,
                   status, files_changed, tests_passed, turns_used,
                   error_detail, prompt_tokens, completion_tokens,
                   created_at, completed_at
            FROM component_results;

        DROP TABLE component_results;

        ALTER TABLE component_results_old RENAME TO component_results;
    """)
