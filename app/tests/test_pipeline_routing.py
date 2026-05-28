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
    from app.database import (
        SessionLocal, Task, TransitionVote, TransitionResult, BudgetEntry,
        SubdivisionRecord, PlanningResult, ComponentResult,
        OptimizationResult, SecurityReviewResult, FinalReviewResult,
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
            SecurityReviewResult, FinalReviewResult, MergeRecord,
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
    from app.database import SessionLocal, LLM
    db = SessionLocal()
    try:
        db.query(LLM).filter(LLM.id == llm_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _cleanup_budget(budget_id):
    from app.database import SessionLocal, Budget
    db = SessionLocal()
    try:
        db.query(Budget).filter(Budget.id == budget_id).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _make_task(task_id, task_type, description="desc", llm_id=None, budget_id=None,
               clarification_status=None):
    from app.database import SessionLocal, Task
    db = SessionLocal()
    try:
        kwargs = dict(id=task_id, title="T", type=task_type, position=0,
                 project="TestPipelineRouting", description=description,
                 llm_id=llm_id, budget_id=budget_id)
        if clarification_status is not None:
            kwargs["clarification_status"] = clarification_status
        t = Task(**kwargs)
        db.add(t)
        db.commit()
    finally:
        db.close()


def _make_budget(name):
    """Create a Budget row and return its id."""
    from app.database import SessionLocal, Budget
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
    from app.database import SessionLocal, LLM
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
    """ADVANCE_HANDLERS covers AI-driven pipeline stages handled by the advance endpoint."""

    def test_all_advanceable_types_present(self):
        import main
        # idea/security/final_review/planning/indev now all dispatched by scheduler via node executors
        assert set(main.ADVANCE_HANDLERS.keys()) == set()

    def test_non_advanceable_types_absent(self):
        """architecture, completed, cancelled, subdividing, idea are never in the map."""
        import main
        for t in ("architecture", "completed", "cancelled", "subdividing", "idea"):
            assert t not in main.ADVANCE_HANDLERS, \
                f"'{t}' should not be manually advanceable"


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

    def test_422_idea_not_manually_advanceable(self):
        """Idea tasks are scheduler-dispatched; manual advance returns 422."""
        task_id = "test-adv-idea"
        _make_task(task_id, "idea", description="Some description")
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
            assert "idea" in r.json()["detail"].lower()
        finally:
            _delete_task(task_id)

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

    def test_422_planning_task_not_advanceable(self):
        """Planning tasks are no longer advanceable via this endpoint; scheduling is scheduler-driven."""
        task_id = "test-adv-plan"
        llm_id = _make_llm("test-plan-host", 19997, "test-plan")
        budget_id = _make_budget("test-budget-advance-plan")
        _make_task(task_id, "planning", description="Design auth module",
                   llm_id=llm_id, budget_id=budget_id)
        try:
            r = self.client.post(f"/api/tasks/{task_id}/advance")
            assert r.status_code == 422
        finally:
            _delete_task(task_id)
            _cleanup_llm(llm_id)
            _cleanup_budget(budget_id)


# ---------------------------------------------------------------------------
# 3. Scheduler dispatch logic
# ---------------------------------------------------------------------------

class TestSchedulerDispatch:
    """Scheduler auto-dispatches all pipeline stages (planning, indev,
    conceptual_review, optimization, security, final_review) plus idea tasks.
    Architecture, human_review, and completed are the only types never dispatched."""

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

        # Patch all side-effect-heavy dispatch helpers that are not under test here.
        # Each makes multiple DB round-trips to arcbox; skipping them cuts ~200 ms/tick.
        _noop = "app.agent.scheduler."
        patches = [
            patch(self._DB_GET_ALL, return_value=[]),
            patch("app.agent.scheduler._cleanup_finished"),
            patch("app.agent.scheduler._recover_hung_sessions"),
            patch("app.agent.scheduler._rescue_stale_jobs"),
            patch("app.agent.scheduler._check_model_block_timeout"),
            patch("app.agent.scheduler._dispatch_clarification_jobs"),
            patch("app.agent.scheduler._expire_autopilot_objectives"),
            patch("app.agent.scheduler._dispatch_maestro", return_value=None),
            patch("app.agent.scheduler._dispatch_heartbeat_maestro", return_value=None),
            patch("app.agent.scheduler._dispatch_file_summary_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_scope_survey_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_arch_gen_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_pip_resolution_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_goal_verification_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_research_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_episodic_summary_jobs", return_value=None),
            patch("app.agent.scheduler._dispatch_stranded_subdivisions"),
            patch("app.agent.scheduler._dispatch_factory_triggers"),
            patch("app.agent.scheduler._run_episodic_cleanup"),
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
        """architecture and completed are never in SCHEDULER_DISPATCHABLE_TYPES."""
        from app.agent.scheduler import SCHEDULER_DISPATCHABLE_TYPES
        for col_type in ("architecture", "completed"):
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
        for col_type in ("indev", "conceptual_review", "optimization", "security", "final_review"):
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

    def test_final_review_task_spawns_thread(self):
        """An orphaned final_review task is re-dispatched (restart recovery)."""
        task = self._ready_task_dict("sched-fr-1", "final_review")
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
# 5. IntakePipeline routing unit tests
# ---------------------------------------------------------------------------

class TestIntakePipelineMockLLM:
    """
    Unit tests for IntakePipeline.run() routing logic.

    All four stage methods are mocked on the pipeline instance with AsyncMock,
    so run() only exercises vote routing and tally logic.  No call_llm, no
    httpx, no DB, no filesystem, no network.  Each test completes in < 100ms.
    """

    def _make_vote(self, stage, verdict, confidence=0.80):
        return {
            "stage": stage,
            "verdict": verdict,
            "confidence": confidence,
            "justification": f"mocked {stage} verdict",
            "raw_response": {},
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model": "mock",
        }

    def _make_canned_research(self, verdict="LIKELY"):
        from app.agent.research import ResearchResult
        return ResearchResult(
            vote={"verdict": verdict, "confidence": 0.90,
                  "justification": "Research mocked in test."},
            lives_used=1, total_turns=1, findings="Mocked findings.",
        )

    def _run_pipeline(self, scope="LIKELY", static="POSSIBLE",
                      conflict="LIKELY", feasibility="LIKELY"):
        """
        Run IntakePipeline.run() with all four stage methods replaced by
        AsyncMock instances returning the specified verdicts.  run_research
        and run_tiebreaker are also patched so no real I/O can escape.
        """
        from app.agent._intake_pipeline import IntakePipeline

        pipeline = IntakePipeline(
            task_id="test-intk-unit",
            task_description="Add user authentication to the app",
            task_title="User Auth",
            all_tasks=[],
            budget_id=1,
            llm_id=1,
            llm_base_url="http://localhost:8008/v1",
            llm_model="mock-model",
            project=None,
        )

        canned = self._make_canned_research()

        async def _go():
            with patch.object(pipeline, "_stage_scope_analysis",
                              AsyncMock(return_value=self._make_vote("scope_analysis", scope))), \
                 patch.object(pipeline, "_stage_static_analysis",
                              AsyncMock(return_value=self._make_vote("static_analysis", static))), \
                 patch.object(pipeline, "_stage_conflict_detection",
                              AsyncMock(return_value=self._make_vote("conflict_detection", conflict))), \
                 patch.object(pipeline, "_stage_feasibility",
                              AsyncMock(return_value=self._make_vote("feasibility", feasibility))), \
                 patch("app.agent.research.run_research", AsyncMock(return_value=canned)), \
                 patch("app.agent.research.run_tiebreaker", AsyncMock(return_value=canned)):
                return await pipeline.run()

        return asyncio.run(_go())

    def test_all_pass_outcome_and_votes(self):
        """All four stages return passing verdicts → outcome 'passed', ≥4 votes."""
        result = self._run_pipeline()
        assert result["outcome"] == "passed", \
            f"Expected 'passed', got {result['outcome']!r}. Votes: {result.get('votes')}"
        assert len(result["votes"]) >= 4, \
            f"Expected at least 4 votes, got {len(result['votes'])}"
        stages = {v["stage"] for v in result["votes"]}
        assert "scope_analysis" in stages

    def test_single_rejected_scope_does_not_veto(self):
        """A single REJECTED scope vote no longer vetoes — all stages still run.

        1 REJECTED out of 4 votes is below the majority threshold of 3,
        so the outcome must be 'passed'.
        """
        result = self._run_pipeline(scope="REJECTED")
        assert result["outcome"] == "passed", \
            f"Expected 'passed' (single REJECTED is not a veto), got {result['outcome']!r}"
        # All 4 stages must have run — no early exit
        assert len(result["votes"]) == 4

    def test_needs_research_triggers_research_then_passes(self):
        """NEEDS_RESEARCH scope → run_research called (mocked LIKELY) → outcome 'passed'."""
        result = self._run_pipeline(scope="NEEDS_RESEARCH")
        assert result["outcome"] == "passed", \
            f"Expected 'passed' after research, got {result['outcome']!r}"

    def test_tie_triggers_tiebreaker_then_passes(self):
        """2 LIKELY vs 2 NOT_SUITABLE → tie → run_tiebreaker (mocked LIKELY) → 'passed'."""
        result = self._run_pipeline(
            scope="LIKELY", static="NOT_SUITABLE",
            conflict="LIKELY", feasibility="NOT_SUITABLE",
        )
        assert result["outcome"] == "passed", \
            f"Expected 'passed' after tiebreak, got {result['outcome']!r}"

    def test_majority_negative_votes_rejected_with_justification(self):
        """3 negative votes out of 4 → 'rejected' with justification strings."""
        result = self._run_pipeline(
            scope="REJECTED", static="NOT_SUITABLE",
            conflict="REJECTED", feasibility="REJECTED",
        )
        assert result["outcome"] == "rejected", \
            f"Expected 'rejected' with 3/4 negative votes, got {result['outcome']!r}"
        assert len(result.get("rejection_reasons", [])) > 0, \
            "rejection_reasons must be populated"
        votes = result.get("votes", [])
        assert any(v.get("justification") for v in votes), \
            "At least one vote must carry a justification string"


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

    def test_rule0_subdivide_majority_beats_everything(self):
        """Majority of LLM stages voting SUBDIVIDE_IDEA wins over LIKELY votes."""
        from app.agent.verdicts import tally_votes, Verdict
        # 2/3 LLM stages vote SUBDIVIDE_IDEA; threshold = max(2, 2) = 2 → fires
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.SUBDIVIDE_IDEA, 70),
            self._vote(Verdict.SUBDIVIDE_IDEA, 72),
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

    def test_rule2_not_suitable_is_abstention(self):
        """NOT_SUITABLE votes are abstentions — majority NOT_SUITABLE no longer rejects."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.NOT_SUITABLE, 55),
            self._vote(Verdict.NOT_SUITABLE, 58),
            self._vote(Verdict.POSSIBLE, 80),
        ]
        result = tally_votes(votes)
        # 2 abstain, 1 POSSIBLE → only 1 effective vote → passed
        assert result.outcome == "passed"

    def test_rule3_needs_research_triggers_research(self):
        """Any NEEDS_RESEARCH vote -> outcome needs_research."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 92),
            self._vote(Verdict.NEEDS_RESEARCH, 65),
        ]
        result = tally_votes(votes)
        assert result.outcome == "needs_research"

    def test_rule4_not_suitable_no_longer_ties(self):
        """NOT_SUITABLE is now an abstention, not fail-ish — 2 LIKELY + 2 NOT_SUITABLE → passed."""
        from app.agent.verdicts import tally_votes, Verdict
        votes = [
            self._vote(Verdict.LIKELY, 95),
            self._vote(Verdict.LIKELY, 92),
            self._vote(Verdict.NOT_SUITABLE, 58),
            self._vote(Verdict.NOT_SUITABLE, 55),
        ]
        result = tally_votes(votes)
        # 2 abstain, 2 LIKELY effective → passed (not tie)
        assert result.outcome == "passed"

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

    def test_rule0_majority_has_priority_over_rule1(self):
        """Majority SUBDIVIDE_IDEA + REJECTED -> outcome is 'subdivide', not 'rejected'."""
        from app.agent.verdicts import tally_votes, Verdict
        # 2/3 LLM stages vote SUBDIVIDE_IDEA; threshold met → Rule 0 fires before Rule 1
        votes = [
            self._vote(Verdict.SUBDIVIDE_IDEA, 60),
            self._vote(Verdict.SUBDIVIDE_IDEA, 65),
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
