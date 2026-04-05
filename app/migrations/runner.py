"""
Migration runner for TheMaestro Kanban DB.

Usage:
    python app/migrations/runner.py migrate   — apply all pending migrations
    python app/migrations/runner.py status    — show applied vs pending
    python app/migrations/runner.py rollback  — revert the last applied migration
    python app/migrations/runner.py reset     — drop all, re-migrate, seed sample data
"""

import sqlite3
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
MIGRATIONS_DIR = Path(__file__).parent / "versions"
# MAESTRO_TEST_DB lets conftest.py redirect the runner to the test database.
# Production code never sets this variable, so it always falls back to
# the canonical data/kanban.db path.
_env_db = os.environ.get("MAESTRO_TEST_DB")
DB_PATH = Path(_env_db) if _env_db else Path(__file__).parent.parent.parent / "data" / "kanban.db"


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def get_connection() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def ensure_migrations_table(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at   DATETIME NOT NULL
        )
    """)
    conn.commit()


# ---------------------------------------------------------------------------
# Migration discovery
# ---------------------------------------------------------------------------

def get_applied(conn: sqlite3.Connection) -> list:
    """Return list of migration IDs (NNNN strings) that have been applied."""
    ensure_migrations_table(conn)
    rows = conn.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
    ).fetchall()
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

def migrate(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations in order."""
    ensure_migrations_table(conn)
    applied = set(get_applied(conn))
    all_migrations = get_all_migrations()

    pending = [(mid, mod) for mid, mod in all_migrations if mid not in applied]

    if not pending:
        print("No pending migrations — database is up to date.")
        return

    for migration_id, mod in pending:
        desc = getattr(mod, "description", migration_id)
        print(f"  Applying {migration_id}: {desc} ...", end=" ")
        mod.up(conn)
        conn.execute(
            "INSERT INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
            (migration_id, datetime.now(timezone.utc).isoformat()),
        )
        conn.commit()
        print("done")

    print(f"Migrations complete. Applied {len(pending)} migration(s).")


def rollback(conn: sqlite3.Connection) -> None:
    """Revert the most recently applied migration."""
    ensure_migrations_table(conn)
    applied = get_applied(conn)

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
    print(f"  Rolling back {last_id}: {desc} ...", end=" ")
    mod.down(conn)
    conn.execute(
        "DELETE FROM schema_migrations WHERE migration_id = ?", (last_id,)
    )
    conn.commit()
    print("done")


def status(conn: sqlite3.Connection) -> None:
    """Print the status of every known migration."""
    ensure_migrations_table(conn)
    applied_rows = conn.execute(
        "SELECT migration_id, applied_at FROM schema_migrations ORDER BY migration_id"
    ).fetchall()
    applied_map = {row["migration_id"]: row["applied_at"] for row in applied_rows}
    all_migrations = get_all_migrations()

    print(f"{'ID':<8}  {'Status':<10}  {'Applied At':<26}  Description")
    print("-" * 72)
    for migration_id, mod in all_migrations:
        desc = getattr(mod, "description", "")
        if migration_id in applied_map:
            state = "applied"
            applied_at = applied_map[migration_id]
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


def reset(conn: sqlite3.Connection) -> None:
    """Drop all tables, re-apply all migrations, then seed sample data."""
    print("WARNING: This will destroy all data. Proceeding...")

    # Drop every user table (skip sqlite_* internals)
    tables = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
    ).fetchall()
    for row in tables:
        print(f"  Dropping table: {row['name']}")
        conn.execute(f"DROP TABLE IF EXISTS [{row['name']}]")
    conn.commit()

    # Re-create migrations table and apply all migrations
    ensure_migrations_table(conn)
    migrate(conn)

    # Seed sample data
    print("Seeding sample data...")
    _seed_via_runner(conn)
    print("Reset complete.")


# ---------------------------------------------------------------------------
# Raw seed (mirrors seed_sample_tasks() in database.py)
# ---------------------------------------------------------------------------

def _seed_via_runner(conn: sqlite3.Connection) -> None:
    """
    Insert the 10 canonical sample tasks using raw sqlite3.
    Kept in sync with database.seed_sample_tasks_raw().
    """
    # Import from database.py without triggering SQLAlchemy engine setup
    # by calling the standalone raw function we added there.
    # Resolve project root so we can import app.database regardless of cwd.
    project_root = str(Path(__file__).parent.parent.parent)
    if project_root not in sys.path:
        sys.path.insert(0, project_root)

    try:
        from app.database import seed_sample_tasks_raw
        seed_sample_tasks_raw(conn)
    except Exception as exc:
        print(f"  WARNING: could not import seed from app.database ({exc}).")
        print("  Falling back to inline seed data.")
        _inline_seed(conn)


def _inline_seed(conn: sqlite3.Connection) -> None:
    """Fallback seed — identical data to seed_sample_tasks_raw."""
    import json
    now = datetime.now(timezone.utc).isoformat()
    history = json.dumps([{"status": "created", "timestamp": now}])

    tasks = [
        ("arch-1",      "Project Stack",                          "architecture", "Core technology stack for TheMaestro",                          "user", json.dumps(["core", "infrastructure"]), json.dumps({"frontend": "HTML/CSS/JS", "backend": "FastAPI + Uvicorn", "database": "SQLite (development)", "style": "Bootstrap CSS"}), history, 0),
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
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks
            (id, title, type, description, owner, tags, content, history, position,
             created_at, updated_at, prerequisites)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*t, now, now, json.dumps([])),
        )
        print(f"  Seeded task: {t[0]} - {t[1]}")
    conn.commit()


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    commands = ("migrate", "status", "rollback", "reset")
    if len(sys.argv) < 2 or sys.argv[1] not in commands:
        print(f"Usage: python runner.py <{'|'.join(commands)}>")
        sys.exit(1)

    cmd = sys.argv[1]
    conn = get_connection()
    try:
        if cmd == "migrate":
            migrate(conn)
        elif cmd == "status":
            status(conn)
        elif cmd == "rollback":
            rollback(conn)
        elif cmd == "reset":
            reset(conn)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
