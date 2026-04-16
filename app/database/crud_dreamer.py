"""
CRUD operations for the DreamerRun table.

Dreamer is the autonomous project-resurrection agent.  These helpers create
and update run records so the scheduler and UI can track Dreamer activity.
"""

import json
import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import DreamerRun

logger = logging.getLogger(__name__)


def create_dreamer_run(project_name: str, llm_id: "int | None", budget_id: "int | None"):
    """Create a new DreamerRun record in 'running' status."""
    db = SessionLocal()
    try:
        run = DreamerRun(
            project_name=project_name,
            started_at=datetime.now(timezone.utc).isoformat(),
            status="running",
            llm_id=llm_id,
            budget_id=budget_id,
        )
        db.add(run)
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        logger.error("Error creating DreamerRun for '%s': %s", project_name, exc)
        return None
    finally:
        db.close()


def update_dreamer_run(
    run_id: int,
    *,
    status: "str | None" = None,
    stall_reason: "str | None" = None,
    actions_taken: "list | None" = None,
    new_task_ids: "list | None" = None,
) -> bool:
    """Update a DreamerRun record.  Only provided (non-None) fields are updated."""
    db = SessionLocal()
    try:
        run = db.query(DreamerRun).filter(DreamerRun.id == run_id).first()
        if not run:
            logger.warning("update_dreamer_run: run %d not found.", run_id)
            return False
        if status is not None:
            run.status = status
            if status in ("completed", "failed"):
                run.finished_at = datetime.now(timezone.utc).isoformat()
        if stall_reason is not None:
            run.stall_reason = stall_reason
        if actions_taken is not None:
            run.actions_taken = json.dumps(actions_taken)
        if new_task_ids is not None:
            run.new_task_ids = json.dumps(new_task_ids)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error updating DreamerRun %d: %s", run_id, exc)
        return False
    finally:
        db.close()


def get_dreamer_runs(project_name: str, limit: int = 20) -> "list[DreamerRun]":
    """Return the most recent DreamerRun records for a project, newest first."""
    db = SessionLocal()
    try:
        return (
            db.query(DreamerRun)
              .filter(DreamerRun.project_name == project_name)
              .order_by(DreamerRun.id.desc())
              .limit(limit)
              .all()
        )
    except Exception as exc:
        logger.error("Error fetching DreamerRuns for '%s': %s", project_name, exc)
        return []
    finally:
        db.close()


def get_dreamer_run(run_id: int) -> "DreamerRun | None":
    """Return a single DreamerRun by ID."""
    db = SessionLocal()
    try:
        return db.query(DreamerRun).filter(DreamerRun.id == run_id).first()
    except Exception as exc:
        logger.error("Error fetching DreamerRun %d: %s", run_id, exc)
        return None
    finally:
        db.close()
