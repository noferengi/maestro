"""
Pytest configuration for TheMaestro migration tests and research agent tests.

Sets up test database and environment variables for isolated testing.
"""

import os
import sys
import pytest
from pathlib import Path

# Add parent directory to path for imports
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

# Signal test mode so session.py uses MAESTRO_TEST_DATABASE_URL.
os.environ["MAESTRO_TEST"] = "1"


@pytest.fixture(scope="session")
def test_db_path(tmp_path_factory):
    """
    Create a temporary test database for the test session.

    This fixture creates a unique test database for each test session,
    ensuring complete isolation from the production database.

    Yields:
        Path: Path to the temporary test database
    """
    tmp_path = tmp_path_factory.getbasetemp()
    test_db = tmp_path / "test_kanban.db"

    # Set environment variable for test database
    os.environ["MAESTRO_TEST_DB"] = str(test_db)

    yield test_db

    # Clean up: remove test database after session
    if test_db.exists():
        test_db.unlink()

    # Clean up environment variable
    if "MAESTRO_TEST_DB" in os.environ:
        del os.environ["MAESTRO_TEST_DB"]


@pytest.fixture
def fresh_test_db(test_db_path):
    """
    Create a fresh test database with migrations table.

    This fixture creates a new test database for each test function,
    ensuring complete isolation between tests.

    Yields:
        sqlite3.Connection: Connection to the fresh test database
    """
    import sqlite3

    conn = sqlite3.connect(str(test_db_path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # Create migrations table
    conn.execute("""
        CREATE TABLE IF NOT EXISTS schema_migrations (
            migration_id TEXT PRIMARY KEY,
            applied_at   DATETIME NOT NULL
        )
    """)
    conn.commit()

    yield conn

    # Close connection after test
    conn.close()


@pytest.fixture
def sample_migration():
    """Load the initial schema migration for testing."""
    from app.migrations.test_framework import load_migration
    return load_migration("app/migrations/versions/0001_initial_schema.py")


@pytest.fixture
def subdivision_migration():
    """Load the subdivision support migration for testing."""
    from app.migrations.test_framework import load_migration
    return load_migration("app/migrations/versions/0010_add_subdivision_support.py")


# ---------------------------------------------------------------------------
# Research Agent Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_task_context():
    return {
        "task_id": "task-42",
        "title": "Add WebSocket support",
        "project": "Maestro",
        "description": "Implement real-time updates using WebSockets.",
    }

@pytest.fixture
def mock_llm_pass():
    from app.agent.mock_llm import MockLLM
    return MockLLM(scenario="pass")

@pytest.fixture
def mock_llm_fail():
    from app.agent.mock_llm import MockLLM
    return MockLLM(scenario="fail")

@pytest.fixture
def mock_llm_needs_research():
    from app.agent.mock_llm import MockLLM
    return MockLLM(scenario="needs_research")

@pytest.fixture
def mock_llm_exhaust_lives():
    from app.agent.mock_llm import MockLLM
    return MockLLM(scenario="exhaust_lives")

@pytest.fixture
def mock_llm_not_suitable():
    from app.agent.mock_llm import MockLLM
    return MockLLM(scenario="not_suitable")


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def get_all_migrations():
    """Get all available migrations."""
    from app.migrations.test_framework import get_all_migrations
    return get_all_migrations()


def print_migration_status(conn):
    """Print migration status for debugging."""
    from app.migrations.test_framework import get_migration_status, get_all_migrations

    all_migrations = get_all_migrations()
    status = get_migration_status(conn)

    print(f"\nMigration Status:")
    print("-" * 60)
    for migration_id, module in all_migrations:
        desc = getattr(module, "description", migration_id)
        state = status.get(migration_id, "pending")
        print(f"  {migration_id}: {state} - {desc}")
    print("-" * 60)


# ---------------------------------------------------------------------------
# Intake Pipeline Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_all_tasks():
    """A sample list of tasks for conflict detection tests."""
    return [
        {
            "id": "task-existing-1",
            "title": "Database Schema Design",
            "type": "planning",
            "description": "Designing the core database schema for the kanban board.",
            "project": "Maestro"
        },
        {
            "id": "task-existing-2",
            "title": "React Dashboard UI",
            "type": "indev",
            "description": "Implementing the main dashboard view with React components.",
            "project": "Maestro"
        },
        {
            "id": "task-existing-3",
            "title": "Authentication API",
            "type": "completed",
            "description": "User authentication endpoints and middleware.",
            "project": "Maestro"
        }
    ]
