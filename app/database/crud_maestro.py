"""
CRUD operations for the MaestroRun table.

Maestro is the autonomous project-orchestration and resurrection agent.
These helpers create and update run records so the scheduler and UI can
track Maestro activity.
"""

import json
import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import MaestroRun, ProjectDecision, Project, TaskSessionState

logger = logging.getLogger(__name__)


def save_task_session_state(task_id: str, session_id: int, turn_count: int, messages: list[dict]) -> bool:
    """Save the serialized message history for a suspended task."""
    db = SessionLocal()
    try:
        # Check for existing state
        state = db.query(TaskSessionState).filter(TaskSessionState.task_id == task_id).first()
        if state:
            state.session_id = session_id
            state.turn_count = turn_count
            state.messages = json.dumps(messages)
        else:
            state = TaskSessionState(
                task_id=task_id,
                session_id=session_id,
                turn_count=turn_count,
                messages=json.dumps(messages)
            )
            db.add(state)
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error saving session state for '%s': %s", task_id, exc)
        return False
    finally:
        db.close()


def get_task_session_state(task_id: str) -> "tuple[int, int, list[dict]] | None":
    """Return (session_id, turn_count, messages) for a suspended task."""
    db = SessionLocal()
    try:
        state = db.query(TaskSessionState).filter(TaskSessionState.task_id == task_id).first()
        if not state:
            return None
        return state.session_id, state.turn_count, json.loads(state.messages)
    except Exception as exc:
        logger.error("Error fetching session state for '%s': %s", task_id, exc)
        return None
    finally:
        db.close()


def delete_task_session_state(task_id: str) -> bool:
    """Clear the saved session state after a successful resume."""
    db = SessionLocal()
    try:
        db.query(TaskSessionState).filter(TaskSessionState.task_id == task_id).delete()
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error deleting session state for '%s': %s", task_id, exc)
        return False
    finally:
        db.close()


def get_project_decisions(project_name: str, only_binding: bool = True) -> "list[ProjectDecision]":
    """Return all architectural decisions for a project."""
    db = SessionLocal()
    try:
        query = db.query(ProjectDecision).join(Project).filter(Project.name == project_name)
        if only_binding:
            query = query.filter(ProjectDecision.is_binding == True)
        return query.order_by(ProjectDecision.created_at.asc()).all()
    except Exception as exc:
        logger.error("Error fetching decisions for '%s': %s", project_name, exc)
        return []
    finally:
        db.close()


def upsert_project_decision(
    project_name: str,
    topic: str,
    decision: str,
    rationale: str | None = None,
    is_binding: bool = True
) -> bool:
    """Create or update a project decision."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == project_name).first()
        if not project:
            return False
        
        # Check for existing topic
        existing = db.query(ProjectDecision).filter(
            ProjectDecision.project_id == project.id,
            ProjectDecision.topic == topic
        ).first()
        
        if existing:
            existing.decision = decision
            existing.rationale = rationale
            existing.is_binding = is_binding
        else:
            new_dec = ProjectDecision(
                project_id=project.id,
                topic=topic,
                decision=decision,
                rationale=rationale,
                is_binding=is_binding
            )
            db.add(new_dec)
        
        db.commit()
        return True
    except Exception as exc:
        db.rollback()
        logger.error("Error upserting decision for '%s': %s", project_name, exc)
        return False
    finally:
        db.close()


def create_maestro_run(project_name: str, llm_id: "int | None", budget_id: "int | None"):
    """Create a new MaestroRun record in 'running' status."""
    db = SessionLocal()
    try:
        run = MaestroRun(
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
        logger.error("Error creating MaestroRun for '%s': %s", project_name, exc)
        return None
    finally:
        db.close()


def update_maestro_run(
    run_id: int,
    *,
    status: "str | None" = None,
    stall_reason: "str | None" = None,
    actions_taken: "list | None" = None,
    new_task_ids: "list | None" = None,
) -> bool:
    """Update a MaestroRun record.  Only provided (non-None) fields are updated."""
    db = SessionLocal()
    try:
        run = db.query(MaestroRun).filter(MaestroRun.id == run_id).first()
        if not run:
            logger.warning("update_maestro_run: run %d not found.", run_id)
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
        logger.error("Error updating MaestroRun %d: %s", run_id, exc)
        return False
    finally:
        db.close()


def get_maestro_runs(project_name: str, limit: int = 20) -> "list[MaestroRun]":
    """Return the most recent MaestroRun records for a project, newest first."""
    db = SessionLocal()
    try:
        return (
            db.query(MaestroRun)
              .filter(MaestroRun.project_name == project_name)
              .order_by(MaestroRun.id.desc())
              .limit(limit)
              .all()
        )
    except Exception as exc:
        logger.error("Error fetching MaestroRuns for '%s': %s", project_name, exc)
        return []
    finally:
        db.close()


def get_maestro_run(run_id: int) -> "MaestroRun | None":
    """Return a single MaestroRun by ID."""
    db = SessionLocal()
    try:
        return db.query(MaestroRun).filter(MaestroRun.id == run_id).first()
    except Exception as exc:
        logger.error("Error fetching MaestroRun %d: %s", run_id, exc)
        return None
    finally:
        db.close()
