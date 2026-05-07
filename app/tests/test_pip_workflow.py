"""
Integration tests for the PIP workflow.

Tests the end-to-end data flow using the real test.db (managed by conftest.py),
with LLM calls and subprocess calls mocked out.

The workflow tested (condensed from plan §15):
  1. Create a task → PIP created via CRUD
  2. PIP requirements attach to task; API returns pips array
  3. Pre-flight gate: all passed → stage proceeds
  4. Pre-flight gate: one failed → pip_resolution_job created
  5. Resolution job status lifecycle (pending → researching → resolving → done)
  6. Stage re-dispatch after resolution: pre-flight passes this time
  7. COMPLETED task still exposes PIPs in its task dict
"""
import asyncio
import json
import uuid

import pytest

from app.database import (
    create_task,
    get_task,
    update_task,
    create_pip,
    get_pips_for_task,
    create_pip_verification,
    get_latest_pip_verification,
    get_pip_verification_map,
    pip_status_at_stage,
    create_pip_resolution_job,
    get_pending_pip_resolution_jobs,
    get_active_pip_resolution_jobs_for_task,
    update_pip_resolution_job,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _uid():
    """Short unique suffix for test isolation."""
    return uuid.uuid4().hex[:8]


def _make_task(stage="indev"):
    """Create a real task row in the test DB and return its id."""
    task = create_task(
        title=f"PIP workflow test task {_uid()}",
        task_type=stage,
        description="Testing PIPs end-to-end.",
        project="TheMaestro",
    )
    assert task is not None
    return task.id


def _make_pip(task_id, stage="security", commit="abc123def"):
    """Insert a PIP row and return the ORM object."""
    pip = create_pip(
        task_id=task_id,
        origin_stage=stage,
        requirements=json.dumps([
            "Ensure all error paths log at ERROR level.",
            "Add unit tests for the empty-payload edge case.",
        ]),
        created_at_commit=commit,
    )
    assert pip is not None
    return pip


# ---------------------------------------------------------------------------
# 1. PIP created and attached to task
# ---------------------------------------------------------------------------

class TestPipCreation:
    def test_create_pip_attaches_to_task(self):
        task_id = _make_task()
        pip = _make_pip(task_id)

        pips = get_pips_for_task(task_id)
        assert len(pips) == 1
        assert pips[0].id == pip.id
        assert pips[0].task_id == task_id
        assert pips[0].origin_stage == "security"
        assert pips[0].created_at_commit == "abc123def"

    def test_multiple_pips_accumulate(self):
        task_id = _make_task()
        _make_pip(task_id, stage="security")
        _make_pip(task_id, stage="optimization")

        pips = get_pips_for_task(task_id)
        assert len(pips) == 2
        stages = {p.origin_stage for p in pips}
        assert "security" in stages
        assert "optimization" in stages

    def test_pip_requirements_round_trip(self):
        task_id = _make_task()
        reqs = ["Add logging.", "Write tests."]
        pip = create_pip(
            task_id=task_id,
            origin_stage="final_review",
            requirements=json.dumps(reqs),
        )
        pips = get_pips_for_task(task_id)
        stored = json.loads(pips[0].requirements)
        assert stored == reqs

    def test_pip_default_commit_is_none(self):
        task_id = _make_task()
        pip = create_pip(
            task_id=task_id,
            origin_stage="security",
            requirements=json.dumps(["req"]),
        )
        assert pip.created_at_commit == "none"


# ---------------------------------------------------------------------------
# 2. PIP status derivation
# ---------------------------------------------------------------------------

class TestPipStatusDerivation:
    def test_unverified_when_no_verification_row(self):
        task_id = _make_task()
        pip = _make_pip(task_id)
        assert pip_status_at_stage(pip.id, "conceptual_review") == "unverified"

    def test_satisfied_after_passed_verification(self):
        task_id = _make_task()
        pip = _make_pip(task_id)
        create_pip_verification(
            pip_id=pip.id,
            task_id=task_id,
            stage="conceptual_review",
            outcome="passed",
            summary="All requirements satisfied.",
        )
        assert pip_status_at_stage(pip.id, "conceptual_review") == "satisfied"

    def test_unsatisfied_after_failed_verification(self):
        task_id = _make_task()
        pip = _make_pip(task_id)
        create_pip_verification(
            pip_id=pip.id,
            task_id=task_id,
            stage="optimization",
            outcome="failed",
            summary="Tests still missing.",
        )
        assert pip_status_at_stage(pip.id, "optimization") == "unsatisfied"

    def test_status_per_stage_is_independent(self):
        """A pip satisfied at conceptual_review is unverified at optimization."""
        task_id = _make_task()
        pip = _make_pip(task_id)
        create_pip_verification(
            pip_id=pip.id,
            task_id=task_id,
            stage="conceptual_review",
            outcome="passed",
            summary="OK at conceptual_review.",
        )
        assert pip_status_at_stage(pip.id, "conceptual_review") == "satisfied"
        assert pip_status_at_stage(pip.id, "optimization") == "unverified"

    def test_latest_verification_wins(self):
        """The most recent verification row determines the displayed status."""
        task_id = _make_task()
        pip = _make_pip(task_id)
        create_pip_verification(
            pip_id=pip.id,
            task_id=task_id,
            stage="security",
            outcome="failed",
            summary="First run: missing tests.",
        )
        create_pip_verification(
            pip_id=pip.id,
            task_id=task_id,
            stage="security",
            outcome="passed",
            summary="Second run: all clear.",
        )
        assert pip_status_at_stage(pip.id, "security") == "satisfied"


# ---------------------------------------------------------------------------
# 3. Pre-flight gate — all passed
# ---------------------------------------------------------------------------

class TestPreflightGate:
    def test_preflight_all_passed_via_crud(self):
        """
        Simulate a pre-flight run where both PIPs pass, then check the
        resulting verification map.
        """
        task_id = _make_task()
        pip1 = _make_pip(task_id, stage="security")
        pip2 = _make_pip(task_id, stage="optimization")

        stage = "conceptual_review"
        for pip in [pip1, pip2]:
            create_pip_verification(
                pip_id=pip.id,
                task_id=task_id,
                stage=stage,
                outcome="passed",
                summary="Requirement satisfied.",
            )

        v_map = get_pip_verification_map(task_id, stage)
        assert v_map[pip1.id] == "passed"
        assert v_map[pip2.id] == "passed"
        # All passed ↔ no resolution jobs needed
        assert not get_active_pip_resolution_jobs_for_task(task_id)

    def test_preflight_one_failed_creates_resolution_job(self):
        """
        When one PIP fails pre-flight, a pip_resolution_job row must be
        created and picked up by get_pending_pip_resolution_jobs().
        """
        task_id = _make_task()
        pip_pass = _make_pip(task_id, stage="security")
        pip_fail = _make_pip(task_id, stage="optimization")

        stage = "conceptual_review"
        create_pip_verification(
            pip_id=pip_pass.id,
            task_id=task_id,
            stage=stage,
            outcome="passed",
            summary="OK.",
        )
        create_pip_verification(
            pip_id=pip_fail.id,
            task_id=task_id,
            stage=stage,
            outcome="failed",
            summary="Tests still missing.",
            findings=json.dumps([
                {"requirement": "Add unit tests.", "status": "missing", "detail": "no test file found"},
            ]),
        )

        job = create_pip_resolution_job(task_id, pip_fail.id, stage)
        assert job is not None
        assert job.task_id == task_id
        assert job.pip_id == pip_fail.id
        assert job.stage_blocked_at == stage
        assert job.status == "pending"

        active = get_active_pip_resolution_jobs_for_task(task_id)
        assert len(active) == 1
        assert active[0].pip_id == pip_fail.id

        # Clean up — leave no active jobs in the shared test DB so the scheduler
        # routing tests don't pick up extra work during their _tick() calls.
        update_pip_resolution_job(job.id, status="done")


# ---------------------------------------------------------------------------
# 4. Resolution job lifecycle
# ---------------------------------------------------------------------------

class TestResolutionJobLifecycle:
    def test_job_status_transitions(self):
        task_id = _make_task()
        pip = _make_pip(task_id)

        job = create_pip_resolution_job(task_id, pip.id, "optimization")
        assert job.status == "pending"

        # pending → researching: still in the active queue
        update_pip_resolution_job(job.id, status="researching")
        queue = get_pending_pip_resolution_jobs(limit=50)
        our_jobs = [j for j in queue if j.id == job.id]
        assert len(our_jobs) == 1

        # researching → resolving: also still in active queue (statuses: pending|researching|resolving)
        update_pip_resolution_job(job.id, status="resolving")
        update_pip_resolution_job(job.id, research_findings="Found: tests/test_auth.py is empty.")
        queue2 = get_pending_pip_resolution_jobs(limit=50)
        still_active = [j for j in queue2 if j.id == job.id]
        assert len(still_active) == 1

        # resolving → done: removed from active queue
        update_pip_resolution_job(job.id, status="done")
        queue3 = get_pending_pip_resolution_jobs(limit=50)
        still_active2 = [j for j in queue3 if j.id == job.id]
        assert not still_active2

    def test_duplicate_job_is_idempotent(self):
        """Creating a second job for the same pip while one is active returns the existing job."""
        task_id = _make_task()
        pip = _make_pip(task_id)

        job1 = create_pip_resolution_job(task_id, pip.id, "security")
        assert job1 is not None

        job2 = create_pip_resolution_job(task_id, pip.id, "security")
        # Returns the existing active job rather than creating a duplicate
        assert job2 is not None
        assert job2.id == job1.id

        # Clean up — don't leave active jobs in the shared test DB.
        update_pip_resolution_job(job1.id, status="done")

    def test_done_job_allows_new_job(self):
        """After a job reaches 'done', a new job for the same pip is allowed."""
        task_id = _make_task()
        pip = _make_pip(task_id)

        job1 = create_pip_resolution_job(task_id, pip.id, "final_review")
        update_pip_resolution_job(job1.id, status="done")

        job2 = create_pip_resolution_job(task_id, pip.id, "final_review")
        assert job2 is not None
        assert job2.status == "pending"

        # Clean up — don't leave active jobs in the shared test DB.
        update_pip_resolution_job(job2.id, status="done")


# ---------------------------------------------------------------------------
# 5. COMPLETED task still exposes PIPs
# ---------------------------------------------------------------------------

class TestCompletedTaskRetainsPips:
    def test_pips_visible_after_task_completed(self):
        """PIPs are never removed — even when the task reaches COMPLETED."""
        task_id = _make_task(stage="indev")
        pip1 = _make_pip(task_id, stage="security")
        pip2 = _make_pip(task_id, stage="optimization")

        # Verify both PIPs, then advance task to completed
        for pip in [pip1, pip2]:
            create_pip_verification(
                pip_id=pip.id,
                task_id=task_id,
                stage="security",
                outcome="passed",
                summary="Resolved.",
            )
        update_task(task_id, type="completed")

        task = get_task(task_id)
        assert task.type == "completed"

        pips = get_pips_for_task(task_id)
        assert len(pips) == 2

        # Both still show as satisfied at the security stage
        for pip in pips:
            assert pip_status_at_stage(pip.id, "security") == "satisfied"


# ---------------------------------------------------------------------------
# 6. Pre-flight via run_pip_preflight (integration: real DB + mocked LLM)
# ---------------------------------------------------------------------------

def _run(coro):
    """Run a coroutine in a fresh event loop — safe to call from sync test methods."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()
        asyncio.set_event_loop(None)


class TestRunPipPreflight:
    def test_preflight_all_passed_writes_verification_rows(self):
        """
        run_pip_preflight() with a passing mock LLM must:
        - return all_passed=True
        - persist one verification row per PIP
        """
        from unittest.mock import AsyncMock, patch

        from app.agent.pip_agent import run_pip_preflight

        task_id = _make_task(stage="indev")
        pip = _make_pip(task_id, stage="security", commit="none")

        passed_response = json.dumps({
            "outcome": "passed",
            "summary": "All requirements satisfied.",
            "findings": [{"requirement": "req", "status": "satisfied", "detail": "done"}],
        })

        with patch("app.agent.pip_agent.call_llm",
                   new_callable=AsyncMock,
                   return_value={
                       "choices": [{"message": {"content": passed_response, "tool_calls": None}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                   }), \
             patch("app.agent.pip_agent.build_project_snapshot", return_value="(snap)"):
            result = _run(run_pip_preflight(task_id, "conceptual_review", 1, 1, None))

        assert result["all_passed"] is True
        assert len(result["results"]) == 1
        assert result["results"][0]["outcome"] == "passed"

        # Verification row was persisted
        v = get_latest_pip_verification(pip.id, "conceptual_review")
        assert v is not None
        assert v.outcome == "passed"

    def test_preflight_failed_result_stored(self):
        """
        run_pip_preflight() with a failing mock LLM must:
        - return all_passed=False
        - persist a failed verification row
        """
        from unittest.mock import AsyncMock, patch

        from app.agent.pip_agent import run_pip_preflight

        task_id = _make_task(stage="indev")
        pip = _make_pip(task_id, stage="optimization", commit="none")

        failed_response = json.dumps({
            "outcome": "failed",
            "summary": "Tests still missing.",
            "findings": [{"requirement": "Write tests.", "status": "missing", "detail": "no tests"}],
        })

        with patch("app.agent.pip_agent.call_llm",
                   new_callable=AsyncMock,
                   return_value={
                       "choices": [{"message": {"content": failed_response, "tool_calls": None}}],
                       "usage": {"prompt_tokens": 10, "completion_tokens": 5},
                   }), \
             patch("app.agent.pip_agent.build_project_snapshot", return_value="(snap)"):
            result = _run(run_pip_preflight(task_id, "optimization", 1, 1, None))

        assert result["all_passed"] is False
        v = get_latest_pip_verification(pip.id, "optimization")
        assert v is not None
        assert v.outcome == "failed"
