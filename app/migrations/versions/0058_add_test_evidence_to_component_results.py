"""Add test_output and coverage_pct to component_results for test evidence tracking.

Revision ID: 0058
Revises: 0057
Create Date: 2026-05-04
"""

revision = "0058"
down_revision = "0057"
branch_labels = None
description = "Add test_output and coverage_pct columns to component_results"


def up(conn):
    conn.execute(
        "ALTER TABLE component_results ADD COLUMN test_output TEXT"
    )
    conn.execute(
        "ALTER TABLE component_results ADD COLUMN coverage_pct REAL"
    )


def down(conn):
    # SQLite has no DROP COLUMN before 3.35; recreate the table without the columns.
    conn.executescript("""
        CREATE TABLE component_results_old AS
            SELECT id, task_id, component_name, step_order, batch_number,
                   dev_run_number, status, files_changed, tests_passed,
                   turns_used, error_detail, prompt_tokens, completion_tokens,
                   created_at, completed_at
            FROM component_results;

        DROP TABLE component_results;

        ALTER TABLE component_results_old RENAME TO component_results;
    """)
