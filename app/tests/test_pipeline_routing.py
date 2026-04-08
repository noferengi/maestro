"""
Tests for the 9-stage pipeline routing layer.

Covers:
  1. ADVANCE_HANDLERS map completeness
  2. /api/tasks/{id}/advance endpoint - 404, 422 validation, 200 happy path
  3. Scheduler _tick() - only auto-dispatches planning / indev
  4. Direct column transition: _advance_to_optimization (conceptual_review -> optimization)
  5. Mock LLM intake pipeline: pass, reject, subdivide, needs_research, tie scenarios
"""

import asyncio
import json
import os
import sys
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


# ---------------------------------------------------------------------------
# Shared DB helpers
# ---------------------------------------------------------------------------

def _delete_task(task_id):
    from database import (
        SessionLocal, Task, TransitionVote, TransitionResult, BudgetEntry,
        SubdivisionRecord, PlanningResult, ComponentResult,
        OptimizationResult, SecurityReviewResult, FullReviewResult,
        MergeRecord,
    )
    db = SessionLocal()
    try:
        # Delete all child records that FK-reference this task before deleting
        # the task itself (required now that PRAGMA foreign_keys=ON is set).
        # Models with task_id FK column:
        for model in (
            TransitionVote, TransitionResult, BudgetEntry,
            PlanningResult, ComponentResult, OptimizationResult,
            SecurityReviewResult, FullReviewResult, MergeRecord,
        ):
            db.query(model).filter(model.task_id == task_id).delete(synchronize_session=False)
        # SubdivisionRecord uses parent_task_id, not task_id.
        db.query(SubdivisionRecord).filter(
            SubdivisionRecord.parent_task_id == task_id
        ).delete(synchronize_session=False)
        # Self-referential child tasks (parent_task_id FK on Task).
        db.query(Task).filter(Task.parent_task_id == task_id).delete(synchronize_session=False)
        db.query(Task).filter(Task.id == task_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _cleanup_llm(llm_id):
    from database import SessionLocal, LLM
    db = SessionLocal()
    try:
        db.query(LLM).filter(LLM.id == llm_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _cleanup_budget(budget_id):
    from database import SessionLocal, Budget
    db = SessionLocal()
    try:
        db.query(Budget).filter(Budget.id == budget_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _make_task(task_id, task_type, description="desc", llm_id=None, budget_id=None):
    from database import SessionLocal, Task
    db = SessionLocal()
    try:
        t = Task(id=task_id, title="T", type=task_type, position=0,
                 project="TestPipelineRouting", description=description,
                 llm_id=llm_id, budget_id=budget_id)
        db.add(t)
        db.commit()
    finally:
        db.close()


def _make_budget(name):
    """Create a Budget row and return its id."""
    from database import SessionLocal, Budget
    db = SessionLocal()
    try:
        b = Budget(name=name)
        db.add(b)
        db.commit()
        db.refresh(b)
        return b.id
    finally:
        db.close()


def _make_llm(address, port, model):
    """Create an LLM row and return its id."""
    from database import SessionLocal, LLM
    db = SessionLocal()
    try:
        llm = LLM(address=address, port=port, model=model)
        db.add(llm)
        db.commit()
        db.refresh(llm)
        return llm.id
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. ADVANCE_HANDLERS map
# ---------------------------------------------------------------------------

class TestAdvanceHandlersMap:
    """ADVANCE_HANDLERS covers exactly the 7 advanceable column types."""

    def test_all_advanceable_types_present(self):
        import main
        expected = {
            "idea", "planning", "indev",
            "conceptual_review", "optimization", "security", "full_review",
        }
        assert set(main.ADVANCE_HANDLERS.keys()) == expected

    def test_handler_values_are_nonempty_strings(self):
        import main
        for col_type, handler in main.ADVANCE_HANDLERS.items():
            assert isinstance(handler, str) and handler, \
                f"Handler for '{col_type}' must be a non-empty string"

    def test_correct_handlers(self):
        import main
        assert main.ADVANCE_HANDLERS["idea"]               == "_run_intake_pipeline"
        assert main.ADVANCE_HANDLERS["planning"]           == "_run_planning_pipeline_bg"
        assert main.ADVANCE_HANDLERS["indev"]              == "_run_dev_orchestrator_bg"
        assert main.ADVANCE_HANDLERS["conceptual_review"]  == "_advance_to_optimization"
        assert main.ADVANCE_HANDLERS["optimization"]       == "_run_security_pipeline_bg"
        assert main.ADVANCE_HANDLERS["security"]           == "_run_full_review_bg"
        assert main.ADVANCE_HANDLERS["full_review"]        == "_execute_merge_bg"

    def test_non_advanceable_types_absent(self):
        """architecture, completed, cancelled, subdividing are never in the map."""
        import main
        for t in ("architecture", "completed", "cancelled", "subdividing"):
            assert t not in main.ADVANCE_HANDLERS, \
                f"'{t}' should not be advanceable"


# ---------------------------------------------------------------------------
# 2. /api/tasks/{id}/advance - endpoint validation
# ---------------------------------------------------------------------------

class TestAdvanceEndpointValidation:
    """Advance endpoint enforces required fields and valid column types."""

    @pytest.fixture(autouse=True)
    def client(self):
        from starlette.testclient import TestClient
        import main
        self.client = TestClient(main.app, raise_server_exceptions=False)

    def test_404_nonexistent_task(self):
        r = self.client.post("/api/tasks/nonexistent-xyz-99999/advance")
        assert r.status_code == 404

    def test_422_empty_description(self):
        task_id = "test-adv-nodesc"
        _make_task(task_id, "idea", description="")
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "description" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)

    def test_422_missing_llm_id(self):
        task_id = "test-adv-nollm"
        _make_task(task_id, "idea", description="Some description")
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "llm" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)

    def test_422_missing_budget_id(self):
        task_id = "test-adv-nobud"
        llm_id = _make_llm("test-nobud-host", 19999, "test-nobud")
        _make_task(task_id, "idea", description="Some description", llm_id=llm_id)
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "budget" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)
            _cleanup_llm(llm_id)

    def test_422_architecture_not_advanceable(self):
        task_id = "test-adv-arch"
        _make_task(task_id, "architecture", description="desc")
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "architecture" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)

    def test_422_completed_not_advanceable(self):
        task_id = "test-adv-done"
        _make_task(task_id, "completed", description="desc")
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "completed" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)

    def test_200_pipeline_started_idea_task(self):
        """Valid idea task fires intake pipeline in the background."""
        task_id = "test-adv-ok"
        llm_id = _make_llm("test-ok-host", 19998, "test-ok")
        budget_id = _make_budget("test-budget-advance-ok")
        _make_task(task_id, "idea", description="Implement login",
                   llm_id=llm_id, budget_id=budget_id)
        try:
            with patch("main._run_intake_pipeline"):
                r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 200
            body = r.json()
            assert body["status"] == "PIPELINE_STARTED"
            assert task_id in body["task_id"]
        finally:
            _delete_task(task_id)
            _cleanup_llm(llm_id)
            _cleanup_budget(budget_id)

    def test_200_pipeline_started_planning_task(self):
        """Valid planning task fires planning pipeline in the background."""
        task_id = "test-adv-plan"
        llm_id = _make_llm("test-plan-host", 19997, "test-plan")
        budget_id = _make_budget("test-budget-advance-plan")
        _make_task(task_id, "planning", description="Design auth module",
                   llm_id=llm_id, budget_id=budget_id)
        try:
            with patch("main._run_planning_pipeline_bg"):
                r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 200
            assert r.json()["status"] == "PIPELINE_STARTED"
        finally:
            _delete_task(task_id)
            _cleanup_llm(llm_id)
            _cleanup_budget(budget_id)


