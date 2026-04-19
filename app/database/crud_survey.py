"""
app/database/crud_survey.py
--------------------------
CRUD operations for scope_summaries and scope_survey_jobs.
"""

import json
from datetime import datetime
from sqlalchemy import desc
from app.database.session import SessionLocal
from app.database.models import ScopeSummary, ScopeSurveyJob

def upsert_scope_summary(
    project_name: str,
    scope_type: str,
    scope_key: str,
    summary: str,
    short_summary: str = None,
    parent_scope_key: str = None,
    depth: int = 0,
    file_paths: list[str] = None,
    file_count: int = 0,
    content_hash: str = None,
    git_commit: str = None,
    staleness_state: str = "fresh",
    llm_id: int = None,
    budget_id: int = None,
) -> ScopeSummary:
    with SessionLocal() as db:
        record = db.query(ScopeSummary).filter(
            ScopeSummary.project_name == project_name,
            ScopeSummary.scope_type == scope_type,
            ScopeSummary.scope_key == scope_key,
        ).first()

        if not record:
            record = ScopeSummary(
                project_name=project_name,
                scope_type=scope_type,
                scope_key=scope_key,
            )
            db.add(record)

        record.summary = summary
        record.short_summary = short_summary
        record.parent_scope_key = parent_scope_key
        record.depth = depth
        record.file_paths = json.dumps(file_paths) if file_paths is not None else None
        record.file_count = file_count
        record.content_hash = content_hash
        record.git_commit = git_commit
        record.staleness_state = staleness_state
        record.llm_id = llm_id
        record.budget_id = budget_id
        record.updated_at = datetime.utcnow()

        db.commit()
        db.refresh(record)
        return record

def get_scope_summary(project_name: str, scope_type: str, scope_key: str) -> ScopeSummary | None:
    with SessionLocal() as db:
        return db.query(ScopeSummary).filter(
            ScopeSummary.project_name == project_name,
            ScopeSummary.scope_type == scope_type,
            ScopeSummary.scope_key == scope_key,
        ).first()

def list_scope_summaries(project_name: str, scope_type: str = None) -> list[ScopeSummary]:
    with SessionLocal() as db:
        query = db.query(ScopeSummary).filter(ScopeSummary.project_name == project_name)
        if scope_type:
            query = query.filter(ScopeSummary.scope_type == scope_type)
        return query.order_by(ScopeSummary.depth, ScopeSummary.scope_key).all()

def mark_scope_stale(project_name: str, scope_type: str, scope_key: str):
    with SessionLocal() as db:
        record = db.query(ScopeSummary).filter(
            ScopeSummary.project_name == project_name,
            ScopeSummary.scope_type == scope_type,
            ScopeSummary.scope_key == scope_key,
        ).first()
        if record:
            record.staleness_state = "stale"
            record.updated_at = datetime.utcnow()
            db.commit()

def enqueue_scope_survey_job(
    project_name: str,
    scope_type: str,
    scope_key: str,
    action: str = "generate",
    priority: float = 0.0,
    llm_id: int = None,
    budget_id: int = None,
) -> ScopeSurveyJob:
    with SessionLocal() as db:
        # If an identical job is already pending or running, leave it alone.
        # If it previously failed, reset it to pending for retry rather than
        # creating a duplicate row — this prevents the partitioning spin loop
        # from accumulating rows each time the LLM is unreachable.
        existing = db.query(ScopeSurveyJob).filter(
            ScopeSurveyJob.project_name == project_name,
            ScopeSurveyJob.scope_type == scope_type,
            ScopeSurveyJob.scope_key == scope_key,
            ScopeSurveyJob.action == action,
            ScopeSurveyJob.status.in_(["pending", "running", "failed"]),
        ).first()
        if existing:
            if existing.status == "failed":
                existing.status = "pending"
                existing.error_message = None
                existing.retry_count = existing.retry_count + 1
                db.commit()
                db.refresh(existing)
            return existing

        job = ScopeSurveyJob(
            project_name=project_name,
            scope_type=scope_type,
            scope_key=scope_key,
            action=action,
            priority=priority,
            llm_id=llm_id,
            budget_id=budget_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job


def get_scope_survey_page_jobs(
    project_name: str,
    page_scope_type: str,
    parent_scope_key: str,
) -> list[ScopeSurveyJob]:
    """Return all page jobs for a partitioned parent scope (any status).

    Page job keys follow the pattern ``{parent_scope_key}:page-N``.
    Used by the scheduler to detect whether page jobs already exist before
    re-partitioning a large directory.
    """
    prefix = f"{parent_scope_key}:page-%"
    with SessionLocal() as db:
        return db.query(ScopeSurveyJob).filter(
            ScopeSurveyJob.project_name == project_name,
            ScopeSurveyJob.scope_type == page_scope_type,
            ScopeSurveyJob.scope_key.like(prefix),
        ).all()

def get_pending_scope_survey_jobs(limit: int = 10) -> list[ScopeSurveyJob]:
    with SessionLocal() as db:
        return db.query(ScopeSurveyJob).filter(
            ScopeSurveyJob.status == "pending"
        ).order_by(ScopeSurveyJob.priority, ScopeSurveyJob.created_at).limit(limit).all()

def update_scope_survey_job(
    job_id: int,
    status: str = None,
    prompt_tokens: int = None,
    completion_tokens: int = None,
    error_message: str = None,
    retry_count: int = None,
):
    with SessionLocal() as db:
        job = db.query(ScopeSurveyJob).filter(ScopeSurveyJob.id == job_id).first()
        if not job:
            return
        if status:
            job.status = status
            if status in ["done", "failed"]:
                job.completed_at = datetime.utcnow()
        if prompt_tokens is not None:
            job.prompt_tokens = prompt_tokens
        if completion_tokens is not None:
            job.completion_tokens = completion_tokens
        if error_message is not None:
            job.error_message = error_message
        if retry_count is not None:
            job.retry_count = retry_count
        db.commit()
