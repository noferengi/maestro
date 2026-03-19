"""
Test database isolation.

All tests run against  data/test.db  — a named file that is:
  • completely separate from the production  data/kanban.db
  • listed in .gitignore so it is never committed
  • left on disk after the run so failures can be inspected
  • brought up-to-date via the real migration runner (same path as production)
  • flushed of all data rows at the start of every pytest session

Setting MAESTRO_TEST_DB at module level (before any test module is collected)
ensures that both database.py and migrations/runner.py build their engines /
connections against test.db when they are first imported during the session.
"""

import os
import sys
import sqlite3
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Redirect all DB I/O to the test database.
# Module-level assignment runs during pytest collection, before any test
# module triggers `import database` or `import migrations.runner`.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent.parent   # …/TheMaestro
_TEST_DB = _PROJECT_ROOT / "data" / "test.db"

os.environ["MAESTRO_TEST_DB"] = str(_TEST_DB)


# ---------------------------------------------------------------------------
# Session fixture: apply migrations then wipe all data rows.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _test_schema():
    """
    Bring test.db up to the latest migration, then truncate every data table
    so each pytest session starts from a clean slate.

    Using the real migration runner (not Base.metadata.create_all) means the
    test schema is always identical to what `migrate.bat migrate` produces in
    production.
    """
    # Make sure app/ is importable (test files already do sys.path.insert
    # individually, but we need it here for the runner import below).
    _app_dir = str(_PROJECT_ROOT / "app")
    if _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)

    _TEST_DB.parent.mkdir(parents=True, exist_ok=True)

    # Apply any pending migrations (no-op when already current).
    from migrations.runner import get_connection, migrate as run_migrate
    conn = get_connection()
    try:
        run_migrate(conn)
    finally:
        conn.close()

    # Truncate all user tables (preserve schema_migrations so the runner
    # stays aware of what has been applied).
    conn2 = sqlite3.connect(str(_TEST_DB))
    try:
        conn2.execute("PRAGMA foreign_keys = OFF")
        tables = conn2.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='table' "
            "  AND name NOT LIKE 'sqlite_%' "
            "  AND name != 'schema_migrations'"
        ).fetchall()
        for row in tables:
            conn2.execute(f"DELETE FROM [{row[0]}]")
        conn2.execute("PRAGMA foreign_keys = ON")
        conn2.commit()
    finally:
        conn2.close()

    yield
    # Leave test.db on disk — useful for post-mortem inspection of failures.
