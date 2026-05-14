"""
Migration runner for TheMaestro Kanban DB.
Supports both SQLite and PostgreSQL based on maestro.ini configuration.

Usage:
    python app/migrations/runner.py migrate   — apply all pending migrations
    python app/migrations/runner.py status    — show applied vs pending
    python app/migrations/runner.py rollback  — revert the last applied migration
    python app/migrations/runner.py reset     — drop all, re-migrate, seed sample data
"""

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import create_engine, text

# Add project root to path so we can import app.*
PROJECT_ROOT = Path(__file__).parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# Import the database URL from the agent config (respects maestro.ini and env)
from app.agent.config import ADMIN_DATABASE_URL

MIGRATIONS_DIR = Path(__file__).parent / "versions"

# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

class RowWrapper:
    """
    Wraps a SQLAlchemy Row to support both positional (index)
    and name-based (string) access, since SQLAlchemy 2.0 Row 
    objects separate these behaviors.
    """
    def __init__(self, row):
        # row can be a SQLAlchemy Row or another RowWrapper
        if hasattr(row, "_row"):
            self._row = row._row
        else:
            self._row = row

    def __getitem__(self, key):
        if isinstance(key, str):
            return self._row._mapping[key]
        return self._row[key]

    def __iter__(self):
        return iter(self._row)

    def __len__(self):
        return len(self._row)

    def keys(self):
        return self._row._mapping.keys()

    def __repr__(self):
        return repr(self._row)


