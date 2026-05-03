"""
Pytest tests for migration test framework.

Tests cover:
- Loading migrations from file paths
- Executing up() and down() functions
- Creating fresh databases
- Querying migration status
- Testing migration isolation
- Testing table creation
- Testing rollback safety
"""

import os
import sqlite3
import tempfile
from pathlib import Path

import pytest

# Add parent directory to path for imports
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.migrations.test_framework import (
    load_migration,
    create_fresh_db,
    execute_migration,
    get_migration_status,
    get_applied_migrations,
    get_pending_migrations,
    test_migration_isolation,
    test_migration_creates_expected_tables,
    test_migration_rollback_safe,
    create_temp_db,
)
from app.migrations.runner import get_all_migrations


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield Path(path)
    # Cleanup is handled by pytest's tmp_path fixture or manual deletion


@pytest.fixture
def fresh_db(temp_db):
    """Create a fresh database with schema_migrations table."""
    conn = create_fresh_db(temp_db)
    yield conn
    conn.close()


@pytest.fixture
def initial_migration():
    """Load the initial schema migration."""
    return load_migration("app/migrations/versions/0001_initial_schema.py")


@pytest.fixture
def prerequisites_migration():
    """Load the prerequisites migration."""
    return load_migration("app/migrations/versions/0002_add_prerequisites.py")


# ---------------------------------------------------------------------------
# Migration loading tests
# ---------------------------------------------------------------------------

class TestMigrationLoading:
    """Tests for load_migration function."""

    def test_load_valid_migration(self, initial_migration):
        """Test loading a valid migration file."""
        assert initial_migration is not None
        assert hasattr(initial_migration, "up")
        assert hasattr(initial_migration, "down")
        assert hasattr(initial_migration, "description")

    def test_load_migration_has_description(self, initial_migration):
        """Test that loaded migration has description attribute."""
        assert initial_migration.description == "Create initial tasks table"

    def test_load_migration_from_path(self):
        """Test loading migration from string path."""
        migration = load_migration("app/migrations/versions/0001_initial_schema.py")
        assert migration is not None

    def test_load_migration_from_path_object(self):
        """Test loading migration from Path object."""
        migration = load_migration(Path("app/migrations/versions/0001_initial_schema.py"))
        assert migration is not None

    def test_load_nonexistent_migration_raises_file_not_found(self):
        """Test that loading a nonexistent migration raises FileNotFoundError."""
        with pytest.raises(FileNotFoundError):
            load_migration("app/migrations/versions/nonexistent.py")


# ---------------------------------------------------------------------------
# Database creation tests
# ---------------------------------------------------------------------------

class TestDatabaseCreation:
    """Tests for database creation functions."""

    def test_create_fresh_db_creates_file(self, temp_db):
        """Test that create_fresh_db creates the database file."""
        conn = create_fresh_db(temp_db)
        assert temp_db.exists()
        conn.close()

    def test_create_fresh_db_creates_schema_migrations_table(self, temp_db):
        """Test that create_fresh_db creates the schema_migrations table."""
        conn = create_fresh_db(temp_db)
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        )
        assert cursor.fetchone() is not None
        conn.close()

    def test_create_fresh_db_removes_existing_file(self, temp_db):
        """Test that create_fresh_db removes existing database file."""
        # Create initial database
        conn1 = create_fresh_db(temp_db)
        conn1.execute("INSERT INTO schema_migrations VALUES ('0001', '2024-01-01')")
        conn1.commit()
        conn1.close()

        # Create fresh database - should remove existing
        conn2 = create_fresh_db(temp_db)
        rows = conn2.execute("SELECT * FROM schema_migrations").fetchall()
        assert len(rows) == 0
        conn2.close()

    def test_create_temp_db_returns_tuple(self):
        """Test that create_temp_db returns (Path, Connection) tuple."""
        db_path, conn = create_temp_db()
        assert isinstance(db_path, Path)
        assert conn is not None
        assert db_path.exists()
        conn.close()


