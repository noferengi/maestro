"""
0038_planning_result_gate_checks

Add ``gate_checks`` (TEXT) to ``planning_results`` so the 7-check planning
gate result is persisted alongside the planning artifacts.  Previously the
gate was evaluated, the pass/fail decision was acted on, and then the check
details were discarded.
"""

description = "Add gate_checks to planning_results"


def up(conn):
    conn.execute(
        "ALTER TABLE planning_results ADD COLUMN gate_checks TEXT"
    )


def down(conn):
    # SQLite does not support DROP COLUMN before 3.35.0; recreate the table.
    conn.executescript("""
        CREATE TABLE planning_results_backup AS SELECT * FROM planning_results;
        DROP TABLE planning_results;
        CREATE TABLE planning_results (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id               TEXT    NOT NULL,
            file_manifest         TEXT,
            dependency_graph      TEXT,
            interface_contracts   TEXT,
            test_strategy         TEXT,
            implementation_steps  TEXT,
            mermaid_diagrams      TEXT,
            pitfalls_identified   TEXT,
            review_votes          TEXT,
            codebase_survey       TEXT,
            best_of_n_designs     TEXT,
            selected_design_index INTEGER,
            selection_justification TEXT,
            confidence            INTEGER DEFAULT 0,
            prompt_tokens         INTEGER DEFAULT 0,
            completion_tokens     INTEGER DEFAULT 0,
            status                TEXT    NOT NULL DEFAULT 'active',
            created_at            DATETIME
        );
        INSERT INTO planning_results
            SELECT id, task_id, file_manifest, dependency_graph,
                   interface_contracts, test_strategy, implementation_steps,
                   mermaid_diagrams, pitfalls_identified, review_votes,
                   codebase_survey, best_of_n_designs, selected_design_index,
                   selection_justification, confidence, prompt_tokens,
                   completion_tokens, status, created_at
            FROM planning_results_backup;
        DROP TABLE planning_results_backup;
    """)