class ConnectionWrapper:
    """
    Wrapper to provide a consistent .execute() interface for both 
    SQLAlchemy connections and raw DB-API connections used in migrations.
    """
    def __init__(self, conn, is_postgres=False):
        self.conn = conn
        self.is_postgres = is_postgres
        self._last_res = None

    def cursor(self):
        """Return self to act as a cursor (provides execute/fetchone/fetchall)."""
        return self

    def execute(self, sql, params=None):
        # Handle positional parameters (?) by converting to named ones for sqlalchemy.text()
        if isinstance(params, (list, tuple)):
            new_params = {}
            parts = sql.split('?')
            new_sql = parts[0]
            for i in range(1, len(parts)):
                placeholder = f"p{i-1}"
                new_sql += f":{placeholder}" + parts[i]
                new_params[placeholder] = params[i-1]
            sql = new_sql
            params = new_params

        # Handle SQLite-specific syntax if we are on Postgres
        if self.is_postgres:
            import re as _re

            # [table] → "table"  (bracket quoting)
            sql = sql.replace("[", "\"").replace("]", "\"")

            # datetime('now') MUST be replaced before the DATETIME type rename
            # because the type regex is case-insensitive and would match the
            # function name first, producing the invalid TIMESTAMP('now').
            # Only replace the 0-arg form; datetime('now', '-N unit') is a
            # different beast handled elsewhere via _date_ago().
            sql = _re.sub(r"datetime\s*\(\s*'now'\s*\)", 'CURRENT_TIMESTAMP', sql)

            # Type names
            sql = _re.sub(r'\bDATETIME\b',  'TIMESTAMP', sql, flags=_re.IGNORECASE)
            sql = _re.sub(r'\bBLOB\b',      'BYTEA',     sql, flags=_re.IGNORECASE)

            # Auto-increment: INTEGER PRIMARY KEY AUTOINCREMENT → SERIAL PRIMARY KEY
            sql = _re.sub(
                r'\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b',
                'SERIAL PRIMARY KEY',
                sql, flags=_re.IGNORECASE,
            )
            # Remove any stray AUTOINCREMENT that wasn't caught above
            sql = _re.sub(r'\s+AUTOINCREMENT\b', '', sql, flags=_re.IGNORECASE)

            # PRAGMA table_info(X) → pg_attribute query that returns the same
            # column shape: name, type, notnull, dflt_value, pk.
            # Used by migration guards (_has_column, _is_integer_pk).
            _pm = _re.match(
                r'^\s*PRAGMA\s+table_info\s*\(\s*(\w+)\s*\)\s*$',
                sql.strip(), _re.IGNORECASE,
            )
            if _pm:
                _t = _pm.group(1)
                sql = f"""
                    SELECT
                        a.attnum - 1 AS cid,
                        a.attname    AS name,
                        t.typname    AS type,
                        CASE WHEN a.attnotnull THEN 1 ELSE 0 END AS notnull,
                        ''           AS dflt_value,
                        CASE WHEN EXISTS (
                            SELECT 1 FROM pg_constraint c2
                            WHERE c2.conrelid = a.attrelid
                              AND c2.contype = 'p'
                              AND a.attnum = ANY(c2.conkey)
                        ) THEN 1 ELSE 0 END AS pk
                    FROM pg_attribute a
                    JOIN pg_class c ON c.oid = a.attrelid
                    JOIN pg_type  t ON t.oid = a.atttypid
                    WHERE c.relname = '{_t}'
                      AND a.attnum > 0
                      AND NOT a.attisdropped
                    ORDER BY a.attnum
                """

            # CREATE VIEW IF NOT EXISTS X → CREATE OR REPLACE VIEW X
            # (PostgreSQL does not support IF NOT EXISTS for views)
            sql = _re.sub(
                r'\bCREATE\s+VIEW\s+IF\s+NOT\s+EXISTS\b',
                'CREATE OR REPLACE VIEW',
                sql, flags=_re.IGNORECASE,
            )

            # rowid is SQLite's internal row identifier.  Migrations use it only to
            # populate a new id column before table rebuilds (0044), always on tables
            # that are empty during Phase 1 schema build.  Translate to 0 so the SQL
            # is valid; the UPDATE is a no-op on empty tables.
            sql = _re.sub(r'\browid\b', '0', sql, flags=_re.IGNORECASE)

            # json_extract(col, '$.key') → (col::json)->>'key'
            # (SQLite JSON function; PostgreSQL uses the -> / ->> operators)
            sql = _re.sub(
                r"\bjson_extract\s*\(\s*([^,]+?)\s*,\s*'\$\.(\w+)'\s*\)",
                r"(\1::json)->>'\2'",
                sql, flags=_re.IGNORECASE,
            )

            # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
            if "INSERT OR IGNORE" in sql:
                sql = sql.replace("INSERT OR IGNORE", "INSERT")
                sql += " ON CONFLICT DO NOTHING"
            # INSERT OR REPLACE → INSERT ... ON CONFLICT DO UPDATE (upsert)
            elif "INSERT OR REPLACE" in sql:
                if "tasks" in sql:
                    sql = sql.replace("INSERT OR REPLACE", "INSERT")
                    sql += (
                        " ON CONFLICT (id) DO UPDATE SET"
                        " title=EXCLUDED.title, type=EXCLUDED.type,"
                        " description=EXCLUDED.description, owner=EXCLUDED.owner,"
                        " tags=EXCLUDED.tags, content=EXCLUDED.content,"
                        " history=EXCLUDED.history, position=EXCLUDED.position,"
                        " updated_at=EXCLUDED.updated_at, prerequisites=EXCLUDED.prerequisites,"
                        " project=EXCLUDED.project, llm_id=EXCLUDED.llm_id,"
                        " budget_id=EXCLUDED.budget_id"
                    )
                else:
                    # Generic fallback: treat as insert-if-not-exists
                    sql = sql.replace("INSERT OR REPLACE", "INSERT")
                    sql += " ON CONFLICT DO NOTHING"

        # Use SQLAlchemy text() for execution
        self._last_res = self.conn.execute(text(sql), params or {})
        return self

    def executescript(self, sql):
        """Execute multiple SQL statements separated by semicolons."""
        statements = [s.strip() for s in sql.split(';') if s.strip()]
        for stmt in statements:
            self.execute(stmt)

    def commit(self):
        # SQLAlchemy connection within a transaction (target_engine.begin()) 
        # commits automatically on block exit.
        pass

    def fetchone(self):
        """Return one row from the last result."""
        if self._last_res:
            row = self._last_res.fetchone()
            return RowWrapper(row) if row else None
        return None

    def fetchall(self, res=None):
        """Return all rows from the last result or provided result."""
        # res might be self (if execute returned self) or a raw Result
        r = self._last_res
        if res and res is not self:
            if hasattr(res, "all"):
                return [RowWrapper(row) for row in res.all()]
            return res
        
        if r:
            return [RowWrapper(row) for row in r.all()]
        return []

    @property
    def rowcount(self):
        """Return the number of rows affected by the last execute()."""
        if self._last_res:
            return self._last_res.rowcount
        return 0

def get_connection():
    """Create a database engine and return engine and is_postgres flag."""
    is_postgres = ADMIN_DATABASE_URL.startswith("postgresql")
    engine = create_engine(ADMIN_DATABASE_URL)
    return engine, is_postgres


