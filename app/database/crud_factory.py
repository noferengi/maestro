"""
CRUD helpers for the factory_runs audit table.
"""

import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import FactoryRun

logger = logging.getLogger(__name__)


def create_factory_run(
    *,
    factory_stage_id: int,
    project_id: int,
    trigger_type: str,
    trigger_card_id: str | None = None,
) -> FactoryRun | None:
    db = SessionLocal()
    try:
        run = FactoryRun(
            factory_stage_id=factory_stage_id,
            project_id=project_id,
            trigger_type=trigger_type,
            trigger_card_id=trigger_card_id,
            status="running",
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    except Exception:
        db.rollback()
        logger.exception("create_factory_run failed")
        return None
    finally:
        db.close()


def update_factory_run(
    run_id: int,
    *,
    status: str,
    cards_created: int = 0,
    completed_at: datetime | None = None,
) -> bool:
    db = SessionLocal()
    try:
        run = db.query(FactoryRun).filter(FactoryRun.id == run_id).first()
        if not run:
            return False
        run.status = status
        run.cards_created = cards_created
        run.completed_at = completed_at or datetime.now(timezone.utc)
        db.commit()
        return True
    except Exception:
        db.rollback()
        logger.exception("update_factory_run failed")
        return False
    finally:
        db.close()


def get_factory_run(run_id: int) -> FactoryRun | None:
    db = SessionLocal()
    try:
        return db.query(FactoryRun).filter(FactoryRun.id == run_id).first()
    finally:
        db.close()


def get_factory_runs_for_stage(factory_stage_id: int, *, limit: int = 20) -> list[FactoryRun]:
    db = SessionLocal()
    try:
        return (
            db.query(FactoryRun)
            .filter(FactoryRun.factory_stage_id == factory_stage_id)
            .order_by(FactoryRun.started_at.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def get_last_cron_run_at(factory_stage_id: int) -> datetime | None:
    """Return completed_at of the most recent completed cron run, or None."""
    db = SessionLocal()
    try:
        run = (
            db.query(FactoryRun)
            .filter(
                FactoryRun.factory_stage_id == factory_stage_id,
                FactoryRun.trigger_type == "cron",
                FactoryRun.status == "completed",
            )
            .order_by(FactoryRun.completed_at.desc())
            .first()
        )
        return run.completed_at if run else None
    finally:
        db.close()


def predecessor_already_triggered(factory_stage_id: int, trigger_card_id: str) -> bool:
    """True if a factory run was already created for this (stage, card) pair."""
    db = SessionLocal()
    try:
        return (
            db.query(FactoryRun)
            .filter(
                FactoryRun.factory_stage_id == factory_stage_id,
                FactoryRun.trigger_card_id == trigger_card_id,
                FactoryRun.trigger_type == "predecessor_complete",
            )
            .first()
            is not None
        )
    finally:
        db.close()


def factory_run_to_dict(run: FactoryRun) -> dict:
    return {
        "id": run.id,
        "factory_stage_id": run.factory_stage_id,
        "project_id": run.project_id,
        "trigger_type": run.trigger_type,
        "trigger_card_id": run.trigger_card_id,
        "started_at": run.started_at.isoformat() if run.started_at else None,
        "completed_at": run.completed_at.isoformat() if run.completed_at else None,
        "cards_created": run.cards_created,
        "status": run.status,
    }