# ---------------------------------------------------------------------------
# Migration execution tests
# ---------------------------------------------------------------------------

class TestMigrationExecution:
    """Tests for migration execution functions."""

    def test_execute_migration_up(self, fresh_db, initial_migration):
        """Test executing migration up."""
        execute_migration(fresh_db, initial_migration, "up")

        # Verify table was created
        cursor = fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        assert cursor.fetchone() is not None

    def test_execute_migration_down(self, fresh_db, initial_migration):
        """Test executing migration down."""
        # First apply the migration
        execute_migration(fresh_db, initial_migration, "up")

        # Then rollback
        execute_migration(fresh_db, initial_migration, "down")

        # Verify table was dropped
        cursor = fresh_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='tasks'"
        )
        assert cursor.fetchone() is None

    def test_execute_migration_invalid_direction_raises(self, fresh_db, initial_migration):
        """Test that invalid direction raises ValueError."""
        with pytest.raises(ValueError, match="Invalid direction"):
            execute_migration(fresh_db, initial_migration, "invalid")

    def test_execute_migration_missing_up_raises(self):
        """Test that migration without up() raises AttributeError."""
        # Create a mock module without up()
        import types
        mock_module = types.ModuleType("mock_migration")
        mock_module.description = "Mock migration"

        with pytest.raises(AttributeError, match="missing 'up' function"):
            execute_migration(fresh_db, mock_module, "up")

    def test_execute_migration_missing_down_raises(self):
        """Test that migration without down() raises AttributeError."""
        # Create a mock module without down()
        import types
        mock_module = types.ModuleType("mock_migration")
        mock_module.description = "Mock migration"

        with pytest.raises(AttributeError, match="missing 'down' function"):
            execute_migration(fresh_db, mock_module, "down")


# ---------------------------------------------------------------------------
# Migration status tests
# ---------------------------------------------------------------------------

class TestMigrationStatus:
    """Tests for migration status functions."""

    def test_get_migration_status_empty(self, fresh_db):
        """Test getting status from empty database."""
        status = get_migration_status(fresh_db)
        assert status == []

    def test_get_migration_status_after_apply(self, fresh_db, initial_migration):
        """Test getting status after applying migration."""
        execute_migration(fresh_db, initial_migration, "up")
        status = get_migration_status(fresh_db)

        assert len(status) == 1
        assert status[0]["migration_id"] == "0001"
        assert "applied_at" in status[0]

    def test_get_applied_migrations(self, fresh_db, initial_migration):
        """Test getting list of applied migrations."""
        execute_migration(fresh_db, initial_migration, "up")
        applied = get_applied_migrations(fresh_db)

        assert applied == ["0001"]

    def test_get_pending_migrations(self, fresh_db, initial_migration):
        """Test getting list of pending migrations."""
        # Get all migrations
        all_migrations = get_all_migrations()

        # No migrations applied yet
        pending = get_pending_migrations(fresh_db, all_migrations)
        assert len(pending) == len(all_migrations)

        # Apply one migration
        execute_migration(fresh_db, initial_migration, "up")
        pending = get_pending_migrations(fresh_db, all_migrations)
        assert len(pending) == len(all_migrations) - 1


# ---------------------------------------------------------------------------
# Isolation test tests
# ---------------------------------------------------------------------------

class TestMigrationIsolation:
    """Tests for migration isolation testing."""

    def test_test_migration_isolation_success(self, initial_migration):
        """Test successful isolation test."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            result = test_migration_isolation(
                path,
                "0001",
                "app/migrations/versions/0001_initial_schema.py"
            )
            assert result is True
        finally:
            os.unlink(path)

    def test_test_migration_isolation_wrong_id_fails(self):
        """Test that wrong migration ID fails."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            with pytest.raises(AssertionError, match="Migration ID mismatch"):
                test_migration_isolation(
                    path,
                    "9999",  # Wrong ID
                    "app/migrations/versions/0001_initial_schema.py"
                )
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Table creation tests
# ---------------------------------------------------------------------------

