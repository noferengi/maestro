"""
CRUD operations for the Project table.

Projects map a name → filesystem path so the agent always operates on the
correct git repository.  get_project_path() is the primary lookup used by
agents and the scheduler.

upsert_project() uses Ellipsis (...) as a sentinel for llm_id / budget_id:
  - Pass Ellipsis (the default) to leave the existing value unchanged.
  - Pass None to explicitly clear the field.
  - Pass an int to set the field.
"""

import logging

from .session import SessionLocal
from .models import Project

logger = logging.getLogger(__name__)


def get_all_projects():
    """Return all projects ordered by name."""
    db = SessionLocal()
    try:
        return db.query(Project).order_by(Project.name).all()
    except Exception as e:
        logger.error("Error getting projects: %s", e)
        return []
    finally:
        db.close()


def get_project(name: str):
    """Return a single project by name, or None if not found."""
    db = SessionLocal()
    try:
        return db.query(Project).filter(Project.name == name).first()
    except Exception as e:
        logger.error("Error getting project '%s': %s", name, e)
        return None
    finally:
        db.close()


def get_project_path(project_name: str) -> "str | None":
    """
    Return the filesystem path for a project, or None if unknown.

    This is the primary helper used by the agent to resolve which git
    repository to operate on for a given task.
    """
    project = get_project(project_name)
    return project.path if project else None


def upsert_project(
    name: str,
    path: "str | None" = None,
    description: "str | None" = None,
    llm_id: "int | None" = ...,     # type: ignore[assignment]
    budget_id: "int | None" = ...,  # type: ignore[assignment]
) -> "Project | None":
    """
    Create or update a project.  ``path`` is the absolute filesystem root of
    the project's git repository.  Passing path=None leaves an existing path
    unchanged (use empty string to explicitly clear it).

    ``llm_id`` and ``budget_id`` follow the same sentinel pattern: the default
    value of ``...`` (Ellipsis) means "don't change the existing value".
    Pass an int or None explicitly to set/clear either field.
    """
    db = SessionLocal()
    try:
        existing = db.query(Project).filter(Project.name == name).first()
        if existing:
            if path is not None:
                existing.path = path or None
            if description is not None:
                existing.description = description
            if llm_id is not ...:
                existing.llm_id = llm_id
            if budget_id is not ...:
                existing.budget_id = budget_id
            db.commit()
            db.refresh(existing)
            return existing
        else:
            project = Project(
                name=name,
                path=path or None,
                description=description,
                llm_id=llm_id if llm_id is not ... else None,
                budget_id=budget_id if budget_id is not ... else None,
            )
            db.add(project)
            db.commit()
            db.refresh(project)
            return project
    except Exception as e:
        db.rollback()
        logger.error("Error upserting project '%s': %s", name, e)
        return None
    finally:
        db.close()


def delete_project(name: str) -> bool:
    """Delete a project record and cancel any associated arch_gen jobs."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == name).first()
        if not project:
            return False
        
        # Cancel any background arch_gen jobs for this project
        from .models import ArchGenJob
        from datetime import datetime, timezone
        (db.query(ArchGenJob)
           .filter(ArchGenJob.project_id == project.id,
                   ArchGenJob.status.in_(['pending', 'running']))
           .update({"status": "cancelled", "completed_at": datetime.now(timezone.utc)},
                   synchronize_session=False))

        db.delete(project)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting project '%s': %s", name, e)
        return False
    finally:
        db.close()
