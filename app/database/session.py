"""
Database engine, session factory, and Base for all SQLAlchemy models.

This module is imported by models.py (for Base) and by all crud_*.py modules
(for SessionLocal).  It must NOT import from models.py or any crud module —
doing so would create a circular import.
"""

from sqlalchemy import create_engine, event, text
from sqlalchemy.orm import declarative_base, sessionmaker
import logging
import os
from app.agent.config import DATABASE_URL as CFG_DATABASE_URL, PROJECT_ROOT

logger = logging.getLogger(__name__)

# DATABASE_PATH — canonical SQLite file location (used by migration scripts and
# the SQLite init check in crud_tasks.py; also serves as the migration source
# for scripts/migrate_to_postgres.py).
DATABASE_PATH = (
    os.environ.get("MAESTRO_TEST_DB")
    or os.path.join(PROJECT_ROOT, "data", "kanban.db")
)
os.makedirs(os.path.dirname(os.path.abspath(DATABASE_PATH)), exist_ok=True)

# MAESTRO_TEST_DB contains a raw file path (e.g. "data/test.db").
# When set, always use SQLite regardless of the use_postgres config flag so
# that the test suite never accidentally connects to the production database.
_test_db_path = os.environ.get("MAESTRO_TEST_DB")
if _test_db_path:
    DATABASE_URL = f"sqlite:///{_test_db_path}"
else:
    DATABASE_URL = CFG_DATABASE_URL
    if DATABASE_URL.startswith("sqlite"):
        raise RuntimeError(
            "SQLite is only supported in tests (set MAESTRO_TEST_DB). "
            "For production set MAESTRO_USE_POSTGRES=true and MAESTRO_DATABASE_URL in .env."
        )

# Create database engine
if DATABASE_URL.startswith("sqlite"):
    # SQLite configuration with 30s busy timeout
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        connect_args={"timeout": 30}
    )
    
    @event.listens_for(engine, "connect")
    def set_sqlite_pragma(dbapi_connection, connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.close()
else:
    # PostgreSQL configuration (uses default connection pooling)
    engine = create_engine(
        DATABASE_URL,
        echo=False,
        pool_size=10,
        max_overflow=20,
        pool_pre_ping=True
    )


# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for all declarative models
Base = declarative_base()


def get_db():
    """Yield a database session (FastAPI dependency)."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db_tables():
    """Create all tables declared on Base (idempotent)."""
    # Import all model modules so their classes register on Base before
    # create_all() is called.  This avoids missing-table bugs if models.py
    # hasn't been imported yet by the caller.
    from . import models  # noqa: F401
    Base.metadata.create_all(bind=engine)

    # For SQLite (tests), Base.metadata.create_all doesn't add missing columns
    # to existing tables.  We manually ensure Phase 1 additive columns exist
    # so existing tests don't break.
    if DATABASE_URL.startswith("sqlite"):
        try:
            with engine.connect() as conn:
                # tasks.stage_key
                try:
                    conn.execute(text("ALTER TABLE tasks ADD COLUMN stage_key TEXT"))
                except Exception:
                    pass
                # projects.pipeline_template_id
                try:
                    conn.execute(text("ALTER TABLE projects ADD COLUMN pipeline_template_id INTEGER"))
                except Exception:
                    pass
                conn.execute(text("UPDATE tasks SET stage_key = type WHERE stage_key IS NULL"))
                # budget_entries.prompt_message_count (delta storage)
                try:
                    conn.execute(text("ALTER TABLE budget_entries ADD COLUMN prompt_message_count INTEGER"))
                except Exception:
                    pass
                conn.commit()
        except Exception as e:
            logger.debug("Minor: could not verify SQLite additive columns: %s", e)

    logger.info("Database tables initialized: %s", DATABASE_URL)
