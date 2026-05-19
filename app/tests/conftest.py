"""
Test database isolation — PostgreSQL edition.

All tests run against maestro_test_db (a dedicated PostgreSQL database on the
same server as production, never the production database itself).

How isolation works
-------------------
Session setup  (_test_schema, scope="session"):
  1. Applies any pending migrations to the test DB via the migration runner.
  2. Truncates every data table (CASCADE) so the session starts from a clean
     slate.  schema_migrations is preserved so the runner stays consistent.

Per-test  (_db_rollback, autouse, scope="function"):
  - Opens one connection and begins an outer transaction that is NEVER committed.
  - Patches every module's SessionLocal to return sessions bound to that
    connection with join_transaction_mode="create_savepoint".  A session.commit()
    inside test code releases a savepoint instead of committing the outer
    transaction — writes are visible within the test but never persisted.
  - On teardown: restores all SessionLocal references, then rolls back and
    closes the connection.

This means every test starts with an empty database (except baseline rows
inserted during the test itself) and leaves no state behind.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

# ---------------------------------------------------------------------------
# Signal test mode BEFORE importing app.database.
# session.py reads MAESTRO_TEST at import time to select the test DB URL.
# ---------------------------------------------------------------------------
_PROJECT_ROOT = Path(__file__).parent.parent.parent
os.environ["MAESTRO_TEST"] = "1"

# Warm up SQLAlchemy (large C extension; on Windows with AV this can take 30s
# on a cold import — doing it here instead of inside the first test avoids
# apparent hangs in test output).
import app.database  # noqa: F401


# ---------------------------------------------------------------------------
# Session fixture: migrate then truncate.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="session", autouse=True)
def _test_schema():
    """Apply pending migrations to the test DB, then truncate all data rows."""
    _app_dir = str(_PROJECT_ROOT / "app")
    if _app_dir not in sys.path:
        sys.path.insert(0, _app_dir)

    # Run migrations against the test DB.  get_connection("test") uses
    # MAESTRO_TEST_ADMIN_DATABASE_URL — never touches production.
    from migrations.runner import get_connection, migrate as run_migrate, ConnectionWrapper
    test_engine, is_pg = get_connection("test")
    with test_engine.begin() as conn:
        run_migrate(ConnectionWrapper(conn, is_pg))

    # Truncate all user tables so every test session starts empty.
    # CASCADE handles FK dependencies in one pass; RESTART IDENTITY resets
    # serial sequences so IDs start from 1 in each session.
    from app.database.session import engine as app_engine
    with app_engine.begin() as conn:
        result = conn.execute(text(
            "SELECT tablename FROM pg_catalog.pg_tables "
            "WHERE schemaname = 'public' AND tablename != 'schema_migrations'"
        ))
        tables = [row[0] for row in result]
        if tables:
            quoted = ", ".join(f'"{t}"' for t in tables)
            conn.execute(text(f"TRUNCATE TABLE {quoted} CASCADE"))

    # Eagerly import database and main so they are in sys.modules for patching.
    import database  # noqa: F401
    try:
        import main  # noqa: F401
    except Exception:
        pass

    yield
    # Leave the test DB populated for post-mortem inspection if a session fails.


# ---------------------------------------------------------------------------
# Per-test fixture: savepoint-based rollback.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _db_rollback():
    """
    Wrap every test in a transaction that is rolled back after the test.

    Mechanism:
      - Open one connection to the test engine and begin an outer transaction.
      - Patch SessionLocal in every module that has it (within the app./database
        namespace) to a factory that returns Session objects bound to that
        connection with join_transaction_mode="create_savepoint".  With this
        mode, session.commit() releases a savepoint instead of committing the
        outer transaction, so CRUD writes are visible within the test but never
        hit the database permanently.
      - On teardown: restore all original SessionLocal references, then roll
        back the outer transaction and close the connection.
    """
    from app.database.session import engine

    conn = engine.connect()
    conn.begin()  # outer transaction — never committed

    def make_session():
        return Session(conn, join_transaction_mode="create_savepoint", autoflush=False)

    originals = []
    for modname, mod in list(sys.modules.items()):
        if (
            modname.startswith("app.")
            or modname.startswith("database")
            or modname == "main"
        ) and hasattr(mod, "SessionLocal"):
            originals.append((mod, mod.SessionLocal))
            mod.SessionLocal = make_session

    yield

    for mod, orig in originals:
        mod.SessionLocal = orig

    try:
        conn.rollback()
    finally:
        conn.close()
