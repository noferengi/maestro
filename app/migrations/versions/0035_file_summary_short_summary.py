"""
Migration 0035 — add short_summary column to file_summaries.

The file_summary_agent now produces two outputs per file:
  - summary      — comprehensive multi-paragraph description (existing column)
  - short_summary — exactly 2 sentences, used in directory listings and
                    agent initial context snapshots

Existing rows get NULL for short_summary; the listing code falls back to
summary when short_summary is absent, so no data repair is needed.
"""

description = "add short_summary column to file_summaries"


def up(conn):
    conn.execute("ALTER TABLE file_summaries ADD COLUMN short_summary TEXT")
    print("[0035] Added short_summary column to file_summaries.")
    conn.commit()


def down(conn):
    # SQLite does not support DROP COLUMN.  Re-create the table without the
    # column to make down() safe on a fresh DB; on a live DB the column just
    # stays (harmless empty column).
    print("[0035] down: SQLite cannot drop columns — short_summary column left in place.")
    conn.commit()