class TestTableCreation:
    """Tests for table creation verification."""

    def test_test_migration_creates_expected_tables(self, initial_migration):
        """Test that migration creates expected tables."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            result = test_migration_creates_expected_tables(
                path,
                "app/migrations/versions/0001_initial_schema.py",
                ["tasks"]
            )
            assert result is True
        finally:
            os.unlink(path)

    def test_test_migration_creates_expected_tables_multiple(self, initial_migration):
        """Test that sequential migrations create the expected tables."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # 0002 ALTERs the tasks table, so 0001 must be applied first
            prereq_migration = load_migration("app/migrations/versions/0002_add_prerequisites.py")
            conn = create_fresh_db(path)
            try:
                execute_migration(conn, initial_migration, "up")
                execute_migration(conn, prereq_migration, "up")
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                )
                tables = {row["name"] for row in cursor.fetchall()}
                assert "tasks" in tables
                assert "schema_migrations" in tables
            finally:
                conn.close()
        finally:
            os.unlink(path)

    def test_test_migration_creates_expected_tables_missing_fails(self):
        """Test that missing expected table fails."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            with pytest.raises(AssertionError, match="Expected table 'nonexistent' was not created"):
                test_migration_creates_expected_tables(
                    path,
                    "app/migrations/versions/0001_initial_schema.py",
                    ["tasks", "nonexistent"]
                )
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Rollback safety tests
# ---------------------------------------------------------------------------

class TestRollbackSafety:
    """Tests for rollback safety verification."""

    def test_test_migration_rollback_safe(self, initial_migration):
        """Test that migration rollback is safe."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # 0001's down() drops tasks; nothing extra expected after rollback
            result = test_migration_rollback_safe(
                path,
                "app/migrations/versions/0001_initial_schema.py",
                before_tables=[],
                after_tables=[]
            )
            assert result is True
        finally:
            os.unlink(path)

    def test_test_migration_rollback_safe_with_before_tables(self, initial_migration):
        """Test rollback leaves schema_migrations intact."""
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)

        try:
            # 0001 creates tasks; after rollback only schema_migrations (from create_fresh_db) remains
            result = test_migration_rollback_safe(
                path,
                "app/migrations/versions/0001_initial_schema.py",
                before_tables=[],
                after_tables=["schema_migrations"]
            )
            assert result is True
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# Integration tests with real migrations
# ---------------------------------------------------------------------------

class TestRealMigrations:
    """Integration tests with actual migration files."""

    def test_load_all_migrations(self):
        """Test loading all migrations from versions directory."""
        migrations = get_all_migrations()

        assert len(migrations) > 0

        # Check that migrations are sorted
        migration_ids = [mid for mid, _ in migrations]
        for i in range(len(migration_ids) - 1):
            assert int(migration_ids[i]) <= int(migration_ids[i + 1])

        # Check that each migration has required attributes
        for migration_id, mod in migrations:
            assert hasattr(mod, "up")
            assert hasattr(mod, "down")
            assert hasattr(mod, "description")

    def test_apply_and_rollback_all_migrations(self, fresh_db):
        """Test applying and rolling back all migrations."""
        all_migrations = get_all_migrations()

        # Apply all migrations
        for migration_id, mod in all_migrations:
            execute_migration(fresh_db, mod, "up")

        # Verify all are applied
        applied = get_applied_migrations(fresh_db)
        applied_ids = {mid for mid in applied}
        expected_ids = {mid for mid, _ in all_migrations}
        assert applied_ids == expected_ids

        # Rollback all migrations
        for migration_id, mod in reversed(all_migrations):
            execute_migration(fresh_db, mod, "down")

        # Verify all are rolled back
        applied = get_applied_migrations(fresh_db)
        assert len(applied) == 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
