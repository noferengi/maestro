"""
Migration Test Framework for TheMaestro Kanban DB.

Provides functions to load and execute individual migrations in isolation,
enabling comprehensive testing of migration scripts without affecting
the production database.

Usage:
    from app.migrations.test_framework import (
        load_migration,
        create_fresh_db,
        execute_migration,
        get_migration_status,
        test_migration_isolation,
    )

    # Test a single migration in isolation
    db_path = "test_data/test.db"
    create_fresh_db(db_path)
    migration = load_migration("app/migrations/versions/0001_initial_schema.py")
    execute_migration(db_path, migration, "up")
    status = get_migration_status(db_path)
"""

import sqlite3
import importlib.util
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


# ---------------------------------------------------------------------------
# Migration loading
# ---------------------------------------------------------------------------

def load_migration(path: str | Path) -> Any:
    """
    Load a migration module from a file path.

    Args:
        path: Path to the migration file (e.g., "app/migrations/versions/0001_initial_schema.py")

    Returns:
        Loaded migration module with up() and down() functions

    Raises:
        FileNotFoundError: If the migration file doesn't exist
        ImportError: If the migration file cannot be loaded as a module
    """
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Migration file not found: {path}")

    spec = importlib.util.spec_from_file_location(path.stem, str(path))
    if spec is None:
        raise ImportError(f"Could not create module spec for {path}")

    mod = importlib.util.module_from_spec(spec)
    if spec.loader is None:
        raise ImportError(f"Could not get loader for {path}")

    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Database creation
# ---------------------------------------------------------------------------

def create_fresh_db(db_path: str | Path) -> sqlite3.Connection:
    """
    Create a fresh SQLite database with the schema_migrations table.

    Args:
        db_path: Path to the database file

    Returns:
        Connection to the newly created database

    Raises:
        OSError: If the database cannot be created
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)

    # Remove existing file if present
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create the migrations tracking table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at   DATETIME NOT NULL
        )
    """)
    conn.commit()

    return conn


