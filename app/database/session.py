"""
Database engine, session factory, and Base for all SQLAlchemy models.

This module is imported by models.py (for Base) and by all crud_*.py modules
(for SessionLocal).  It must NOT import from models.py or any crud module —
doing so would create a circular import.
"""

from sqlalchemy import create_engine, event
from sqlalchemy.orm import declarative_base, sessionmaker
import logging
import os

logger = logging.getLogger(__name__)

# Database path — keep it in the project's data/ directory.
# MAESTRO_TEST_DB env var lets conftest.py redirect to a temp file per session.
# NOTE: __file__ is app/database/session.py so we need two levels up to reach
# the repo root, then down into data/.
DATABASE_PATH = (
    os.environ.get("MAESTRO_TEST_DB")
    or os.path.join(os.path.dirname(__file__), '..', '..', 'data', 'kanban.db')
)

# Ensure data directory exists
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

# Create database engine
engine = create_engine(f"sqlite:///{DATABASE_PATH}", echo=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


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
    logger.info("Database tables initialized at: %s", DATABASE_PATH)
