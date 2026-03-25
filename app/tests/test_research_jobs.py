"""
Tests for research job CRUD, MaestroLoop NEEDS_RESEARCH handler, and scheduler dispatch.
"""

from __future__ import annotations

import json
import os
import pytest


# ---------------------------------------------------------------------------
# Research job CRUD
# ---------------------------------------------------------------------------

def test_create_and_get_research_job(tmp_path, monkeypatch):
    """create_research_job + get_research_job round-trip."""
    db_path = str(tmp_path / "test.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    # Re-import with patched env
    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)

    # Need at least the tables — create them
    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    # Create a minimal task row first (FK)
    from datetime import datetime
    task = db_mod.Task(
        id="task-test-1",
        title="Test",
        type="idea",
        project="TestProj",
        history=[],
    )
    session = db_mod.SessionLocal()
    session.add(task)
    session.commit()
    session.close()

    job = db_mod.create_research_job(
        task_id="task-test-1",
        question="What is the best sorting algorithm?",
        context=json.dumps({"notes": "perf matters"}),
        priority=5.0,
        depth=1,
    )
    assert job is not None
    assert job.id is not None
    assert job.status == "pending"
    assert job.question == "What is the best sorting algorithm?"
    assert job.priority == 5.0

    fetched = db_mod.get_research_job(job.id)
    assert fetched is not None
    assert fetched.id == job.id