# ---------------------------------------------------------------------------
# 3. Scheduler dispatch logic
# ---------------------------------------------------------------------------

class TestSchedulerDispatch:
    """Scheduler auto-dispatches all pipeline stages (planning, indev,
    conceptual_review, optimization, full_review) plus idea tasks.
    Architecture, security, and completed are the only types never dispatched."""

    # Patch targets: _tick() uses lazy imports, so we patch at the source module
    _DB_GET_ALL   = "app.database.get_all_tasks"
    _DB_GET_TASK  = "app.database.get_task"
    _DB_GET_LLM   = "app.database.get_llm"
    _DAG_RESOLVER = "app.agent.dag.DAGResolver"

    def _ready_task_dict(self, task_id, task_type):
        return {"id": task_id, "type": task_type, "position": 0, "prerequisites": []}

    def _resolver_returning(self, tasks):
        """Build a MockResolver.get_ready_tasks() that returns tasks."""
        mock = MagicMock()
        mock.get_ready_tasks.return_value = tasks
        return mock

    def _isolated_tick(self, ready_tasks, db_task=None, llm=None,
                       initial_counts=None, initial_cooldowns=None):
        """
        Run scheduler._tick() with all state dicts cleared (then optionally
        pre-seeded) and relevant modules patched.
        Returns the mock_thread used to check dispatches.
        """
        from app.agent import scheduler

        original_sessions  = dict(scheduler._active_sessions)
        original_counts    = dict(scheduler._llm_session_counts)
        original_cooldowns = dict(scheduler._failed_cooldowns)
        scheduler._active_sessions.clear()
        scheduler._llm_session_counts.clear()
        scheduler._failed_cooldowns.clear()
        # Apply any pre-conditions the caller needs
        if initial_counts:
            scheduler._llm_session_counts.update(initial_counts)
        if initial_cooldowns:
            scheduler._failed_cooldowns.update(initial_cooldowns)

        mock_thread = MagicMock()
        mock_thread.return_value.is_alive.return_value = False

        patches = [
            patch(self._DB_GET_ALL, return_value=[]),
            patch("app.agent.scheduler._cleanup_finished"),
            patch(self._DAG_RESOLVER, return_value=self._resolver_returning(ready_tasks)),
            patch("threading.Thread", mock_thread),
        ]
        if db_task is not None:
            patches.append(patch(self._DB_GET_TASK, return_value=db_task))
        if llm is not None:
            patches.append(patch(self._DB_GET_LLM, return_value=llm))

        try:
            for p in patches:
                p.start()
            scheduler._tick()
        finally:
            for p in reversed(patches):
                p.stop()
            scheduler._active_sessions.clear()
            scheduler._active_sessions.update(original_sessions)
            scheduler._llm_session_counts.clear()
            scheduler._llm_session_counts.update(original_counts)
            scheduler._failed_cooldowns.clear()
            scheduler._failed_cooldowns.update(original_cooldowns)

        return mock_thread

    def test_truly_non_dispatchable_columns_never_spawn_threads(self):
        """architecture, security, and completed are never in SCHEDULER_DISPATCHABLE_TYPES."""
        from app.agent.scheduler import SCHEDULER_DISPATCHABLE_TYPES
        for col_type in ("architecture", "security", "completed"):
            assert col_type not in SCHEDULER_DISPATCHABLE_TYPES, \
                f"'{col_type}' must not be auto-dispatchable"
            task = self._ready_task_dict(f"sched-skip-{col_type}", col_type)
            mock_thread = self._isolated_tick([task])
            mock_thread.assert_not_called(), \
                f"Scheduler must NOT dispatch '{col_type}'"

    def test_pipeline_stages_are_dispatchable(self):
        """All mid-pipeline stages must be in SCHEDULER_DISPATCHABLE_TYPES for
        orphan recovery after server restart."""
        from app.agent.scheduler import SCHEDULER_DISPATCHABLE_TYPES
        for col_type in ("indev", "conceptual_review", "optimization", "full_review"):
            assert col_type in SCHEDULER_DISPATCHABLE_TYPES, \
                f"'{col_type}' must be auto-dispatchable (restart recovery)"

    def test_planning_task_spawns_thread(self):
        """A ready planning task with LLM assigned gets dispatched."""
        task = self._ready_task_dict("sched-plan-1", "planning")
        db_task = MagicMock(llm_id=55, budget_id=1)
        llm = MagicMock(id=55, parallel_sessions=5, address="localhost",
                        port=8008, model="test")
        mock_thread = self._isolated_tick([task], db_task=db_task, llm=llm)
        mock_thread.assert_called_once()
        target = mock_thread.call_args.kwargs.get("target") or \
                 mock_thread.call_args[1].get("target")
        from app.agent.scheduler import _run_task
        assert target == _run_task

    def test_indev_task_spawns_thread(self):
        """A ready indev task with LLM assigned gets dispatched."""
        task = self._ready_task_dict("sched-indev-1", "indev")
        db_task = MagicMock(llm_id=66, budget_id=1)
        llm = MagicMock(id=66, parallel_sessions=3, address="localhost",
                        port=8008, model="test")
        mock_thread = self._isolated_tick([task], db_task=db_task, llm=llm)
        mock_thread.assert_called_once()

    def test_conceptual_review_task_spawns_thread(self):
        """An orphaned conceptual_review task is re-dispatched (restart recovery)."""
        task = self._ready_task_dict("sched-cr-1", "conceptual_review")
        db_task = MagicMock(llm_id=77, budget_id=1)
        llm = MagicMock(id=77, parallel_sessions=3, address="localhost",
                        port=8008, model="test")
        mock_thread = self._isolated_tick([task], db_task=db_task, llm=llm)
        mock_thread.assert_called_once()

    def test_optimization_task_spawns_thread(self):
        """An orphaned optimization task is re-dispatched (restart recovery)."""
        task = self._ready_task_dict("sched-opt-1", "optimization")
        db_task = MagicMock(llm_id=78, budget_id=1)
        llm = MagicMock(id=78, parallel_sessions=3, address="localhost",
                        port=8008, model="test")
        mock_thread = self._isolated_tick([task], db_task=db_task, llm=llm)
        mock_thread.assert_called_once()

    def test_full_review_task_spawns_thread(self):
        """An orphaned full_review task is re-dispatched (restart recovery)."""
        task = self._ready_task_dict("sched-fr-1", "full_review")
        db_task = MagicMock(llm_id=79, budget_id=1)
        llm = MagicMock(id=79, parallel_sessions=3, address="localhost",
                        port=8008, model="test")
        mock_thread = self._isolated_tick([task], db_task=db_task, llm=llm)
        mock_thread.assert_called_once()

    def test_task_without_llm_is_skipped(self):
        """Task with no LLM assigned must not be dispatched."""
        task = self._ready_task_dict("sched-nollm-1", "planning")
        db_task = MagicMock(llm_id=None)
        mock_thread = self._isolated_tick([task], db_task=db_task)
        mock_thread.assert_not_called()

    def test_llm_at_capacity_defers_task(self):
        """Task whose LLM is already at max parallel_sessions is deferred."""
        task = self._ready_task_dict("sched-cap-1", "planning")
        db_task = MagicMock(llm_id=44, budget_id=1)
        llm = MagicMock(id=44, parallel_sessions=1, address="localhost",
                        port=8008, model="test")
        # Pre-seed: LLM 44 already has 1 session (== parallel_sessions limit)
        mock_thread = self._isolated_tick(
            [task], db_task=db_task, llm=llm, initial_counts={44: 1}
        )
        mock_thread.assert_not_called()

    def test_cooldown_prevents_retry(self):
        """Task in cooldown after failure must not be re-dispatched."""
        import time
        task = self._ready_task_dict("sched-cool-1", "planning")
        db_task = MagicMock(llm_id=33, budget_id=1)
        llm = MagicMock(id=33, parallel_sessions=5, address="localhost",
                        port=8008, model="test")
        # Pre-seed: task failed 5s ago (well within 60s cooldown window)
        mock_thread = self._isolated_tick(
            [task], db_task=db_task, llm=llm,
            initial_cooldowns={"sched-cool-1": time.time() - 5}
        )
        mock_thread.assert_not_called()


