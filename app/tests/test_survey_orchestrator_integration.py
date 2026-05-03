"""
app/tests/test_survey_orchestrator_integration.py
--------------------------------------------------
Integration tests for SurveyOrchestrator worker logic in scheduler.
"""

import pytest
import os
import asyncio
import json
from unittest.mock import MagicMock, patch, AsyncMock
from app.agent.scheduler import _run_scope_survey_job
from app.database import upsert_scope_summary, get_scope_summary, enqueue_scope_survey_job

class Namespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)

@pytest.fixture
def mock_llm():
    return Namespace(
        id=1,
        name="TestLLM",
        model="test-model",
        address="localhost",
        port=8008,
        max_context=10000
    )

@pytest.fixture
def mock_job():
    return Namespace(
        id=100,
        project_name="TestProj",
        scope_type="directory",
        scope_key="app/agent",
        action="generate",
        priority=1.0,
        budget_id=1,
        llm_id=1,
        status="running"
    )

def test_run_scope_survey_job_directory_success(mock_job, mock_llm, tmp_path):
    """Test successful directory summary generation."""
    project_root = str(tmp_path)

    # Setup some file summaries in DB
    from app.database.models import FileSummary
    from app.database.session import SessionLocal
    with SessionLocal() as db:
        f1 = FileSummary(
            sha1_hash="abc",
            file_size_bytes=100,
            file_path=os.path.join(project_root, "app/agent/orchestrator.py"),
            summary="Orchestrates everything.",
            short_summary="Orchestrator logic."
        )
        db.add(f1)
        db.commit()

    # Mock dependencies
    with patch("app.database.get_project_path", return_value=project_root), \
         patch("app.agent.llm_client.call_llm", new_callable=AsyncMock) as mock_call, \
         patch("app.database.update_scope_survey_job") as mock_update_job, \
         patch("app.database.create_agent_session", return_value=1), \
         patch("app.database.close_agent_session"):

        mock_call.return_value = {
            "message": {
                "content": "Full summary text.\nSHORT_SUMMARY: Short summary text.",
            },
            "prompt_tokens": 0,
            "completion_tokens": 0,
        }

        _run_scope_survey_job(mock_job, mock_llm)

        # Verify LLM was called with file summary
        args, kwargs = mock_call.call_args
        prompt = kwargs["messages"][0]["content"]
        assert "Orchestrator logic." in prompt
        assert "app/agent/orchestrator.py" in prompt

        # Verify scope summary was saved
        ss = get_scope_summary("TestProj", "directory", "app/agent")
        assert ss is not None
        assert ss.summary == "Full summary text."
        assert ss.short_summary == "Short summary text."

        from unittest.mock import ANY
        # Verify job marked as done (it also passes prompt/completion tokens)
        mock_update_job.assert_any_call(mock_job.id, status="done", prompt_tokens=ANY, completion_tokens=ANY)

def test_run_scope_survey_job_partitioning(mock_job, mock_llm, tmp_path):
    """Test that a large directory is partitioned into pages."""
    project_root = str(tmp_path)
    mock_llm.max_context = 2000 # Small context to force partitioning
    # branching factor = max(3, int(1/0.1) - 2) = 8.

    # Create 15 file summaries
    from app.database.models import FileSummary
    from app.database.session import SessionLocal
    with SessionLocal() as db:
        for i in range(15):
            f = FileSummary(
                sha1_hash=f"sha{i}",
                file_size_bytes=100,
                file_path=os.path.join(project_root, f"app/agent/file{i}.py"),
                summary=f"Summary {i}",
                short_summary=f"Short {i}"
            )
            db.add(f)
        db.commit()

    with patch("app.database.get_project_path", return_value=project_root), \
         patch("app.database.enqueue_scope_survey_job") as mock_enqueue, \
         patch("app.database.update_scope_survey_job") as mock_update_job, \
         patch("app.database.create_agent_session", return_value=1), \
         patch("app.database.close_agent_session"):

        _run_scope_survey_job(mock_job, mock_llm)

        # Should have enqueued 2 page jobs (15 files / 8 branching factor)
        assert mock_enqueue.call_count == 2

        # enqueue_scope_survey_job(project_name, scope_type, scope_key, action='generate', ...)
        call0 = mock_enqueue.call_args_list[0]
        args, kwargs = call0
        assert args[1] == "directory_page"
        assert args[2] == "app/agent:page-1"

        # Verify parent job marked as pending
        mock_update_job.assert_any_call(mock_job.id, status="pending", error_message="Waiting for 2 pages")
