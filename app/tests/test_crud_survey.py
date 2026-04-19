"""
app/tests/test_crud_survey.py
----------------------------
CRUD tests for scope_summaries and scope_survey_jobs.
"""

import pytest
from datetime import datetime
from app.database import (
    upsert_scope_summary, get_scope_summary, list_scope_summaries, mark_scope_stale,
    enqueue_scope_survey_job, get_pending_scope_survey_jobs, update_scope_survey_job
)

def test_upsert_scope_summary_basic():
    """Verify we can create and update a scope summary."""
    project = "TestProj"
    s_type = "directory"
    key = "app/agent"
    summary_text = "Core agent logic."
    short_summary_text = "Agent logic."

    ss = upsert_scope_summary(
        project_name=project,
        scope_type=s_type,
        scope_key=key,
        summary=summary_text,
        short_summary=short_summary_text,
        file_count=10,
        staleness_state="fresh"
    )

    assert ss.id is not None
    assert ss.project_name == project
    assert ss.scope_type == s_type
    assert ss.scope_key == key
    assert ss.summary == summary_text
    assert ss.short_summary == short_summary_text
    assert ss.file_count == 10
    assert ss.staleness_state == "fresh"

    # Update
    updated_summary = "Revised agent logic."
    ss2 = upsert_scope_summary(
        project_name=project,
        scope_type=s_type,
        scope_key=key,
        summary=updated_summary,
        short_summary=short_summary_text,
        file_count=12
    )

    assert ss2.id == ss.id
    assert ss2.summary == updated_summary
    assert ss2.file_count == 12

def test_get_and_list_scope_summaries():
    """Verify retrieval and listing of scope summaries."""
    project = "ListProj"
    upsert_scope_summary(project, "directory", "dir1", "sum1")
    upsert_scope_summary(project, "directory", "dir2", "sum2")
    upsert_scope_summary(project, "module", "mod1", "sum3")
    upsert_scope_summary("OtherProj", "directory", "dir1", "other")

    # Get single
    ss = get_scope_summary(project, "directory", "dir1")
    assert ss is not None
    assert ss.summary == "sum1"

    # List all for project
    all_ss = list_scope_summaries(project)
    assert len(all_ss) == 3

    # List filtered by type
    dirs = list_scope_summaries(project, scope_type="directory")
    assert len(dirs) == 2
    assert all(d.scope_type == "directory" for d in dirs)

def test_mark_scope_stale():
    """Verify we can mark a scope as stale."""
    project = "StaleProj"
    upsert_scope_summary(project, "directory", "dir1", "sum1", staleness_state="fresh")
    
    mark_scope_stale(project, "directory", "dir1")
    
    ss = get_scope_summary(project, "directory", "dir1")
    assert ss.staleness_state == "stale"

def test_enqueue_scope_survey_job_deduplication():
    """Verify that redundant jobs are not enqueued."""
    project = "JobProj"
    s_type = "directory"
    key = "app"
    
    job1 = enqueue_scope_survey_job(project, s_type, key, action="generate", priority=1.0)
    assert job1.status == "pending"
    assert job1.priority == 1.0

    # Redundant job should return the same record
    job2 = enqueue_scope_survey_job(project, s_type, key, action="generate", priority=2.0)
    assert job2.id == job1.id
    assert job2.priority == 1.0  # priority of the original job

def test_get_and_update_survey_jobs():
    """Verify lifecycle of survey jobs."""
    project = "LifecycleProj"
    job1 = enqueue_scope_survey_job(project, "directory", "dir1", priority=1.0)
    job2 = enqueue_scope_survey_job(project, "directory", "dir2", priority=0.5)

    pending = [j for j in get_pending_scope_survey_jobs(limit=10) if j.project_name == project]
    assert len(pending) == 2
    # Check priority sorting (0.5 should come before 1.0)
    assert pending[0].id == job2.id
    assert pending[1].id == job1.id

    # Update job status
    update_scope_survey_job(job2.id, status="running")
    pending = [j for j in get_pending_scope_survey_jobs(limit=10) if j.project_name == project]
    assert len(pending) == 1
    assert pending[0].id == job1.id

    # Finish job
    update_scope_survey_job(job2.id, status="done", prompt_tokens=100, completion_tokens=50)
    
    from app.database.session import SessionLocal
    from app.database.models import ScopeSurveyJob
    with SessionLocal() as db:
        j = db.query(ScopeSurveyJob).filter(ScopeSurveyJob.id == job2.id).first()
        assert j.status == "done"
        assert j.prompt_tokens == 100
        assert j.completion_tokens == 50
        assert j.completed_at is not None
