description = "Add tier column to job tables for priority scheduling"

_TABLES = [
    "file_summary_jobs",
    "arch_gen_jobs",
    "scope_survey_jobs",
    "research_jobs",
    "pip_resolution_jobs",
]


def up(conn):
    for table in _TABLES:
        if conn.is_postgres:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN IF NOT EXISTS tier INTEGER NOT NULL DEFAULT 2")
        else:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN tier INTEGER NOT NULL DEFAULT 2")


def down(conn):
    for table in _TABLES:
        if conn.is_postgres:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS tier")
        else:
            conn.execute(f"ALTER TABLE {table} DROP COLUMN tier")