def test_get_pending_research_jobs_ordering(tmp_path, monkeypatch):
    """Pending jobs are returned ordered by priority ASC, created_at ASC."""
    db_path = str(tmp_path / "test2.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    session = db_mod.SessionLocal()
    task = db_mod.Task(id="task-ord", title="T", type="idea", project="P", history=[])
    session.add(task)
    session.commit()
    session.close()

    db_mod.create_research_job(task_id="task-ord", question="q3", priority=10.0, depth=0)
    db_mod.create_research_job(task_id="task-ord", question="q1", priority=1.0, depth=0)
    db_mod.create_research_job(task_id="task-ord", question="q2", priority=5.0, depth=0)

    jobs = db_mod.get_pending_research_jobs(limit=10)
    priorities = [j.priority for j in jobs]
    assert priorities == sorted(priorities)


def test_update_research_job(tmp_path, monkeypatch):
    """update_research_job persists changes and sets completed_at on terminal status."""
    db_path = str(tmp_path / "test3.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    session = db_mod.SessionLocal()
    task = db_mod.Task(id="task-upd", title="T", type="idea", project="P", history=[])
    session.add(task)
    session.commit()
    session.close()

    job = db_mod.create_research_job(task_id="task-upd", question="q", priority=0.0)
    db_mod.update_research_job(job.id, status="running")
    updated = db_mod.get_research_job(job.id)
    assert updated.status == "running"
    assert updated.completed_at is None

    db_mod.update_research_job(job.id, status="completed", findings="Found it.")
    completed = db_mod.get_research_job(job.id)
    assert completed.status == "completed"
    assert completed.findings == "Found it."
    assert completed.completed_at is not None


def test_count_pending_research_jobs(tmp_path, monkeypatch):
    db_path = str(tmp_path / "test4.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    session = db_mod.SessionLocal()
    task = db_mod.Task(id="task-cnt", title="T", type="idea", project="P", history=[])
    session.add(task)
    session.commit()
    session.close()

    assert db_mod.count_pending_research_jobs() == 0

    db_mod.create_research_job(task_id="task-cnt", question="a", priority=0.0)
    db_mod.create_research_job(task_id="task-cnt", question="b", priority=0.0)
    assert db_mod.count_pending_research_jobs() == 2

    jobs = db_mod.get_pending_research_jobs()
    db_mod.update_research_job(jobs[0].id, status="completed")
    assert db_mod.count_pending_research_jobs() == 1


# ---------------------------------------------------------------------------
# MaestroLoop signal extraction
# ---------------------------------------------------------------------------

def test_extract_signal_accepts_needs_research():
    """_extract_signal must recognise the NEEDS_RESEARCH signal."""
    import sys
    sys.path.insert(0, str(os.path.join(os.path.dirname(__file__), "..", "..")))

    from app.agent.loop import MaestroLoop
    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "task-x"

    content = json.dumps({
        "signal": "NEEDS_RESEARCH",
        "task_id": "task-x",
        "question": "How does X work?",
        "context": "Some context",
    })
    parsed = loop._extract_signal(content)
    assert parsed is not None
    assert parsed["signal"] == "NEEDS_RESEARCH"


def test_extract_signal_accepts_accepted():
    from app.agent.loop import MaestroLoop
    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "task-x"

    content = json.dumps({"signal": "ACCEPTED", "task_id": "task-x", "summary": "done"})
    parsed = loop._extract_signal(content)
    assert parsed is not None
    assert parsed["signal"] == "ACCEPTED"


def test_extract_signal_ignores_unknown():
    from app.agent.loop import MaestroLoop
    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "task-x"

    content = json.dumps({"signal": "UNKNOWN_SIGNAL", "task_id": "task-x"})
    parsed = loop._extract_signal(content)
    assert parsed is None


# ---------------------------------------------------------------------------
# MaestroLoop _handle_needs_research (with mocked run_research)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handle_needs_research_success(tmp_path, monkeypatch):
    """_handle_needs_research creates a job, runs research, updates job, returns findings."""
    import unittest.mock as mock
    import importlib

    db_path = str(tmp_path / "loop_test.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import app.database as db_mod
    importlib.reload(db_mod)
    db_mod.Base.metadata.create_all(bind=db_mod.engine)

    session = db_mod.SessionLocal()
    task = db_mod.Task(id="task-loop", title="T", type="indev", project="P", history=[])
    session.add(task)
    session.commit()
    session.close()

    from app.agent.research import ResearchResult

    async def mock_run_research(**kwargs):
        return ResearchResult(
            vote={"verdict": "POSSIBLE", "confidence": 80},
            lives_used=1,
            total_turns=3,
            findings="The answer is 42.",
            prompt_tokens=100,
            completion_tokens=50,
        )

    from app.agent.loop import MaestroLoop
    loop = MaestroLoop.__new__(MaestroLoop)
    loop.task_id = "task-loop"
    loop.llm_id = None
    loop.budget_id = None
    loop.llm_base_url = "http://localhost:8008/v1"
    loop.llm_model = "test-model"

    signal_dict = {
        "signal": "NEEDS_RESEARCH",
        "task_id": "task-loop",
        "question": "What is the optimal cache size?",
        "context": "We are building a cache for X.",
    }

    with mock.patch("app.agent.research.run_research", side_effect=mock_run_research):
        result = await loop._handle_needs_research(signal_dict)

    assert result.get("findings") == "The answer is 42."
    assert result.get("verdict") == "POSSIBLE"

    # Verify a job was created and updated
    jobs = db_mod.get_research_jobs_for_task("task-loop")
    assert len(jobs) >= 1
    assert jobs[0].status == "completed"


# ---------------------------------------------------------------------------
# Research Jobs API routes
# ---------------------------------------------------------------------------

def _delete_research_task(task_id):
    """Clean up a test task and its research jobs from the shared DB."""
    from database import SessionLocal, Task
    try:
        from database import ResearchJob
    except ImportError:
        from app.database import ResearchJob

    db = SessionLocal()
    try:
        db.query(ResearchJob).filter(ResearchJob.task_id == task_id).delete(synchronize_session=False)
        db.query(Task).filter(Task.id == task_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_get_research_jobs_for_task_route():
    """GET /api/tasks/{task_id}/research-jobs returns jobs for a known task."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main
    from database import SessionLocal, Task, create_research_job

    task_id = "task-api-rj-list"
    _delete_research_task(task_id)

    db = SessionLocal()
    try:
        task = Task(id=task_id, title="API RJ Test", type="idea", project="TestProj", history=[])
        db.add(task)
        db.commit()
    finally:
        db.close()

    try:
        create_research_job(task_id=task_id, question="q1", priority=0.0)
        create_research_job(task_id=task_id, question="q2", priority=1.0)

        client = TestClient(main.app, raise_server_exceptions=True)
        response = client.get(f"/api/tasks/{task_id}/research-jobs")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        assert all("id" in item for item in data)
        assert all("status" in item for item in data)
    finally:
        _delete_research_task(task_id)


def test_get_research_jobs_for_missing_task_returns_404():
    """GET /api/tasks/{task_id}/research-jobs returns 404 for unknown task."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/api/tasks/nonexistent-rj-task-zzz/research-jobs")
    assert response.status_code == 404


def test_get_single_research_job_route():
    """GET /api/research-jobs/{job_id} returns a single job."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main
    from database import SessionLocal, Task, create_research_job

    task_id = "task-api-rj-single"
    _delete_research_task(task_id)

    db = SessionLocal()
    try:
        task = Task(id=task_id, title="API RJ Single", type="idea", project="TestProj", history=[])
        db.add(task)
        db.commit()
    finally:
        db.close()

    try:
        job = create_research_job(task_id=task_id, question="single?", priority=0.0)

        client = TestClient(main.app, raise_server_exceptions=True)
        response = client.get(f"/api/research-jobs/{job.id}")
        assert response.status_code == 200
        data = response.json()
        assert data["id"] == job.id
        assert data["question"] == "single?"
        assert data["status"] == "pending"
    finally:
        _delete_research_task(task_id)


def test_get_single_research_job_not_found():
    """GET /api/research-jobs/{job_id} returns 404 for unknown ID."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/api/research-jobs/9999999")
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Benchmarks API routes
# ---------------------------------------------------------------------------

def _delete_benchmark_tasks(*task_ids):
    """Clean up test tasks and their benchmark records from the shared DB."""
    try:
        from database import SessionLocal, Task, OptimizationBenchmark
    except ImportError:
        from app.database import SessionLocal, Task, OptimizationBenchmark

    db = SessionLocal()
    try:
        for task_id in task_ids:
            db.query(OptimizationBenchmark).filter(
                (OptimizationBenchmark.task_id == task_id) |
                (OptimizationBenchmark.parent_task_id == task_id)
            ).delete(synchronize_session=False)
        for task_id in task_ids:
            db.query(Task).filter(Task.id == task_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def test_get_benchmarks_for_task_route():
    """GET /api/tasks/{task_id}/benchmarks returns benchmark records for a known parent task."""
    import os, sys, json
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main
    from database import SessionLocal, Task, create_optimization_benchmark

    parent_id = "task-api-bm-parent"
    child_id = "task-api-bm-child"
    _delete_benchmark_tasks(parent_id, child_id)

    db = SessionLocal()
    try:
        db.add(Task(id=parent_id, title="BM Parent", type="optimization", project="TestProj", history=[]))
        db.add(Task(id=child_id, title="BM Child", type="idea", project="TestProj", history=[]))
        db.commit()
    finally:
        db.close()

    try:
        metrics_before = json.dumps({"test_duration_ms": 200.0, "memory_peak_mb": 50.0, "complexity_score": 40})
        metrics_after  = json.dumps({"test_duration_ms": 120.0, "memory_peak_mb": 45.0, "complexity_score": 35, "big_o_class": "O(n)"})
        create_optimization_benchmark(child_id, parent_id, "before", metrics_before)
        create_optimization_benchmark(child_id, parent_id, "after",  metrics_after)

        client = TestClient(main.app, raise_server_exceptions=True)
        response = client.get(f"/api/tasks/{parent_id}/benchmarks")
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data, list)
        assert len(data) == 2
        types = {r["benchmark_type"] for r in data}
        assert types == {"before", "after"}
        for record in data:
            assert record["parent_task_id"] == parent_id
            assert record["task_id"] == child_id
            assert "metrics" in record
            assert "created_at" in record
    finally:
        _delete_benchmark_tasks(parent_id, child_id)


def test_get_benchmarks_empty_for_task_without_records():
    """GET /api/tasks/{task_id}/benchmarks returns [] when no benchmarks exist."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main
    from database import SessionLocal, Task

    task_id = "task-api-bm-empty"
    _delete_benchmark_tasks(task_id)

    db = SessionLocal()
    try:
        db.add(Task(id=task_id, title="BM Empty", type="optimization", project="TestProj", history=[]))
        db.commit()
    finally:
        db.close()

    try:
        client = TestClient(main.app, raise_server_exceptions=True)
        response = client.get(f"/api/tasks/{task_id}/benchmarks")
        assert response.status_code == 200
        assert response.json() == []
    finally:
        _delete_benchmark_tasks(task_id)


def test_get_benchmarks_for_missing_task_returns_404():
    """GET /api/tasks/{task_id}/benchmarks returns 404 for unknown task."""
    import os, sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
    from starlette.testclient import TestClient
    import main

    client = TestClient(main.app, raise_server_exceptions=False)
    response = client.get("/api/tasks/nonexistent-bm-task-zzz/benchmarks")
    assert response.status_code == 404