# ---------------------------------------------------------------------------
# 4. Direct column transitions
# ---------------------------------------------------------------------------

class TestDirectTransitions:
    """_advance_to_optimization dispatches conceptual review and moves column."""

    def test_passes_moves_to_optimization(self):
        task_id = "test-trans-pass"
        budget_id = None
        try:
            budget_id = _make_budget("test-trans-pass-budget")
            _make_task(task_id, "conceptual_review", description="desc",
                       budget_id=budget_id)
            import main
            from database import get_task
            with patch("app.agent.conceptual_review.run_conceptual_review",
                       return_value={"outcome": "passed", "votes": []}), \
                 patch("main._store_pipeline_result_generic"), \
                 patch("main._resolve_llm_endpoint",
                       return_value=("http://localhost:8008/v1", "test", 4096)):
                main._advance_to_optimization(task_id)
            updated = get_task(task_id)
            assert updated.type == "optimization"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)

    def test_failure_demotes_to_indev(self):
        task_id = "test-trans-fail"
        budget_id = None
        try:
            budget_id = _make_budget("test-trans-fail-budget")
            _make_task(task_id, "conceptual_review", description="desc",
                       budget_id=budget_id)
            import main
            from database import get_task
            with patch("app.agent.conceptual_review.run_conceptual_review",
                       return_value={"outcome": "rejected", "votes": []}), \
                 patch("main._store_pipeline_result_generic"), \
                 patch("main._resolve_llm_endpoint",
                       return_value=("http://localhost:8008/v1", "test", 4096)):
                main._advance_to_optimization(task_id)
            updated = get_task(task_id)
            assert updated.type == "indev"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)


