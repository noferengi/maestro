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
  3. Opens ONE outer transaction on a shared connection held for the entire
     pytest session (never committed).

Per-test  (_db_rollback, autouse, scope="function"):
  - Creates a SAVEPOINT on the shared connection at the start of each test.
  - Patches every module's SessionLocal (using a pre-built cache, not a full
    sys.modules scan) to return Session objects bound to the shared connection
    with join_transaction_mode="create_savepoint".  A session.commit() inside
    test code releases an inner savepoint instead of committing the outer
    transaction — writes are visible within the test but never persisted.
  - On teardown: restores all SessionLocal references, then rolls back to the
    test's savepoint (ROLLBACK TO SAVEPOINT), discarding all writes.

This means every test starts with an empty database and leaves no state behind,
while paying only ~1 round-trip per test (SAVEPOINT + ROLLBACK TO) instead of
2 (connection open + close).
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
# Session fixture: migrate, truncate, open the shared outer connection.
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


@pytest.fixture(scope="session")
def _shared_conn(_test_schema):
    """
    One database connection held open for the entire pytest session.

    An outer transaction is started here and never committed.  Per-test
    isolation is provided by SAVEPOINT / ROLLBACK TO SAVEPOINT in _db_rollback.
    """
    from app.database.session import engine
    conn = engine.connect()
    conn.begin()  # outer transaction — never committed
    yield conn
    try:
        conn.rollback()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Module cache: built once, reused by every test.
# ---------------------------------------------------------------------------

# Populated by _build_session_local_cache() after all imports are done.
_SESSION_LOCAL_MODULES: list = []


@pytest.fixture(scope="session", autouse=True)
def _build_session_local_cache(_test_schema):
    """
    Walk sys.modules once (after migrations + eager imports) and cache every
    module that has a SessionLocal attribute.  Avoids a full sys.modules scan
    on every test (~2 000 modules x 1 000 tests = 2 M iterations saved).
    """
    for modname, mod in list(sys.modules.items()):
        if (
            modname.startswith("app.")
            or modname.startswith("database")
            or modname == "main"
        ) and hasattr(mod, "SessionLocal"):
            _SESSION_LOCAL_MODULES.append(mod)


# ---------------------------------------------------------------------------
# Per-test fixture: savepoint-based rollback on the shared connection.
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def _db_rollback(_shared_conn, _build_session_local_cache):
    """
    Wrap every test in a SAVEPOINT that is rolled back on teardown.

    Uses the session-scoped _shared_conn so no DB connection is opened or
    closed per test.  The pre-built _SESSION_LOCAL_MODULES cache avoids
    scanning sys.modules on every test.
    """
    sp = _shared_conn.begin_nested()  # SAVEPOINT

    def make_session():
        return Session(_shared_conn, join_transaction_mode="create_savepoint",
                       autoflush=False)

    originals = [(mod, mod.SessionLocal) for mod in _SESSION_LOCAL_MODULES]
    for mod, _ in originals:
        mod.SessionLocal = make_session

    yield

    for mod, orig in originals:
        mod.SessionLocal = orig

    sp.rollback()  # ROLLBACK TO SAVEPOINT — discards all test writes


@pytest.fixture(autouse=True)
def _reset_shutdown_events():
    yield
    from app.agent.llm_client import _shutdown_event, _force_shutdown_event
    _shutdown_event.clear()
    _force_shutdown_event.clear()
