"""
Migration 0031 — add is_active column to tasks.

Soft-delete support: setting is_active=0 hides a task and all its
descendants from all board queries.  Hard deletes are no longer needed
(and were failing anyway when foreign-key children existed).
"""

description = "add is_active column to tasks (soft-delete)"


def up(conn):
    conn.execute(
        "ALTER TABLE tasks ADD COLUMN is_active INTEGER NOT NULL DEFAULT 1"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_tasks_is_active ON tasks (is_active)"
    )
    conn.commit()
    print("[0031] Added is_active column to tasks.")


def down(conn):
    # SQLite doesn't support DROP COLUMN directly before 3.35.
    # Recreate table without the column.
    conn.execute("""
        CREATE TABLE tasks_backup AS
        SELECT id, title, type, description, owner, tags, content,
               llm_id, budget_id, history, prerequisites, position,
               project, parent_task_id, subdivision_generation,
               is_big_idea, interface_contracts, review_notes,
               demotion_count, demotion_history, map_x, map_y,
               created_at, updated_at
        FROM tasks
    """)
    conn.execute("DROP TABLE tasks")
    conn.execute("ALTER TABLE tasks_backup RENAME TO tasks")
    conn.commit()
    print("[0031] Removed is_active column from tasks.")
