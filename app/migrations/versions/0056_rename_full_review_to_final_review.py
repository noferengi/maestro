description = "rename full review to final review"


def up(conn):
    conn.executescript("""
        -- Rename the results table
        ALTER TABLE full_review_results RENAME TO final_review_results;

        -- Rename the column in merge_records
        ALTER TABLE merge_records RENAME COLUMN full_review_ids TO final_review_ids;

        -- Recreate the index with the new name
        DROP INDEX IF EXISTS idx_full_review_task;
        CREATE INDEX IF NOT EXISTS idx_final_review_task ON final_review_results(task_id);

        -- Update task type values so existing tasks become dispatchable
        UPDATE tasks SET type = 'final_review' WHERE type = 'full_review';
    """)


def down(conn):
    conn.executescript("""
        -- Revert task type values
        UPDATE tasks SET type = 'full_review' WHERE type = 'final_review';

        -- Revert table name
        ALTER TABLE final_review_results RENAME TO full_review_results;

        -- Revert column name
        ALTER TABLE merge_records RENAME COLUMN final_review_ids TO full_review_ids;

        -- Revert index name
        DROP INDEX IF EXISTS idx_final_review_task;
        CREATE INDEX IF NOT EXISTS idx_full_review_task ON full_review_results(task_id);
    """)
