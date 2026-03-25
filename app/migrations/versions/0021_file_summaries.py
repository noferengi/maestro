"""
Migration 0021 — file_summaries table.

Stores LLM-generated natural-language file summaries keyed on
SHA1 hash + file size so the same content is never summarised twice,
even across sessions or if a file is renamed.

Schema:
    sha1_hash           — hex SHA1 of the file bytes
    file_size_bytes     — byte length of the file
    file_path           — last-known path (informational, not a lookup key)
    summary             — natural-language summary (LLM-generated)
    static_analysis_json — JSON from static_analysis.analyze_file() or NULL
    created_at          — when this cache entry was written

The unique index on (sha1_hash, file_size_bytes) is the cache key.
"""

description = "Add file_summaries table for DB-cached LLM file summaries"


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS file_summaries (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            sha1_hash            TEXT    NOT NULL,
            file_size_bytes      INTEGER NOT NULL,
            file_path            TEXT    NOT NULL,
            summary              TEXT    NOT NULL,
            static_analysis_json TEXT,
            created_at           DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)
    conn.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_file_summaries_sha1_size
            ON file_summaries (sha1_hash, file_size_bytes)
    """)
    conn.commit()


def down(conn):
    conn.execute("DROP INDEX IF EXISTS idx_file_summaries_sha1_size")
    conn.execute("DROP TABLE IF EXISTS file_summaries")
    conn.commit()
