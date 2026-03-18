"""
Migration 0012 — Planning results table for pipeline audit trail.

Stores the output of each planning pipeline run including file manifests,
dependency graphs, interface contracts, test strategies, and best-of-N
design selection metadata.
"""

description = "Planning results table for pipeline audit trail"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS planning_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            file_manifest TEXT,
            dependency_graph TEXT,
            interface_contracts TEXT,
            test_strategy TEXT,
            implementation_steps TEXT,
            mermaid_diagrams TEXT,
            pitfalls_identified TEXT,
            review_votes TEXT,
            codebase_survey TEXT,
            best_of_n_designs TEXT,
            selected_design_index INTEGER,
            selection_justification TEXT,
            confidence INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'active',
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_planning_results_task ON planning_results(task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS planning_results")
    conn.execute("DROP INDEX IF EXISTS idx_planning_results_task")
    conn.commit()