def ensure_migrations_table(conn_wrapper) -> None:
    conn_wrapper.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at   TIMESTAMP NOT NULL
        )
    """)


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------

def get_applied(conn_wrapper) -> list:
    """Return list of migration IDs (NNNN strings) that have been applied."""
    ensure_migrations_table(conn_wrapper)
    res = conn_wrapper.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
    )
    rows = conn_wrapper.fetchall(res)
    return [row["migration_id"] for row in rows]


def _load_module(path: Path):
    """Load a Python module from a file path."""
    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_all_migrations() -> list:
    """
    Return a sorted list of (migration_id, module) tuples.
    migration_id is the NNNN prefix extracted from the filename.
    """
    results = []
    if not MIGRATIONS_DIR.exists():
        return []
        
    for entry in sorted(MIGRATIONS_DIR.iterdir()):
        if entry.suffix != ".py" or entry.name.startswith("_"):
            continue
        # Filename format: NNNN_description.py
        migration_id = entry.name.split("_")[0]
        if not migration_id.isdigit():
            continue
        mod = _load_module(entry)
        results.append((migration_id, mod))
    # Sort by numeric value of the prefix to guarantee order
    results.sort(key=lambda t: int(t[0]))
    return results


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------

def migrate(conn_wrapper) -> None:
    """Apply all pending migrations in order."""
    ensure_migrations_table(conn_wrapper)
    applied = set(get_applied(conn_wrapper))
    all_migrations = get_all_migrations()

    pending = [(mid, mod) for mid, mod in all_migrations if mid not in applied]

    if not pending:
        print("No pending migrations — database is up to date.")
        return

    for migration_id, mod in pending:
        desc = getattr(mod, "description", migration_id)
        print(f"  Applying {migration_id}: {desc} ...", end=" ", flush=True)
        try:
            mod.up(conn_wrapper)
            conn_wrapper.execute(
                "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (:mid, :at)",
                {"mid": migration_id, "at": datetime.now(timezone.utc)}
            )
            print("done")
        except Exception as e:
            print(f"FAILED: {e}")
            raise

    print(f"Migrations complete. Applied {len(pending)} migration(s).")


def rollback(conn_wrapper) -> None:
    """Revert the most recently applied migration."""
    ensure_migrations_table(conn_wrapper)
    applied = get_applied(conn_wrapper)

    if not applied:
        print("Nothing to roll back — no migrations have been applied.")
        return

    last_id = applied[-1]
    all_migrations = dict(get_all_migrations())

    if last_id not in all_migrations:
        print(f"ERROR: Migration module for '{last_id}' not found in versions/.")
        sys.exit(1)

    mod = all_migrations[last_id]
    desc = getattr(mod, "description", last_id)
    print(f"  Rolling back {last_id}: {desc} ...", end=" ", flush=True)
    mod.down(conn_wrapper)
    conn_wrapper.execute(
        "DELETE FROM schema_migrations WHERE migration_id = :mid", 
        {"mid": last_id}
    )
    print("done")


def status(conn_wrapper) -> None:
    """Print the status of every known migration."""
    ensure_migrations_table(conn_wrapper)
    res = conn_wrapper.execute(
        "SELECT migration_id, applied_at FROM schema_migrations ORDER BY migration_id"
    )
    applied_rows = conn_wrapper.fetchall(res)
    applied_map = {row["migration_id"]: row["applied_at"] for row in applied_rows}
    all_migrations = get_all_migrations()

    print(f"{'ID':<8}  {'Status':<10}  {'Applied At':<26}  Description")
    print("-" * 80)
    for migration_id, mod in all_migrations:
        desc = getattr(mod, "description", "")
        if migration_id in applied_map:
            state = "applied"
            applied_at = str(applied_map[migration_id])
        else:
            state = "pending"
            applied_at = ""
        print(f"{migration_id:<8}  {state:<10}  {applied_at:<26}  {desc}")

    # Warn about orphaned applied entries (migration file deleted)
    known_ids = {mid for mid, _ in all_migrations}
    orphans = [mid for mid in applied_map if mid not in known_ids]
    if orphans:
        print()
        print("WARNING: The following applied migrations have no matching file:")
        for oid in orphans:
            print(f"  {oid}")


def reset(engine, is_postgres) -> None:
    """Drop all tables, re-apply all migrations, then seed sample data."""
    print("WARNING: This will destroy all data. Proceeding...")

    with engine.begin() as conn:
        conn_wrapper = ConnectionWrapper(conn, is_postgres)
        
        if is_postgres:
            # Postgres: drop all tables in public schema
            res = conn.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"))
            tables = res.fetchall()
            for row in tables:
                print(f"  Dropping table: {row[0]}")
                conn.execute(text(f"DROP TABLE IF EXISTS \"{row[0]}\" CASCADE"))
        else:
            # SQLite: drop every user table
            res = conn.execute(text("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"))
            tables = res.fetchall()
            for row in tables:
                print(f"  Dropping table: {row[0]}")
                conn.execute(text(f"DROP TABLE IF EXISTS [{row[0]}]"))

        # Re-create migrations table and apply all migrations
        ensure_migrations_table(conn_wrapper)
        migrate(conn_wrapper)

        # Seed sample data
        print("Seeding sample data...")
        _seed_via_runner(conn_wrapper)
        print("Reset complete.")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------

def _seed_via_runner(conn_wrapper) -> None:
    """Insert canonical sample tasks using the provided connection wrapper."""
    try:
        from app.database import seed_sample_tasks_raw
        # seed_sample_tasks_raw expects a connection with .execute()
        seed_sample_tasks_raw(conn_wrapper)
    except Exception as exc:
        print(f"  WARNING: could not import seed from app.database ({exc}).")
        print("  Falling back to inline seed data.")
        _inline_seed(conn_wrapper)


def _inline_seed(conn_wrapper) -> None:
    """Fallback seed — identical data to seed_sample_tasks_raw."""
    import json
    now = datetime.now(timezone.utc).isoformat()
    history = json.dumps([{"status": "created", "timestamp": now}])

    tasks = [
        ("arch-1",      "Project Stack",                          "architecture", "Core technology stack for TheMaestro",                          "user", json.dumps(["core", "infrastructure"]), json.dumps({"frontend": "HTML/CSS/JS", "backend": "FastAPI + Uvicorn", "database": "PostgreSQL", "style": "Bootstrap CSS"}), history, 0),
        ("arch-2",      "Code Structure",                         "architecture", "Organizational structure of the codebase",                       "user", json.dumps(["core", "structure"]),        json.dumps({"dags": "dags.py", "config": "config.py", "repl": "repl.py", "tests": "test_*.py"}), history, 1),
        ("planning-1",  "Setup FastAPI development environment",  "planning",     "Configure Python virtual environment and install dependencies",   "user", json.dumps(["backend", "setup"]),         None, history, 0),
        ("planning-2",  "Create Kanban board UI mockup",          "planning",     "Design wireframes for the Kanban board interface",                "user", json.dumps(["frontend", "design"]),       None, history, 1),
        ("planning-3",  "Implement drag-and-drop",                "planning",     "Add drag-and-drop functionality for task reordering",             "user", json.dumps(["feature", "frontend"]),      None, history, 2),
        ("dev-1",       "Configure venv and install dependencies","indev",        "Set up Python 3.13 virtual environment",                          "user", json.dumps(["setup", "backend"]),         None, history, 0),
        ("dev-2",       "Create app structure and main.py",       "indev",        "Set up FastAPI application with main entry point",                "user", json.dumps(["structure", "backend"]),     None, history, 1),
        ("review-1",    "Review requirements.txt",                "conceptual_review", "Verify all dependencies are properly listed",               "user", json.dumps(["qa", "backend"]),             None, history, 0),
        ("completed-1", "Initialize Git repository",              "completed",    "Create .gitignore and initial commit",                            "user", json.dumps(["setup", "devops"]),           None, history, 0),
        ("completed-2", "Create database schema",                 "completed",    "Define SQLAlchemy models for tasks",                              "user", json.dumps(["database", "backend"]),       None, history, 1),
    ]

    for t in tasks:
        conn_wrapper.execute(
            """
            INSERT INTO tasks
            (id, title, type, description, owner, tags, content, history, position,
             created_at, updated_at, prerequisites)
            VALUES (:id, :title, :type, :desc, :owner, :tags, :content, :history, :pos, :created, :updated, :prereqs)
            """,
            {
                "id": t[0], "title": t[1], "type": t[2], "desc": t[3], "owner": t[4], 
                "tags": t[5], "content": t[6], "history": t[7], "pos": t[8], 
                "created": now, "updated": now, "prereqs": json.dumps([])
            }
        )
        print(f"  Seeded task: {t[0]} - {t[1]}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    commands = ("migrate", "status", "rollback", "reset")
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print("TheMaestro Database Migration Runner")
        print("-" * 36)
        print("Usage: python app/migrations/runner.py <command>")
        print("\nCommands:")
        print("  status    - Show applied vs pending migrations (Recommended first step)")
        print("  migrate   - Apply all pending migrations to the database")
        print("  rollback  - Revert the last applied migration")
        print("  reset     - DESTROY all data and re-apply all migrations (Dev only)")
        sys.exit(1)

    cmd = sys.argv[1]
    engine, is_postgres = get_connection()
    
    if cmd == "reset":
        reset(engine, is_postgres)
        return

    with engine.begin() as conn:
        conn_wrapper = ConnectionWrapper(conn, is_postgres)
        if cmd == "migrate":
            migrate(conn_wrapper)
        elif cmd == "status":
            status(conn_wrapper)
        elif cmd == "rollback":
            rollback(conn_wrapper)

if __name__ == "__main__":
    main()
