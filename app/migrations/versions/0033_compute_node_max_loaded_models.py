"""
Migration 0033 — add max_loaded_models to compute_nodes.

Separates two distinct constraints on a compute node:
  - max_parallel_sessions: total concurrent request slots across all loaded models
    (hardware throughput ceiling, e.g. 9 for a node running one 9-batch model)
  - max_loaded_models: how many distinct model weights can reside in VRAM simultaneously
    (memory ceiling, e.g. 1 for a single GPU that can only hold one model at a time)

The scheduler previously used max_parallel_sessions for both purposes, which caused it
to block all further sessions the moment a single request was in-flight on a node with
max_parallel_sessions=1.
"""

description = "add max_loaded_models column to compute_nodes"


def up(conn):
    conn.execute(
        "ALTER TABLE compute_nodes ADD COLUMN max_loaded_models INTEGER NOT NULL DEFAULT 1"
    )
    conn.commit()
    print("[0033] Added max_loaded_models to compute_nodes.")


def down(conn):
    # SQLite does not support DROP COLUMN before 3.35; recreate the table.
    conn.execute("""
        CREATE TABLE compute_nodes_backup AS
        SELECT id, name, description, max_parallel_sessions
        FROM compute_nodes
    """)
    conn.execute("DROP TABLE compute_nodes")
    conn.execute("ALTER TABLE compute_nodes_backup RENAME TO compute_nodes")
    conn.commit()
    print("[0033] Removed max_loaded_models from compute_nodes.")
