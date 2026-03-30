"""
Migration 0032 — compute_nodes table and compute_node_id on llms.

Adds a compute_nodes table to represent physical/virtual compute resources
that LLM endpoints run on.  Each LLM endpoint can optionally reference a
compute node via compute_node_id.  The scheduler enforces a per-node
max_parallel_sessions cap on top of the existing per-endpoint cap.
"""

description = "add compute_nodes table and compute_node_id FK on llms"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS compute_nodes (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            name                 TEXT    NOT NULL UNIQUE,
            description          TEXT,
            max_parallel_sessions INTEGER NOT NULL DEFAULT 1
        )
    """)
    conn.execute(
        "ALTER TABLE llms ADD COLUMN compute_node_id INTEGER REFERENCES compute_nodes(id)"
    )
    conn.commit()
    print("[0032] Created compute_nodes table and added compute_node_id to llms.")


def down(conn):
    # Remove compute_node_id from llms by recreating the table without it
    conn.execute("""
        CREATE TABLE llms_backup AS
        SELECT id, address, port, model, settings, parallel_sessions,
               max_context, notes,
               cost_per_million_prompt_tokens,
               cost_per_million_completion_tokens
        FROM llms
    """)
    conn.execute("DROP TABLE llms")
    conn.execute("ALTER TABLE llms_backup RENAME TO llms")
    conn.execute("DROP TABLE IF EXISTS compute_nodes")
    conn.commit()
    print("[0032] Removed compute_nodes table and compute_node_id from llms.")
