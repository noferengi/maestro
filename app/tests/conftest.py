"""
Test database isolation.

All tests run against  data/test.db  - a named file that is:
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
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Redirect all DB I/O to the test database.
# Module-level assignment runs during pytest collection, before any test
# module triggers `import database` or `import migrations.runner`.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent.parent   # …/TheMaestro
_TEST_DB = _PROJECT_ROOT / "data" / "test.db"

os.environ["MAESTRO_TEST_DB"] = str(_TEST_DB)

# Eagerly import app.database so that SQLAlchemy (a large library) is
# warmed up here during session setup rather than inside the first test
# that needs it.  On Windows with AV scanning enabled the cold import can
# take 30+ seconds, which makes individual tests appear to hang forever.
import app.database  # noqa: F401

# Ensure the SQLite test DB has all current ORM-defined tables (including
# Phase 1 pipeline_* tables).  The migration runner in _test_schema targets
# Postgres; create_all() handles the SQLite schema so pipeline_router queries
# don't raise OperationalError during tests.
from app.database.session import init_db_tables
init_db_tables()


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
    # get_connection() returns (engine, is_postgres); use engine.begin() for the
    # ConnectionWrapper the runner expects.
    from migrations.runner import get_connection, migrate as run_migrate, ConnectionWrapper
    engine, is_postgres = get_connection()
    with engine.begin() as conn:
        run_migrate(ConnectionWrapper(conn, is_postgres))

    # Eagerly import database and main so they are in sys.modules for patching.
    # main.py adds app/ to sys.path, so 'import database' works here too.
    import database  # noqa: F401
    try:
        import main  # noqa: F401
    except Exception:
        # main.py might fail on import if some env vars are missing, but 
        # we try our best to get it into sys.modules.
        pass

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
    # Leave test.db on disk - useful for post-mortem inspection of failures.


@pytest.fixture(autouse=True)
def _db_rollback():
    """
    Wrap every test in a transaction that is rolled back after the test completes.

    Mechanism:
      - Open one connection to the test engine and begin an outer transaction.
      - Patch SessionLocal in every module that has it (within the app. namespace)
        to a factory that returns Session objects bound to that connection with
        join_transaction_mode="create_savepoint". With this mode, session.commit()
        releases a savepoint instead of committing the outer transaction, so
        CRUD writes are visible within the test but never hit the database
        permanently.
      - On teardown: restore all original SessionLocal references, then roll
        back the outer transaction and close the connection.

    Safe for Pattern 2 tests (test_research_jobs, test_optimization_subtasks)
    that use importlib.reload(): those tests redirect MAESTRO_TEST_DB to a
    tmp_path and reload app.database, which replaces the CRUD module SessionLocal
    references entirely.  The reload blows away our patches for the duration
    of the test body—which is correct, since the test is operating on a completely
    different database.
    """
    from app.database.session import engine

    conn = engine.connect()
    conn.begin()  # outer transaction — never committed

    def make_session():
        return Session(conn, join_transaction_mode="create_savepoint", autoflush=False)

    originals = []
    # Dynamically discover all app.* and database.* modules that have SessionLocal.
    # We must catch both because app/main.py often adds 'app/' to sys.path and 
    # imports 'database' directly, which can result in duplicate module objects
    # if app.database was also imported.
    for modname, mod in list(sys.modules.items()):
        if (modname.startswith("app.") or modname.startswith("database") or modname == "main") and hasattr(mod, "SessionLocal"):
            originals.append((mod, mod.SessionLocal))
            mod.SessionLocal = make_session

    yield

    # Restore originals first, then rollback (order matters: rollback must
    # happen before the connection is closed so in-flight savepoints resolve).
    for mod, orig in originals:
        mod.SessionLocal = orig

    try:
        conn.rollback()
    finally:
        conn.close()
