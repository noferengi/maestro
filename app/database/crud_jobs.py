"""
CRUD operations for background job tables.

ResearchJob        — scheduler-dispatched research agent investigations
                     (priority 0.0; lower = higher priority)
FileSummaryJob     — scheduler-dispatched file summary LLM calls
                     (priority -1.0; dispatched BEFORE research jobs in _tick())
OptimizationBenchmark — before/after profiling metrics for optimization sub-tasks

File summary jobs block the calling agent thread on a threading.Event
(completion registry in scheduler.py), so they get top priority to minimise
wait time.
"""

import logging
from datetime import datetime, timezone
from sqlalchemy import or_, and_

from .session import SessionLocal
from .models import ResearchJob, FileSummaryJob, OptimizationBenchmark, ArchGenJob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# ResearchJob CRUD
# ---------------------------------------------------------------------------

def create_research_job(task_id, question, context=None, priority=0.0, depth=0,
                        llm_id=None, budget_id=None, parent_job_id=None):
    """Create a new research job record."""
    db = SessionLocal()
    try:
        job = ResearchJob(
            task_id=task_id,
            question=question,
            context=context,
            priority=priority,
            depth=depth,
            llm_id=llm_id,
            budget_id=budget_id,
            parent_job_id=parent_job_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error creating research job: %s", e)
        return None
    finally:
        db.close()


def get_research_job(job_id):
    """Get a research job by ID."""
    db = SessionLocal()
    try:
        return db.query(ResearchJob).filter(ResearchJob.id == job_id).first()
    except Exception as e:
        logger.error("Error getting research job %s: %s", job_id, e)
        return None
    finally:
        db.close()


def get_pending_research_jobs(limit=10):
    """Return pending research jobs ordered by priority ASC, created_at ASC.
    Only returns jobs for active tasks.
    """
    from .models import Task
    db = SessionLocal()
    try:
        return (
            db.query(ResearchJob)
            .join(Task, ResearchJob.task_id == Task.id)
            .filter(ResearchJob.status == 'pending', Task.is_active == True)
            .order_by(ResearchJob.priority, ResearchJob.created_at)
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.error("Error getting pending research jobs: %s", e)
        return []
    finally:
        db.close()


def update_research_job(job_id, **kwargs):
    """Update a research job with provided fields."""
    db = SessionLocal()
    try:
        job = db.query(ResearchJob).filter(ResearchJob.id == job_id).first()
        if not job:
            return None
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        if kwargs.get('status') in ('completed', 'failed', 'cancelled'):
            job.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error updating research job %s: %s", job_id, e)
        return None
    finally:
        db.close()


def get_research_jobs_for_task(task_id):
    """Return all research jobs for a task, most recent first."""
    db = SessionLocal()
    try:
        return (
            db.query(ResearchJob)
            .filter(ResearchJob.task_id == task_id)
            .order_by(ResearchJob.created_at.desc())
            .all()
        )
    except Exception as e:
        logger.error("Error getting research jobs for task '%s': %s", task_id, e)
        return []
    finally:
        db.close()


def count_pending_research_jobs():
    """Return the number of pending research jobs for active tasks."""
    from .models import Task
    db = SessionLocal()
    try:
        return (
            db.query(ResearchJob)
            .join(Task, ResearchJob.task_id == Task.id)
            .filter(ResearchJob.status == 'pending', Task.is_active == True)
            .count()
        )
    except Exception as e:
        logger.error("Error counting pending research jobs: %s", e)
        return 0
    finally:
        db.close()


def get_retriable_research_jobs(
    failed_cooldown_seconds: float = 300.0,
    limit: int = 10,
) -> "list[ResearchJob]":
    """Return research jobs that should be retried.
    Only returns jobs for active tasks.

    Includes:
    - Jobs with status='failed' whose completed_at is older than failed_cooldown_seconds.
      (Caller resets these to 'pending'.)
    - Jobs with status='running' regardless of age.
      (Caller checks _active_sessions; orphaned ones get reset to 'pending'.)
    """
    from datetime import timedelta, timezone
    from .models import Task
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=failed_cooldown_seconds)
        return (
            db.query(ResearchJob)
            .join(Task, ResearchJob.task_id == Task.id)
            .filter(
                Task.is_active == True,
                or_(
                    and_(
                        ResearchJob.status == 'failed',
                        ResearchJob.completed_at < cutoff,
                    ),
                    ResearchJob.status == 'running',
                )
            )
            .order_by(ResearchJob.priority, ResearchJob.created_at)
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.error("Error getting retriable research jobs: %s", exc)
        return []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# FileSummaryJob CRUD
# ---------------------------------------------------------------------------

def create_file_summary_job(
    sha1: str,
    filesize: int,
    path: str,
    content: str,
    *,
    static_analysis_json: "str | None" = None,
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    task_id: "str | None" = None,
    priority: float = -1.0,
    previous_summary: "str | None" = None,
) -> "FileSummaryJob":
    """Insert a new pending file summary job."""
    db = SessionLocal()
    try:
        job = FileSummaryJob(
            sha1_hash=sha1,
            file_size_bytes=filesize,
            file_path=path,
            file_content=content,
            static_analysis_json=static_analysis_json,
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            priority=priority,
            previous_summary=previous_summary,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    finally:
        db.close()


def get_pending_file_summary_jobs(limit: int = 20) -> "list[FileSummaryJob]":
    """Return pending file summary jobs ordered by priority ASC, created_at ASC.
    Only returns jobs for active tasks (or jobs not linked to a task).
    """
    from .models import Task
    db = SessionLocal()
    try:
        return (
            db.query(FileSummaryJob)
            .outerjoin(Task, FileSummaryJob.task_id == Task.id)
            .filter(
                FileSummaryJob.status == 'pending',
                or_(FileSummaryJob.task_id == None, Task.is_active == True)
            )
            .order_by(FileSummaryJob.priority.asc(), FileSummaryJob.created_at.asc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def get_file_summary_job_by_sha1(sha1: str, filesize: int) -> "FileSummaryJob | None":
    """Find an existing pending or running job for deduplication."""
    db = SessionLocal()
    try:
        return (
            db.query(FileSummaryJob)
            .filter(
                FileSummaryJob.sha1_hash == sha1,
                FileSummaryJob.file_size_bytes == filesize,
                FileSummaryJob.status.in_(['pending', 'running']),
            )
            .first()
        )
    finally:
        db.close()


def update_file_summary_job(job_id: int, **kwargs) -> None:
    """Update fields on a file summary job; auto-sets completed_at on terminal status."""
    db = SessionLocal()
    try:
        job = db.query(FileSummaryJob).filter(FileSummaryJob.id == job_id).first()
        if not job:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        if kwargs.get('status') in ('completed', 'failed') and job.completed_at is None:
            job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error updating file_summary_job %d: %s", job_id, exc)
    finally:
        db.close()


def count_pending_file_summary_jobs() -> int:
    """Return the number of pending file summary jobs for active tasks."""
    from .models import Task
    db = SessionLocal()
    try:
        return (
            db.query(FileSummaryJob)
            .outerjoin(Task, FileSummaryJob.task_id == Task.id)
            .filter(
                FileSummaryJob.status == 'pending',
                or_(FileSummaryJob.task_id == None, Task.is_active == True)
            )
            .count()
        )
    except Exception as exc:
        logger.error("Error counting pending file summary jobs: %s", exc)
        return 0
    finally:
        db.close()


def get_retriable_file_summary_jobs(
    failed_cooldown_seconds: float = 300.0,
    limit: int = 20,
) -> "list[FileSummaryJob]":
    """Return file summary jobs that should be retried.
    Only returns jobs for active tasks (or jobs not linked to a task).

    Includes:
    - Jobs with status='failed' whose completed_at is older than failed_cooldown_seconds.
      (Caller resets these to 'pending'.)
    - Jobs with status='running' regardless of age.
      (Caller checks _active_sessions; orphaned ones get reset to 'pending'.)
    """
    from datetime import timedelta
    from .models import Task
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=failed_cooldown_seconds)
        return (
            db.query(FileSummaryJob)
            .outerjoin(Task, FileSummaryJob.task_id == Task.id)
            .filter(
                or_(FileSummaryJob.task_id == None, Task.is_active == True),
                or_(
                    and_(
                        FileSummaryJob.status == 'failed',
                        FileSummaryJob.completed_at < cutoff,
                    ),
                    FileSummaryJob.status == 'running',
                )
            )
            .order_by(FileSummaryJob.priority.asc(), FileSummaryJob.created_at.asc())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.error("Error getting retriable file summary jobs: %s", exc)
        return []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# OptimizationBenchmark CRUD
# ---------------------------------------------------------------------------

def create_optimization_benchmark(task_id, parent_task_id, benchmark_type, metrics):
    """Record a before/after benchmark for an optimization sub-task."""
    db = SessionLocal()
    try:
        bench = OptimizationBenchmark(
            task_id=task_id,
            parent_task_id=parent_task_id,
            benchmark_type=benchmark_type,
            metrics=metrics if isinstance(metrics, str) else __import__('json').dumps(metrics),
        )
        db.add(bench)
        db.commit()
        db.refresh(bench)
        return bench
    except Exception as e:
        db.rollback()
        logger.error("Error creating optimization benchmark: %s", e)
        return None
    finally:
        db.close()


def get_optimization_benchmarks(parent_task_id):
    """Return all benchmarks for a parent task, ordered by created_at."""
    db = SessionLocal()
    try:
        return (
            db.query(OptimizationBenchmark)
            .filter(OptimizationBenchmark.parent_task_id == parent_task_id)
            .order_by(OptimizationBenchmark.created_at)
            .all()
        )
    except Exception as e:
        logger.error("Error getting benchmarks for task '%s': %s", parent_task_id, e)
        return []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ArchGenJob CRUD
# ---------------------------------------------------------------------------

def create_arch_gen_job(
    project: str,
    category: str,
    *,
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    priority: float = 1.0,
) -> "ArchGenJob | None":
    """Insert a new pending arch gen job."""
    db = SessionLocal()
    try:
        job = ArchGenJob(
            project=project,
            category=category,
            llm_id=llm_id,
            budget_id=budget_id,
            priority=priority,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error creating arch_gen_job: %s", e)
        return None
    finally:
        db.close()


def get_pending_arch_gen_jobs(limit: int = 10) -> "list[ArchGenJob]":
    """Return pending arch gen jobs ordered by priority ASC, created_at ASC.
    Only returns jobs for projects that still exist in the registry.
    """
    from .models import Project
    db = SessionLocal()
    try:
        return (
            db.query(ArchGenJob)
            .join(Project, ArchGenJob.project == Project.name)
            .filter(ArchGenJob.status == 'pending')
            .order_by(ArchGenJob.priority.asc(), ArchGenJob.created_at.asc())
            .limit(limit)
            .all()
        )
    finally:
        db.close()


def update_arch_gen_job(job_id: int, **kwargs) -> None:
    """Update fields on an arch gen job; auto-sets completed_at on terminal status."""
    db = SessionLocal()
    try:
        job = db.query(ArchGenJob).filter(ArchGenJob.id == job_id).first()
        if not job:
            return
        for key, value in kwargs.items():
            setattr(job, key, value)
        if kwargs.get('status') in ('completed', 'failed') and job.completed_at is None:
            job.completed_at = datetime.utcnow()
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("Error updating arch_gen_job %d: %s", job_id, exc)
    finally:
        db.close()


def get_retriable_arch_gen_jobs(
    failed_cooldown_seconds: float = 300.0,
    limit: int = 10,
) -> "list[ArchGenJob]":
    """Return arch gen jobs that should be retried.
    Only returns jobs for projects that still exist in the registry.

    Includes:
    - Jobs with status='failed' whose completed_at is older than failed_cooldown_seconds.
    - Jobs with status='running' regardless of age (orphaned by a crash).
    """
    from datetime import timedelta
    from .models import Project
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=failed_cooldown_seconds)
        return (
            db.query(ArchGenJob)
            .join(Project, ArchGenJob.project == Project.name)
            .filter(
                or_(
                    and_(
                        ArchGenJob.status == 'failed',
                        ArchGenJob.completed_at < cutoff,
                    ),
                    ArchGenJob.status == 'running',
                )
            )
            .order_by(ArchGenJob.priority.asc(), ArchGenJob.created_at.asc())
            .limit(limit)
            .all()
        )
    except Exception as exc:
        logger.error("Error getting retriable arch_gen_jobs: %s", exc)
        return []
    finally:
        db.close()
