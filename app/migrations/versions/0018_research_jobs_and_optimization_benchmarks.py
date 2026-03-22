"""
Migration 0018 — Add research_jobs and optimization_benchmarks tables.

research_jobs tracks all agent research requests — both inline (spawned by
MaestroLoop via NEEDS_RESEARCH signal) and queued (dispatched by the
scheduler as background work).

optimization_benchmarks stores before/after profiling metrics collected
by optimization sub-tasks as they flow through the pipeline.
"""

description = "Add research_jobs and optimization_benchmarks tables"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS research_jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            parent_job_id INTEGER REFERENCES research_jobs(id),
            question TEXT NOT NULL,
            context TEXT,
            status TEXT NOT NULL DEFAULT 'pending',
            priority REAL NOT NULL DEFAULT 0.0,
            depth INTEGER NOT NULL DEFAULT 0,
            verdict TEXT,
            findings TEXT,
            lives_used INTEGER DEFAULT 0,
            prompt_tokens INTEGER DEFAULT 0,
            completion_tokens INTEGER DEFAULT 0,
            llm_id INTEGER REFERENCES llms(id),
            budget_id INTEGER REFERENCES budgets(id),
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            completed_at DATETIME
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_jobs_status ON research_jobs(status)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_research_jobs_task ON research_jobs(task_id)"
    )

    conn.execute("""
        CREATE TABLE IF NOT EXISTS optimization_benchmarks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id),
            parent_task_id TEXT NOT NULL REFERENCES tasks(id),
            benchmark_type TEXT NOT NULL,
            metrics TEXT NOT NULL,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_opt_bench_parent ON optimization_benchmarks(parent_task_id)"
    )
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE IF EXISTS optimization_benchmarks")
    conn.execute("DROP TABLE IF EXISTS research_jobs")
    conn.commit()
