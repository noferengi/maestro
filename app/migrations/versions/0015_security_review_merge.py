"""
Migration 0015 — Security review results, full review results, and merge records tables.

Creates three tables supporting the review-and-merge pipeline: security-focused
review findings, full functional review findings, and merge commit records
linking back to their review evidence.
"""

description = "Security review results, full review results, and merge records tables"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS security_review_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            reviewer_type TEXT NOT NULL,
            owasp_findings TEXT,
            secrets_detected TEXT,
            dependency_vulnerabilities TEXT,
            data_flow_map TEXT,
            compliance_findings TEXT,
            optimization_regressions TEXT,
            verdict TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            justification TEXT,
            critical_count INTEGER DEFAULT 0,
            high_count INTEGER DEFAULT 0,
            raw_response TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            model TEXT,
            llm_id INTEGER,
            budget_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_security_review_task ON security_review_results(task_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS full_review_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            reviewer_type TEXT NOT NULL,
            test_results TEXT,
            quality_findings TEXT,
            requirements_mapping TEXT,
            integration_checks TEXT,
            verdict TEXT NOT NULL,
            confidence INTEGER NOT NULL,
            justification TEXT,
            raw_response TEXT,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            model TEXT,
            llm_id INTEGER,
            budget_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_full_review_task ON full_review_results(task_id)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS merge_records (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            branch_name TEXT NOT NULL,
            merge_commit_sha TEXT,
            status TEXT NOT NULL,
            test_output TEXT,
            error_detail TEXT,
            security_review_ids TEXT,
            full_review_ids TEXT,
            total_pipeline_tokens INTEGER DEFAULT 0,
            llm_id INTEGER,
            budget_id INTEGER,
            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_merge_records_task ON merge_records(task_id)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS merge_records")
    conn.execute("DROP TABLE IF EXISTS full_review_results")
    conn.execute("DROP TABLE IF EXISTS security_review_results")
    conn.execute("DROP INDEX IF EXISTS idx_merge_records_task")
    conn.execute("DROP INDEX IF EXISTS idx_full_review_task")
    conn.execute("DROP INDEX IF EXISTS idx_security_review_task")
    conn.commit()
