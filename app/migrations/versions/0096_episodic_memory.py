description = "Add episodic_memory table with HNSW vector index (pgvector 0.8+)"


def up(conn):
    # Enable the vector extension — requires pgvector to be installed on the PG host.
    # On Ubuntu: sudo apt-get install postgresql-18-pgvector
    conn.execute("CREATE EXTENSION IF NOT EXISTS vector")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS episodic_memory (
            id            SERIAL PRIMARY KEY,
            project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            task_id       TEXT    NULL     REFERENCES tasks(id)    ON DELETE SET NULL,
            episode_type  TEXT    NOT NULL
                              CHECK (episode_type IN ('failure', 'session_summary', 'document')),
            content       TEXT    NOT NULL,
            embedding     vector(1536),
            metadata      JSONB   NOT NULL DEFAULT '{}',
            created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
            expires_at    TIMESTAMPTZ NOT NULL,
            last_accessed TIMESTAMPTZ NULL
        )
    """)

    # HNSW index: works on empty tables (unlike IVFFlat which needs ~3900 rows).
    conn.execute("""
        CREATE INDEX IF NOT EXISTS episodic_memory_embedding_idx
        ON episodic_memory USING hnsw (embedding vector_cosine_ops)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS episodic_memory_project_expires_idx
        ON episodic_memory (project_id, expires_at)
    """)

    conn.execute("""
        CREATE INDEX IF NOT EXISTS episodic_memory_task_idx
        ON episodic_memory (task_id)
    """)


def down(conn):
    conn.execute("DROP TABLE IF EXISTS episodic_memory")
    # Do not drop the vector extension — other features may depend on it.