# ---------------------------------------------------------------------------
# 5. Mock LLM - intake pipeline scenarios
# ---------------------------------------------------------------------------

class TestIntakePipelineMockLLM:
    """
    Run run_intake_pipeline() end-to-end with a mocked LLM.

    The mock patches httpx.AsyncClient so call_llm() returns canned responses
    without hitting any real server. The full pipeline logic (tally, rule
    evaluation, stage sequencing) executes for real.
    """

    def _run_intake(self, scenario, task_id, budget_id):
        """Helper: run intake pipeline with a given MockLLM scenario."""
        from app.agent.intake import run_intake_pipeline
        from app.agent.mock_llm import MockLLM

        mock = MockLLM(scenario=scenario)

        async def _go():
            with patch("httpx.AsyncClient") as cls:
                cls.return_value = mock.get_async_client_mock()
                return await run_intake_pipeline(
                    task_id=task_id,
                    task_description="Add user authentication to the app",
                    task_title="User Auth",
                    all_tasks=[],
                    budget_id=budget_id,
                    llm_id=1,
                    llm_base_url="http://localhost:8008/v1",
                    llm_model="mock-model",
                    project=None,  # Pipeline will fail unless project is configured
                )

        return asyncio.run(_go())

    def test_all_pass_outcome_and_votes(self):
        """intake_all_pass: outcome == 'passed' with votes from all stages.

        Patches app.agent.intake.call_llm (Level 1) for the three LLM stages.
        Also patches _stage_static_analysis directly: that stage runs real
        tree-sitter on the project filesystem (the original source of the 5s
        slowness) and is orthogonally tested in test_static_analysis.py.
        Without the static analysis mock, generate_vote can return NEEDS_RESEARCH
        non-deterministically, which triggers the research handler — a different
        module that has its own call_llm import site not covered by this patch.
        """
        from app.agent.mock_llm import (
            _SCOPE_RESPONSE_PASS,
            _CONFLICT_RESPONSE_PASS,
            _FEASIBILITY_RESPONSE_PASS,
        )
        from app.agent.intake import IntakePipeline, run_intake_pipeline

        def _resp(content_dict):
            return {
                "choices": [{"message": {"content": json.dumps(content_dict),
                                         "tool_calls": None},
                             "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 50, "completion_tokens": 100,
                          "total_tokens": 150},
            }

        responses = [
            _resp(_SCOPE_RESPONSE_PASS),
            _resp(_CONFLICT_RESPONSE_PASS),
            _resp(_FEASIBILITY_RESPONSE_PASS),
        ]

        async def _sequential(*a, **kw):
            return responses.pop(0) if len(responses) > 1 else responses[0]

        async def _mock_static_analysis(self, scope_vote):
            return {
                "stage": "static_analysis",
                "verdict": "POSSIBLE",
                "confidence": 0.75,
                "justification": "Static analysis mocked in test.",
                "raw_response": {},
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": "static_analysis",
            }

        task_id = "test-intake-pass"
        budget_id = None
        try:
            budget_id = _make_budget("test-intake-pass-budget")
            _make_task(task_id, "idea", description="User auth")

            async def _go():
                with patch("app.agent.intake.call_llm", _sequential), \
                     patch.object(IntakePipeline, "_stage_static_analysis",
                                  _mock_static_analysis):
                    return await run_intake_pipeline(
                        task_id=task_id,
                        task_description="Add user authentication to the app",
                        task_title="User Auth",
                        all_tasks=[],
                        budget_id=budget_id,
                        llm_id=1,
                        llm_base_url="http://localhost:8008/v1",
                        llm_model="mock-model",
                        project=None,
                    )

            result = asyncio.run(_go())
            assert result["outcome"] == "passed", \
                f"Expected 'passed', got {result['outcome']!r}. Votes: {result.get('votes')}"
            assert len(result["votes"]) >= 3, \
                f"Expected at least 3 votes, got {len(result['votes'])}"
            assert "scope_analysis" in {v["stage"] for v in result["votes"]}
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)

    def test_rejected_scope_outcome_is_rejected(self):
        """intake_rejected scenario (scope votes REJECTED) -> outcome == 'rejected'."""
        task_id = "test-intake-rej"
        budget_id = None
        try:
            budget_id = _make_budget("test-intake-rej-budget")
            _make_task(task_id, "idea", description="User auth")
            result = self._run_intake("intake_rejected", task_id, budget_id)
            assert result["outcome"] == "rejected", \
                f"Expected 'rejected', got {result['outcome']!r}"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)

    def test_needs_research_triggers_research_then_passes(self):
        """intake_needs_research scenario -> research agent runs -> outcome == 'passed'."""
        task_id = "test-intake-nr"
        budget_id = None
        try:
            budget_id = _make_budget("test-intake-nr-budget")
            _make_task(task_id, "idea", description="User auth")
            result = self._run_intake("intake_needs_research", task_id, budget_id)
            assert result["outcome"] == "passed", \
                f"Expected 'passed' after research, got {result['outcome']!r}"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)

    def test_tie_triggers_tiebreaker_then_passes(self):
        """intake_tie scenario -> tie-breaker fires -> outcome == 'passed'."""
        task_id = "test-intake-tie"
        budget_id = None
        try:
            budget_id = _make_budget("test-intake-tie-budget")
            _make_task(task_id, "idea", description="User auth")
            result = self._run_intake("intake_tie", task_id, budget_id)
            assert result["outcome"] == "passed", \
                f"Expected 'passed' after tiebreak, got {result['outcome']!r}"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)


    def test_rejected_result_contains_justification(self):
        """Rejected intake result must carry a justification in the vote."""
        task_id = "test-intake-rej-just"
        budget_id = None
        try:
            budget_id = _make_budget("test-intake-rej-just-budget")
            _make_task(task_id, "idea", description="User auth")
            result = self._run_intake("intake_rejected", task_id, budget_id)
            assert result["outcome"] == "rejected"
            votes = result.get("votes", [])
            assert any(v.get("justification") for v in votes), \
                "At least one vote must carry a justification string"
        finally:
            _delete_task(task_id)
            if budget_id is not None:
                _cleanup_budget(budget_id)


