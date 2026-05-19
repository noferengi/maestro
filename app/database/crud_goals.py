"""
app/database/crud_goals.py
--------------------------
CRUD for maestro_goals and goal_verification_jobs.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import MaestroGoal, GoalVerificationJob, Project

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Goal CRUD
# ---------------------------------------------------------------------------

def create_goal(
    project_id: int,
    title: str,
    statement: str,
    *,
    criteria: "list | None" = None,
    parent_id: "int | None" = None,
    priority: int = 1,
    color: "str | None" = None,
    created_by: str = "human",
) -> "MaestroGoal | None":
    db = SessionLocal()
    try:
        goal = MaestroGoal(
            project_id=project_id,
            title=title,
            statement=statement,
            criteria=criteria,
            parent_id=parent_id,
            priority=priority,
            color=color,
            created_by=created_by,
        )
        db.add(goal)
        db.commit()
        db.refresh(goal)
        return goal
    except Exception as exc:
        db.rollback()
        logger.error("Error creating goal: %s", exc)
        return None
    finally:
        db.close()


def get_goal(goal_id: int) -> "MaestroGoal | None":
    db = SessionLocal()
    try:
        return db.query(MaestroGoal).filter(MaestroGoal.id == goal_id).first()
    finally:
        db.close()


def get_active_goals_for_project(project_name: str) -> "list[MaestroGoal]":
    db = SessionLocal()
    try:
        project_id = db.query(Project.id).filter(Project.name == project_name).scalar()
        if project_id is None:
            return []
        return (
            db.query(MaestroGoal)
            .filter(
                MaestroGoal.project_id == project_id,
                MaestroGoal.status == "active",
            )
            .order_by(MaestroGoal.priority.asc(), MaestroGoal.created_at.asc())
            .all()
        )
    finally:
        db.close()


def get_goals_for_project(project_name: str) -> "list[MaestroGoal]":
    """Return all non-abandoned goals for a project."""
    db = SessionLocal()
    try:
        project_id = db.query(Project.id).filter(Project.name == project_name).scalar()
        if project_id is None:
            return []
        return (
            db.query(MaestroGoal)
            .filter(
                MaestroGoal.project_id == project_id,
                MaestroGoal.status != "abandoned",
            )
            .order_by(MaestroGoal.priority.asc(), MaestroGoal.created_at.asc())
            .all()
        )
    finally:
        db.close()


def update_goal(goal_id: int, **kwargs) -> "MaestroGoal | None":
    db = SessionLocal()
    try:
        goal = db.query(MaestroGoal).filter(MaestroGoal.id == goal_id).first()
        if not goal:
            return None
        for key, value in kwargs.items():
            setattr(goal, key, value)
        goal.updated_at = datetime.utcnow()
        db.commit()
        db.refresh(goal)
        return goal
    except Exception as exc:
        db.rollback()
        logger.error("Error updating goal %d: %s", goal_id, exc)
        return None
    finally:
        db.close()


def append_goal_evidence(goal_id: int, text: str) -> None:
    """Append a timestamped entry to goal.evidence."""
    db = SessionLocal()
    try:
        goal = db.query(MaestroGoal).filter(MaestroGoal.id == goal_id).first()
        if not goal:
            return
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
        entry = f"\n\n**{ts}**\n{text}"
        goal.evidence = (goal.evidence or "") + entry
        goal.updated_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error appending evidence to goal %d: %s", goal_id, exc)
    finally:
        db.close()


def goal_to_dict(goal: MaestroGoal) -> dict:
    return {
        "id": goal.id,
        "project_id": goal.project_id,
        "title": goal.title,
        "statement": goal.statement,
        "criteria": goal.criteria,
        "status": goal.status,
        "evidence": goal.evidence,
        "progress": goal.progress,
        "last_verdict": goal.last_verdict,
        "parent_id": goal.parent_id,
        "priority": goal.priority,
        "color": goal.color,
        "created_by": goal.created_by,
        "arch_card_id": goal.arch_card_id,
        "created_at": goal.created_at.isoformat() if goal.created_at else None,
        "updated_at": goal.updated_at.isoformat() if goal.updated_at else None,
    }


# ---------------------------------------------------------------------------
# Goal verification job CRUD
# ---------------------------------------------------------------------------

def create_goal_verification_job(
    goal_id: int,
    *,
    triggered_by: "str | None" = "manual",
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    priority: float = 0.0,
    tier: int = 2,
) -> "GoalVerificationJob | None":
    db = SessionLocal()
    try:
        job = GoalVerificationJob(
            goal_id=goal_id,
            triggered_by=triggered_by,
            llm_id=llm_id,
            budget_id=budget_id,
            priority=priority,
            tier=tier,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception as exc:
        db.rollback()
        logger.error("Error creating goal_verification_job: %s", exc)
        return None
    finally:
        db.close()


def get_pending_goal_verification_jobs(limit: int = 5) -> "list[GoalVerificationJob]":
    db = SessionLocal()
    try:
        return (
            db.query(GoalVerificationJob)
            .filter(GoalVerificationJob.status == "pending")
            .order_by(
                GoalVerificationJob.tier.asc(),
                GoalVerificationJob.priority.asc(),
                GoalVerificationJob.created_at.asc(),
            )
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def update_goal_verification_job(job_id: int, **kwargs) -> None:
    db = SessionLocal()
    try:
        job = db.query(GoalVerificationJob).filter(GoalVerificationJob.id == job_id).first()
        if not job:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        if kwargs.get("status") in ("done", "failed") and job.completed_at is None:
            job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error updating goal_verification_job %d: %s", job_id, exc)
    finally:
        db.close()


def get_verification_jobs_for_goal(goal_id: int, limit: int = 20) -> "list[GoalVerificationJob]":
    db = SessionLocal()
    try:
        return (
            db.query(GoalVerificationJob)
            .filter(GoalVerificationJob.goal_id == goal_id)
            .order_by(GoalVerificationJob.created_at.desc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()
