description = "Add planning result cache fields and task cache_mode"


def up(conn):
    conn.executescript("""
        ALTER TABLE planning_results ADD COLUMN content_hash VARCHAR;
        ALTER TABLE planning_results ADD COLUMN was_gate_passed INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE tasks ADD COLUMN cache_mode VARCHAR NOT NULL DEFAULT 'normal';
    """)


def down(conn):
    # SQLite has no DROP COLUMN before 3.35; recreate tables without the new columns.
    conn.executescript("""
        CREATE TABLE planning_results_old AS
            SELECT id, task_id, file_manifest, dependency_graph, interface_contracts,
                   test_strategy, implementation_steps, mermaid_diagrams, pitfalls_identified,
                   review_votes, codebase_survey, best_of_n_designs, selected_design_index,
                   selection_justification, gate_checks, error_message, confidence,
                   prompt_tokens, completion_tokens, status, correction_attempts, created_at
            FROM planning_results;
        DROP TABLE planning_results;
        ALTER TABLE planning_results_old RENAME TO planning_results;

        CREATE TABLE tasks_old AS
            SELECT id, title, type, description, owner, tags, content, llm_id, budget_id,
                   history, prerequisites, position, project_id, parent_task_id,
                   subdivision_generation, is_big_idea, interface_contracts, review_notes,
                   demotion_count, demotion_history, map_x, map_y, is_active,
                   intake_exhausted_at, created_at, updated_at
            FROM tasks;
        DROP TABLE tasks;
        ALTER TABLE tasks_old RENAME TO tasks;
    """)