# ---------------------------------------------------------------------------
# 6. Mock LLM - tally rule edge cases
# ---------------------------------------------------------------------------

class TestTallyRules:
    """
    Verify tally_votes() enforces the correct rule priority order.
    These tests call tally_votes() directly - no HTTP required.
    """

    def _vote(self, verdict, confidence=80):
        from app.agent.verdicts import Vote
        return Vote(stage="test", verdict=verdict, confidence=confidence,
                    justification="test")

    def test_rule0_subdivide_beats_everything(self):
        """Any SUBDIVIDE_IDEA vote wins over LIKELY votes."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.LIKELY, 92),
            self._vote(Verdict.SUBDIVIDE_IDEA, 70),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_rule1_single_rejected_blocks(self):
        """A single REJECTED vote blocks all passing votes."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.POSSIBLE, 80),
            self._vote(Verdict.REJECTED, 20),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"

    def test_rule2_majority_not_suitable_rejects(self):
        """Majority NOT_SUITABLE -> rejected."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.NOT_SUITABLE, 55),
            self._vote(Verdict.NOT_SUITABLE, 58),
            self._vote(Verdict.POSSIBLE, 80),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"

    def test_rule3_needs_research_triggers_research(self):
        """Any NEEDS_RESEARCH vote -> outcome needs_research."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 92),
            self._vote(Verdict.NEEDS_RESEARCH, 65),
        ]
        result = tally_votes(votes)
        assert result.outcome == "needs_research"

    def test_rule4_tie_triggers_tiebreaker(self):
        """Equal pass/fail split with no REJECTED -> tie.

        Rule 1 (any REJECTED -> rejected) fires before Rule 4, so the tie
        scenario must use only NOT_SUITABLE (fail-ish) against pass-ish votes.
        2 NOT_SUITABLE vs 2 LIKELY: majority threshold = (4//2)+1 = 3, so
        Rule 2 doesn't fire; Rule 4 fires -> tie.
        """
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.LIKELY, 92),
            self._vote(Verdict.NOT_SUITABLE, 58),
            self._vote(Verdict.NOT_SUITABLE, 55),
        ]
        result = tally_votes(votes)
        assert result.outcome == "tie"

    def test_rule5_majority_pass(self):
        """Clear majority of passing verdicts -> passed."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.LIKELY, 93),
            self._vote(Verdict.POSSIBLE, 80),
            self._vote(Verdict.NOT_SUITABLE, 55),
        ]
        result = tally_votes(votes)
        assert result.outcome == "passed"

    def test_rule0_has_priority_over_rule1(self):
        """SUBDIVIDE_IDEA + REJECTED -> outcome is 'subdivide', not 'rejected'."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.SUBDIVIDE_IDEA, 60),
            self._vote(Verdict.REJECTED, 15),
        ]
        result = tally_votes(votes)
        assert result.outcome == "subdivide"

    def test_rule1_has_priority_over_rule3(self):
        """REJECTED + NEEDS_RESEARCH -> outcome is 'rejected', not 'needs_research'."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.REJECTED, 20),
            self._vote(Verdict.NEEDS_RESEARCH, 65),
        ]
        result = tally_votes(votes)
        assert result.outcome == "rejected"
