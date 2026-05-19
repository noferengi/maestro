"""
Database engine, session factory, and Base for all SQLAlchemy models.

This module is imported by models.py (for Base) and by all crud_*.py modules
(for SessionLocal).  It must NOT import from models.py or any crud module —
doing so would create a circular import.

Test mode
---------
When the test suite sets MAESTRO_TEST=1 before importing this module, the
engine connects to MAESTRO_TEST_DATABASE_URL (a separate PostgreSQL database
on the same server) instead of the production MAESTRO_DATABASE_URL.
"""

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker
import logging
import os
from app.agent.config import DATABASE_URL as CFG_DATABASE_URL, TEST_DATABASE_URL, PROJECT_ROOT

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Choose URL: test DB when MAESTRO_TEST=1, production DB otherwise.
# ---------------------------------------------------------------------------
_in_test = bool(os.environ.get("MAESTRO_TEST"))
if _in_test:
    if not TEST_DATABASE_URL:
        raise RuntimeError(
            "MAESTRO_TEST=1 is set but MAESTRO_TEST_DATABASE_URL is not configured. "
            "Add it to .env and create the maestro_test_db PostgreSQL database."
        )
    DATABASE_URL = TEST_DATABASE_URL
else:
    DATABASE_URL = CFG_DATABASE_URL

# Legacy alias — some scripts reference DATABASE_PATH expecting a file path.
# For PostgreSQL this is a no-op placeholder; the URL above is authoritative.
DATABASE_PATH = os.path.join(PROJECT_ROOT, "data", "kanban.db")

# ---------------------------------------------------------------------------
# Engine (PostgreSQL only)
# ---------------------------------------------------------------------------
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

# Session factory
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
    """Create any missing tables declared on Base (idempotent, PostgreSQL-safe).

    In production the migration runner owns the schema; this is a safety net
    for development environments that bypass the runner.  It does NOT apply
    data migrations (seed rows, column backfills, etc.).
    """
    from . import models  # noqa: F401 — registers ORM models on Base
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables verified: %s", DATABASE_URL.split("@")[-1])