def create_temp_db() -> tuple[Path, sqlite3.Connection]:
    """
    Create a temporary database file and return its path and connection.

    Returns:
        Tuple of (Path to temp db, sqlite3.Connection)

    Note:
        The caller is responsible for closing the connection and deleting
        the temp file when done.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)

    conn = create_fresh_db(path)
    return Path(path), conn


# ---------------------------------------------------------------------------
# Migration execution
# ---------------------------------------------------------------------------

def execute_migration(conn: sqlite3.Connection, migration: Any, direction: str = "up", migration_id: str | None = None) -> None:
    """
    Execute the up() or down() function of a loaded migration.

    Args:
        conn: SQLite database connection
        migration: Loaded migration module
        direction: "up" to run up(), "down" to run down()
        migration_id: NNNN prefix to record in schema_migrations (auto-derived from
                      migration.__name__ if not provided)

    Raises:
        ValueError: If direction is not "up" or "down"
        AttributeError: If the migration module doesn't have the requested function
    """
    if migration_id is None and hasattr(migration, "__name__"):
        stem = migration.__name__
        migration_id = stem.split("_")[0] if "_" in stem else stem

    if direction == "up":
        if not hasattr(migration, "up"):
            raise AttributeError(f"Migration module missing 'up' function: {migration}")
        migration.up(conn)
        if migration_id:
            conn.execute(
                "INSERT OR REPLACE INTO schema_migrations (migration_id, applied_at) VALUES (?, ?)",
                (migration_id, datetime.now(timezone.utc).isoformat())
            )
            conn.commit()
    elif direction == "down":
        if not hasattr(migration, "down"):
            raise AttributeError(f"Migration module missing 'down' function: {migration}")
        migration.down(conn)
        if migration_id:
            conn.execute("DELETE FROM schema_migrations WHERE migration_id = ?", (migration_id,))
            conn.commit()
    else:
        raise ValueError(f"Invalid direction: {direction}. Must be 'up' or 'down'")


def execute_migration_path(db_path: str | Path, migration_path: str | Path, direction: str = "up") -> None:
    """
    Load and execute a migration from a file path.

    Args:
        db_path: Path to the database file
        migration_path: Path to the migration file
        direction: "up" to run up(), "down" to run down()

    Raises:
        FileNotFoundError: If either file doesn't exist
        ValueError: If direction is invalid
    """
    db_path = Path(db_path)
    migration_path = Path(migration_path)

    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    if not migration_path.exists():
        raise FileNotFoundError(f"Migration file not found: {migration_path}")

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    try:
        migration = load_migration(migration_path)
        execute_migration(conn, migration, direction)
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration status
# ---------------------------------------------------------------------------

def get_migration_status(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """
    Query the status of applied migrations.

    Args:
        conn: SQLite database connection

    Returns:
        List of dicts with migration_id and applied_at for each applied migration
    """
    cursor = conn.execute("SELECT migration_id, applied_at FROM schema_migrations ORDER BY migration_id")
    rows = cursor.fetchall()
    return [
        {
            "migration_id": row["migration_id"],
            "applied_at": row["applied_at"],
        }
        for row in rows
    ]


def get_applied_migrations(conn: sqlite3.Connection) -> list[str]:
    """
    Get list of migration IDs that have been applied.

    Args:
        conn: SQLite database connection

    Returns:
        List of migration IDs (NNNN strings) that have been applied
    """
    cursor = conn.execute(
        "SELECT migration_id FROM schema_migrations ORDER BY migration_id"
    )
    rows = cursor.fetchall()
    return [row["migration_id"] for row in rows]


def get_pending_migrations(conn: sqlite3.Connection, all_migrations: list[tuple[str, Any]]) -> list[tuple[str, Any]]:
    """
    Get list of migrations that have not been applied yet.

    Args:
        conn: SQLite database connection
        all_migrations: List of (migration_id, module) tuples for all known migrations

    Returns:
        List of (migration_id, module) tuples for pending migrations
    """
    applied = set(get_applied_migrations(conn))
    return [(mid, mod) for mid, mod in all_migrations if mid not in applied]


# ---------------------------------------------------------------------------
# Isolation testing
# ---------------------------------------------------------------------------

def test_migration_isolation(db_path: str | Path, migration_id: str, migration_path: str | Path) -> bool:
    """
    Test a single migration in isolation.

    This function:
    1. Creates a fresh database
    2. Applies the specified migration
    3. Verifies the migration was applied
    4. Rolls back the migration
    5. Verifies the rollback was successful

    Args:
        db_path: Path to the database file
        migration_id: Expected migration ID (NNNN prefix)
        migration_path: Path to the migration file

    Returns:
        True if the migration and rollback both succeeded

    Raises:
        AssertionError: If the migration or rollback fails
    """
    db_path = Path(db_path)
    migration_path = Path(migration_path)

    # Create fresh database
    conn = create_fresh_db(db_path)

    try:
        # Load and execute migration
        migration = load_migration(migration_path)

        # Verify migration ID matches
        expected_id = migration_path.stem.split("_")[0]
        if expected_id != migration_id:
            raise AssertionError(
                f"Migration ID mismatch: expected {migration_id}, got {expected_id}"
            )

        # Apply migration
        execute_migration(conn, migration, "up")

        # Verify migration was applied
        status = get_migration_status(conn)
        applied_ids = [s["migration_id"] for s in status]
        if migration_id not in applied_ids:
            raise AssertionError(f"Migration {migration_id} was not applied")

        # Rollback
        execute_migration(conn, migration, "down")

        # Verify rollback was successful
        status = get_migration_status(conn)
        applied_ids = [s["migration_id"] for s in status]
        if migration_id in applied_ids:
            raise AssertionError(f"Migration {migration_id} was not rolled back")

        return True

    finally:
        conn.close()


def test_migration_creates_expected_tables(
    db_path: str | Path,
    migration_path: str | Path,
    expected_tables: list[str]
) -> bool:
    """
    Test that a migration creates the expected tables.

    Args:
        db_path: Path to the database file
        migration_path: Path to the migration file
        expected_tables: List of table names that should be created

    Returns:
        True if all expected tables were created

    Raises:
        AssertionError: If any expected table is missing
    """
    db_path = Path(db_path)
    migration_path = Path(migration_path)

    # Create fresh database
    conn = create_fresh_db(db_path)

    try:
        # Load and execute migration
        migration = load_migration(migration_path)
        execute_migration(conn, migration, "up")

        # Check for expected tables
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        tables = {row["name"] for row in cursor.fetchall()}

        for table in expected_tables:
            if table not in tables:
                raise AssertionError(f"Expected table '{table}' was not created")

        return True

    finally:
        conn.close()


def test_migration_rollback_safe(
    db_path: str | Path,
    migration_path: str | Path,
    before_tables: list[str],
    after_tables: list[str]
) -> bool:
    """
    Test that a migration rollback is safe and restores the database to a known state.

    Args:
        db_path: Path to the database file
        migration_path: Path to the migration file
        before_tables: Table names that should exist before the migration
        after_tables: Table names that should exist after the rollback

    Returns:
        True if the migration and rollback both succeeded and restored the expected state

    Raises:
        AssertionError: If the database state is incorrect after migration or rollback
    """
    db_path = Path(db_path)
    migration_path = Path(migration_path)

    # Create fresh database
    conn = create_fresh_db(db_path)

    try:
        # Load and execute migration
        migration = load_migration(migration_path)
        execute_migration(conn, migration, "up")

        # Check state after migration
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        after_migration = {row["name"] for row in cursor.fetchall()}

        # After migration should have all before tables plus any new tables
        for table in before_tables:
            if table not in after_migration:
                raise AssertionError(f"Expected table '{table}' missing after migration")

        # Rollback
        execute_migration(conn, migration, "down")

        # Check state after rollback
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
        after_rollback = {row["name"] for row in cursor.fetchall()}

        # After rollback should have only the before tables
        for table in after_tables:
            if table not in after_rollback:
                raise AssertionError(f"Expected table '{table}' missing after rollback")

        for table in before_tables:
            if table not in after_rollback:
                raise AssertionError(f"Expected table '{table}' missing after rollback")

        return True

    finally:
        conn.close()


# Prevent pytest from collecting these helper functions as test cases
# when they are imported into a test module by name.
test_migration_isolation.__test__ = False
test_migration_creates_expected_tables.__test__ = False
test_migration_rollback_safe.__test__ = False
