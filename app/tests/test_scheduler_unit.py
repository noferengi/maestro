"""
test_scheduler_unit.py
----------------------
Unit tests for app/agent/scheduler.py.

Covers:
  - get_scheduler_status() structure and type invariants
  - start_scheduler() no-op when SCHEDULER_ENABLED is False
  - start_scheduler() idempotency when a thread is already alive
  - _task_to_mini_dict() field mapping (including None prerequisites)
  - _cleanup_finished() removes dead threads, keeps alive threads
  - _run_task() releases LLM session slot on success AND on exception
  - _run_task() records cooldown timestamp on failure
  - _tick() skips tasks that are not in (planning, indev)
  - _tick() skips tasks already running in _active_sessions
  - _tick() skips tasks within their failure cooldown window
"""

import os
import sys
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app.database
import app.agent.scheduler as sched_mod
from app.agent.scheduler import (
    _active_sessions,
    _active_sessions_lock,
    _cleanup_finished,
    _failed_cooldowns,
    _llm_counts_lock,
    _llm_session_counts,
    _run_task,
    _task_to_mini_dict,
    _rescue_stale_jobs,
    get_scheduler_status,
    start_scheduler,
    stop_scheduler,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fake_llm(llm_id=1, parallel_sessions=2):
    llm = MagicMock()
    llm.id = llm_id
    llm.parallel_sessions = parallel_sessions
    llm.address = "127.0.0.1"
    llm.port = 8008
    llm.model = "test-model"
    llm.max_context = 4096
    return llm


def _fake_db_task(
    task_id="t1",
    task_type="planning",
    llm_id=1,
    budget_id=1,
    project="TestProject",
    description="Do a thing",
    prerequisites=None,
    parent_task_id=None,
):
    task = MagicMock()
    task.id = task_id
    task.type = task_type
    task.position = 0
    task.prerequisites = prerequisites or []
    task.llm_id = llm_id
    task.budget_id = budget_id
    task.project = project
    task.description = description
    task.title = "Test Task"
    task.parent_task_id = parent_task_id
    task.intake_exhausted_at = None  # not exhausted by default
    return task


@pytest.fixture(autouse=True)
def clean_scheduler_state():
    """Reset shared scheduler state before and after every test."""
    # Before
    with _active_sessions_lock:
        _active_sessions.clear()
    with _llm_counts_lock:
        _llm_session_counts.clear()
    _failed_cooldowns.clear()
    sched_mod._scheduler_thread = None
    sched_mod._scheduler_stop.clear()
    yield
    # After - stop any running scheduler thread
    if sched_mod._scheduler_thread and sched_mod._scheduler_thread.is_alive():
        sched_mod._scheduler_stop.set()
        sched_mod._scheduler_thread.join(timeout=3)
    sched_mod._scheduler_thread = None
    with _active_sessions_lock:
        _active_sessions.clear()
    with _llm_counts_lock:
        _llm_session_counts.clear()
    _failed_cooldowns.clear()


# ===========================================================================
# get_scheduler_status
# ===========================================================================

class TestGetSchedulerStatus:
    def test_returns_dict(self):
        assert isinstance(get_scheduler_status(), dict)

    def test_has_required_keys(self):
        status = get_scheduler_status()
        for key in ("running", "active_sessions", "llm_session_counts", "tick_interval"):
            assert key in status, f"Missing key: {key}"

    def test_running_is_bool(self):
        assert isinstance(get_scheduler_status()["running"], bool)

    def test_active_sessions_is_dict(self):
        assert isinstance(get_scheduler_status()["active_sessions"], dict)

    def test_tick_interval_is_positive(self):
        assert get_scheduler_status()["tick_interval"] > 0

    def test_not_running_when_no_thread(self):
        sched_mod._scheduler_thread = None
        assert get_scheduler_status()["running"] is False

    def test_active_sessions_reflects_live_thread(self):
        barrier = threading.Barrier(2)
        t = threading.Thread(target=lambda: barrier.wait())
        t.start()
        with _active_sessions_lock:
            _active_sessions["live-task"] = t
        try:
            status = get_scheduler_status()
            assert "live-task" in status["active_sessions"]
            assert status["active_sessions"]["live-task"] is True
        finally:
            barrier.wait()
            t.join()


# ===========================================================================
# start_scheduler / stop_scheduler
# ===========================================================================

class TestStartStopScheduler:
    def test_start_when_disabled_creates_no_thread(self, monkeypatch):
        monkeypatch.setattr("app.agent.scheduler.SCHEDULER_ENABLED", False)
        start_scheduler()
        assert sched_mod._scheduler_thread is None

    def test_start_when_already_alive_does_not_create_second_thread(self, monkeypatch):
        monkeypatch.setattr("app.agent.scheduler.SCHEDULER_ENABLED", True)
        fake_thread = MagicMock()
        fake_thread.is_alive.return_value = True
        sched_mod._scheduler_thread = fake_thread

        with patch("app.agent.scheduler.threading.Thread") as mock_cls:
            start_scheduler()
            mock_cls.assert_not_called()

    def test_stop_when_no_thread_does_not_raise(self):
        sched_mod._scheduler_thread = None
        stop_scheduler()  # Must not raise


# ===========================================================================
# _task_to_mini_dict
# ===========================================================================

class TestTaskToMiniDict:
    def test_id_mapped(self):
        task = _fake_db_task(task_id="task-7")
        assert _task_to_mini_dict(task)["id"] == "task-7"

    def test_type_mapped(self):
        task = _fake_db_task(task_type="indev")
        assert _task_to_mini_dict(task)["type"] == "indev"

    def test_position_mapped(self):
        task = _fake_db_task()
        task.position = 42
        assert _task_to_mini_dict(task)["position"] == 42

    def test_prerequisites_mapped(self):
        task = _fake_db_task(prerequisites=["dep-1", "dep-2"])
        assert _task_to_mini_dict(task)["prerequisites"] == ["dep-1", "dep-2"]

    def test_none_prerequisites_becomes_empty_list(self):
        """None prerequisites must become [] - DAGResolver does not handle None."""
        task = _fake_db_task()
        task.prerequisites = None
        result = _task_to_mini_dict(task)
        assert result["prerequisites"] == []

    def test_parent_task_id_mapped(self):
        task = _fake_db_task(parent_task_id="parent-99")
        assert _task_to_mini_dict(task)["parent_task_id"] == "parent-99"

    def test_parent_task_id_none_when_absent(self):
        task = _fake_db_task()
        assert _task_to_mini_dict(task)["parent_task_id"] is None

    def test_returns_plain_dict(self):
        task = _fake_db_task()
        assert type(_task_to_mini_dict(task)) is dict


# ===========================================================================
# _cleanup_finished
# ===========================================================================

class TestCleanupFinished:
    def test_dead_thread_removed(self):
        t = threading.Thread(target=lambda: None)
        t.start()
        t.join()  # Wait until definitely dead
        with _active_sessions_lock:
            _active_sessions["dead-t"] = t

        _cleanup_finished()

        with _active_sessions_lock:
            assert "dead-t" not in _active_sessions

    def test_live_thread_retained(self):
        barrier = threading.Barrier(2)
        t = threading.Thread(target=lambda: barrier.wait())
        t.start()
        with _active_sessions_lock:
            _active_sessions["live-t"] = t

        _cleanup_finished()

        with _active_sessions_lock:
            assert "live-t" in _active_sessions

        barrier.wait()
        t.join()
        with _active_sessions_lock:
            _active_sessions.pop("live-t", None)

    def test_mixed_alive_and_dead(self):
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()

        barrier = threading.Barrier(2)
        alive = threading.Thread(target=lambda: barrier.wait())
        alive.start()

        with _active_sessions_lock:
            _active_sessions["dead-m"] = dead
            _active_sessions["alive-m"] = alive

        _cleanup_finished()

        with _active_sessions_lock:
            assert "dead-m" not in _active_sessions
            assert "alive-m" in _active_sessions

        barrier.wait()
        alive.join()
        with _active_sessions_lock:
            _active_sessions.pop("alive-m", None)


class TestCheckPlanningTimeouts:
    def test_check_planning_timeouts_kills_session(self):
        from app.agent.scheduler import (
            _check_planning_timeouts, _active_sessions, _session_types,
            _session_started_at, _session_ids, _PLANNING_SESSION_TIMEOUT_SECS,
            _session_llm_ids, _llm_session_counts
        )
        import time
        
        mock_thread = MagicMock(spec=threading.Thread)
        mock_thread.is_alive.return_value = True
        task_id = "timeout-task"
        session_id = "llm-session-123"
        
        # Patch the function in llm_client since it's imported locally in _check_planning_timeouts
        with patch("app.agent.llm_client.kill_session") as mock_kill:
            with _active_sessions_lock:
                _active_sessions[task_id] = mock_thread
                _session_types[task_id] = "planning"
                _session_started_at[task_id] = time.time() - (_PLANNING_SESSION_TIMEOUT_SECS + 10)
                _session_ids[task_id] = session_id
                _session_llm_ids[task_id] = 46
            
            with _llm_counts_lock:
                _llm_session_counts[46] = 1
                
            _check_planning_timeouts()
            
            mock_kill.assert_called_once_with(session_id)
            with _active_sessions_lock:
                assert task_id not in _active_sessions
                assert task_id not in _session_ids
            with _llm_counts_lock:
                assert _llm_session_counts[46] == 0


# ===========================================================================
# _run_task - LLM slot lifecycle
# ===========================================================================

class TestRunTaskSlotLifecycle:
    def test_slot_released_on_normal_completion(self):
        """LLM slot decremented after successful _run_maestro_loop."""
        llm = _fake_llm(llm_id=10)
        db_task = _fake_db_task(task_type="planning", task_id="t-ok")
        with _llm_counts_lock:
            _llm_session_counts[10] = 1

        with patch("app.agent.scheduler._run_maestro_loop", return_value=None):
            _run_task("t-ok", "planning", llm, db_task, None)

        with _llm_counts_lock:
            assert _llm_session_counts[10] == 0

    def test_slot_released_on_exception(self):
        """LLM slot must be decremented even when the inner function raises."""
        llm = _fake_llm(llm_id=11)
        db_task = _fake_db_task(task_type="planning", task_id="t-err")
        with _llm_counts_lock:
            _llm_session_counts[11] = 1

        with patch("app.agent.scheduler._run_maestro_loop",
                   side_effect=RuntimeError("boom")):
            _run_task("t-err", "planning", llm, db_task, None)

        with _llm_counts_lock:
            assert _llm_session_counts[11] == 0

    def test_slot_never_goes_negative(self):
        """Even if the count is already 0, _run_task must not make it negative."""
        llm = _fake_llm(llm_id=12)
        db_task = _fake_db_task(task_type="planning", task_id="t-neg")
        with _llm_counts_lock:
            _llm_session_counts[12] = 0  # Simulate already-zero state

        with patch("app.agent.scheduler._run_maestro_loop", return_value=None):
            _run_task("t-neg", "planning", llm, db_task, None)

        with _llm_counts_lock:
            assert _llm_session_counts[12] >= 0

    def test_failed_task_added_to_cooldown(self):
        """A task that raises must be recorded in _failed_cooldowns."""
        llm = _fake_llm(llm_id=13)
        # Use a non-special task type to route to _run_maestro_loop
        db_task = _fake_db_task(task_type="generic", task_id="t-cool")
        _failed_cooldowns.pop("t-cool", None)
        with _llm_counts_lock:
            _llm_session_counts[13] = 1

        with patch("app.agent.scheduler._run_maestro_loop",
                   side_effect=RuntimeError("failure")):
            _run_task("t-cool", "generic", llm, db_task, None)

        assert "t-cool" in _failed_cooldowns
        assert _failed_cooldowns["t-cool"] <= time.time()
        _failed_cooldowns.pop("t-cool", None)

    def test_successful_task_not_added_to_cooldown(self):
        """A task that completes normally must NOT be recorded in _failed_cooldowns."""
        llm = _fake_llm(llm_id=14)
        db_task = _fake_db_task(task_type="generic", task_id="t-no-cool")
        _failed_cooldowns.pop("t-no-cool", None)
        with _llm_counts_lock:
            _llm_session_counts[14] = 1

        with patch("app.agent.scheduler._run_maestro_loop", return_value=None):
            _run_task("t-no-cool", "generic", llm, db_task, None)

        assert "t-no-cool" not in _failed_cooldowns

    def test_indev_task_routes_to_dev_orchestrator(self):
        """task_type='indev' must call _run_dev_orchestrator_task, not _run_maestro_loop."""
        llm = _fake_llm(llm_id=15)
        db_task = _fake_db_task(task_type="indev", task_id="t-indev")
        with _llm_counts_lock:
            _llm_session_counts[15] = 1

        with patch("app.agent.scheduler._run_dev_orchestrator_task") as mock_dev, \
             patch("app.agent.scheduler._run_maestro_loop") as mock_loop:
            _run_task("t-indev", "indev", llm, db_task, None)

        mock_dev.assert_called_once()
        mock_loop.assert_not_called()

    def test_planning_task_routes_to_planning_task(self):
        """task_type='planning' must call _run_planning_task, not maestro loop or dev orchestrator."""
        llm = _fake_llm(llm_id=16)
        db_task = _fake_db_task(task_type="planning", task_id="t-plan")
        with _llm_counts_lock:
            _llm_session_counts[16] = 1

        with patch("app.agent.scheduler._run_planning_task") as mock_plan, \
             patch("app.agent.scheduler._run_maestro_loop") as mock_loop, \
             patch("app.agent.scheduler._run_dev_orchestrator_task") as mock_dev:
            _run_task("t-plan", "planning", llm, db_task, None)

        mock_plan.assert_called_once()
        mock_loop.assert_not_called()
        mock_dev.assert_not_called()


# ===========================================================================
# _tick - dispatch filtering
# ===========================================================================

class TestTickFiltering:
    """
    _tick() imports get_all_tasks, get_task, get_llm lazily from app.database,
    and DAGResolver lazily from app.agent.dag.  Patch those source locations.
    """

    def _make_ready_task(self, task_id, task_type):
        return {"id": task_id, "type": task_type, "position": 0, "prerequisites": []}

    def test_idea_tasks_dispatched_when_in_dispatchable_types(self):
        """IDEA tasks are dispatched when 'idea' is in SCHEDULER_DISPATCHABLE_TYPES (default)."""
        ready = [self._make_ready_task("idea-1", "idea")]

        fake_task = _fake_db_task(task_id="idea-1", task_type="idea")
        fake_llm = _fake_llm(llm_id=20)

        with patch("app.database.get_all_tasks", return_value=[]):
            with patch("app.agent.dag.DAGResolver") as MockDAG:
                MockDAG.return_value.get_ready_tasks.return_value = ready
                with patch("app.database.get_task", return_value=fake_task):
                    with patch("app.database.get_llm", return_value=fake_llm):
                        with patch("app.database.count_pending_research_jobs", return_value=0):
                            with patch("app.database.get_pending_research_jobs", return_value=[]):
                                with patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:
                                    mock_thread_cls.return_value.start = lambda: None
                                    sched_mod._tick()
                                    # idea tasks are now in default dispatchable types
                                    mock_thread_cls.assert_called()

    def test_completed_tasks_not_dispatched(self):
        """Completed tasks must not be auto-dispatched."""
        ready = [self._make_ready_task("done-1", "completed")]

        with patch("app.database.get_all_tasks", return_value=[]):
            with patch("app.agent.dag.DAGResolver") as MockDAG:
                MockDAG.return_value.get_ready_tasks.return_value = ready
                with patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:
                    sched_mod._tick()
                    mock_thread_cls.assert_not_called()

    def test_already_running_task_not_redispatched(self):
        """A task already in _active_sessions with a live thread is not redispatched."""
        ready = [self._make_ready_task("t-alive", "planning")]

        barrier = threading.Barrier(2)
        live_thread = threading.Thread(target=lambda: barrier.wait())
        live_thread.start()

        with _active_sessions_lock:
            _active_sessions["t-alive"] = live_thread

        try:
            with patch("app.database.get_all_tasks", return_value=[]):
                with patch("app.agent.dag.DAGResolver") as MockDAG:
                    MockDAG.return_value.get_ready_tasks.return_value = ready
                    with patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:
                        sched_mod._tick()
                        mock_thread_cls.assert_not_called()
        finally:
            barrier.wait()
            live_thread.join()
            with _active_sessions_lock:
                _active_sessions.pop("t-alive", None)

    def test_task_in_cooldown_not_dispatched(self):
        """A task recorded in _failed_cooldowns within the window is skipped."""
        ready = [self._make_ready_task("t-cd", "planning")]
        _failed_cooldowns["t-cd"] = time.time()  # Just failed

        fake_task = _fake_db_task(task_id="t-cd", task_type="planning")

        with patch("app.database.get_all_tasks", return_value=[]):
            with patch("app.agent.dag.DAGResolver") as MockDAG:
                MockDAG.return_value.get_ready_tasks.return_value = ready
                with patch("app.database.get_task", return_value=fake_task):
                    with patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:
                        sched_mod._tick()
                        mock_thread_cls.assert_not_called()

        _failed_cooldowns.pop("t-cd", None)


# ===========================================================================
# New dispatcher routing tests
# ===========================================================================

class TestNewDispatcherRouting:
    """Test that _run_task routes new task types to the correct dispatcher."""

    def test_conceptual_review_routes_to_new_dispatcher(self):
        llm = _fake_llm(llm_id=30)
        db_task = _fake_db_task(task_type="conceptual_review", task_id="t-cr")
        with _llm_counts_lock:
            _llm_session_counts[30] = 1

        with patch("app.agent.scheduler._run_conceptual_review_task") as mock_fn, \
             patch("app.agent.scheduler._run_maestro_loop") as mock_loop:
            _run_task("t-cr", "conceptual_review", llm, db_task, None)

        mock_fn.assert_called_once()
        mock_loop.assert_not_called()

    def test_optimization_routes_to_new_dispatcher(self):
        llm = _fake_llm(llm_id=31)
        db_task = _fake_db_task(task_type="optimization", task_id="t-opt")
        with _llm_counts_lock:
            _llm_session_counts[31] = 1

        with patch("app.agent.scheduler._run_optimization_security_task") as mock_fn, \
             patch("app.agent.scheduler._run_maestro_loop") as mock_loop:
            _run_task("t-opt", "optimization", llm, db_task, None)

        mock_fn.assert_called_once()
        mock_loop.assert_not_called()

    def test_full_review_routes_to_new_dispatcher(self):
        llm = _fake_llm(llm_id=32)
        db_task = _fake_db_task(task_type="full_review", task_id="t-fr")
        with _llm_counts_lock:
            _llm_session_counts[32] = 1

        with patch("app.agent.scheduler._run_full_review_task") as mock_fn, \
             patch("app.agent.scheduler._run_maestro_loop") as mock_loop:
            _run_task("t-fr", "full_review", llm, db_task, None)

        mock_fn.assert_called_once()
        mock_loop.assert_not_called()


# ===========================================================================
# _run_conceptual_review_task unit test
# ===========================================================================

class TestRunConceptualReviewTask:
    def test_advances_to_optimization_on_passed(self, monkeypatch):
        from app.agent.scheduler import _run_conceptual_review_task

        fake_task = _fake_db_task(task_id="cr-1", task_type="conceptual_review")
        fake_planning = MagicMock()
        fake_planning.file_manifest = "[]"
        fake_planning.dependency_graph = "{}"
        fake_planning.implementation_steps = "[]"
        fake_planning.test_strategy = "[]"

        updated_types = []

        def fake_update_task(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.get_planning_result", return_value=fake_planning), \
             patch("app.database.update_task", side_effect=fake_update_task), \
             patch("app.database.create_transition_result"), \
             patch("app.agent.tools.set_task_git_cwd"), \
             patch("app.agent.conceptual_review.run_conceptual_review",
                   return_value={"outcome": "passed", "votes": [], "total_prompt_tokens": 0, "total_completion_tokens": 0}):
            _run_conceptual_review_task("cr-1", "http://localhost:8008/v1", "model")

        assert "optimization" in updated_types

    def test_demotes_to_indev_on_failed(self, monkeypatch):
        from app.agent.scheduler import _run_conceptual_review_task

        fake_task = _fake_db_task(task_id="cr-2", task_type="conceptual_review")
        fake_planning = MagicMock()
        fake_planning.file_manifest = "[]"
        fake_planning.dependency_graph = "{}"
        fake_planning.implementation_steps = "[]"
        fake_planning.test_strategy = "[]"

        updated_types = []

        def fake_update_task(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.get_planning_result", return_value=fake_planning), \
             patch("app.database.update_task", side_effect=fake_update_task), \
             patch("app.database.create_transition_result"), \
             patch("app.agent.tools.set_task_git_cwd"), \
             patch("app.agent.scheduler._record_demotion_inline"), \
             patch("app.agent.conceptual_review.run_conceptual_review",
                   return_value={"outcome": "failed", "votes": [], "summary": "Issues found", "total_prompt_tokens": 0, "total_completion_tokens": 0}):
            _run_conceptual_review_task("cr-2", "http://localhost:8008/v1", "model")

        assert "indev" in updated_types


# ===========================================================================
# _run_optimization_security_task unit tests
# ===========================================================================

class TestRunOptimizationSecurityTask:
    _OPT_PASS = {
        "outcome": "optimized",
        "total_prompt_tokens": 10,
        "total_completion_tokens": 20,
    }
    _SEC_PASS = {
        "outcome": "passed",
        "demotion_target": None,
        "summary": "",
        "total_prompt_tokens": 0,
        "total_completion_tokens": 0,
    }

    def _common_patches(self, task_id, sec_result):
        """Return list of patch context managers for shared fixtures."""
        return [
            patch("app.database.get_task", return_value=_fake_db_task(task_id=task_id, task_type="optimization")),
            patch("app.database.create_transition_result", MagicMock()),
            patch("app.agent.tools.set_task_git_cwd", MagicMock()),
            patch("app.agent.optimization.run_optimization_pipeline",
                  return_value=self._OPT_PASS),
            patch("app.agent.security_review.run_security_pipeline",
                  return_value=sec_result),
        ]

    def test_advances_to_full_review_on_security_pass(self):
        from app.agent.scheduler import _run_optimization_security_task

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="opt-1", task_type="optimization")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.optimization.run_optimization_pipeline",
                   return_value=self._OPT_PASS), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value=self._SEC_PASS):
            _run_optimization_security_task("opt-1", "http://localhost:8008/v1", "model")

        assert "full_review" in updated_types

    def test_demotes_on_security_rejected(self):
        from app.agent.scheduler import _run_optimization_security_task

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        sec_fail = {"outcome": "rejected", "demotion_target": "indev",
                    "summary": "vuln found", "total_prompt_tokens": 0, "total_completion_tokens": 0}

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="opt-2")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.optimization.run_optimization_pipeline",
                   return_value=self._OPT_PASS), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value=sec_fail), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_optimization_security_task("opt-2", "http://localhost:8008/v1", "model")

        assert "indev" in updated_types
        assert "full_review" not in updated_types

    def test_demotion_target_respected(self):
        from app.agent.scheduler import _run_optimization_security_task

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        sec_opt_demotion = {
            "outcome": "rejected", "demotion_target": "optimization",
            "summary": "", "total_prompt_tokens": 0, "total_completion_tokens": 0,
        }

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="opt-3")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.optimization.run_optimization_pipeline",
                   return_value=self._OPT_PASS), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value=sec_opt_demotion), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_optimization_security_task("opt-3", "http://localhost:8008/v1", "model")

        assert "optimization" in updated_types
        assert "full_review" not in updated_types

    def test_transition_result_recorded(self):
        from app.agent.scheduler import _run_optimization_security_task

        mock_create_tr = MagicMock()

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="opt-4")), \
             patch("app.database.update_task", MagicMock()), \
             patch("app.database.create_transition_result", mock_create_tr), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.optimization.run_optimization_pipeline",
                   return_value=self._OPT_PASS), \
             patch("app.agent.security_review.run_security_pipeline",
                   return_value=self._SEC_PASS):
            _run_optimization_security_task("opt-4", "http://localhost:8008/v1", "model")

        mock_create_tr.assert_called_once()
        call_kwargs = mock_create_tr.call_args.kwargs
        assert call_kwargs.get("transition") == "security_review"
        assert call_kwargs.get("outcome") == "passed"


# ===========================================================================
# _run_full_review_task unit tests
# ===========================================================================

class TestRunFullReviewTask:
    _FR_PASS = {
        "outcome": "passed", "demotion_target": None, "summary": "",
        "total_prompt_tokens": 10, "total_completion_tokens": 20, "votes": [],
    }
    _FR_FAIL = {
        "outcome": "rejected", "demotion_target": "indev", "summary": "bad",
        "total_prompt_tokens": 10, "total_completion_tokens": 20, "votes": [],
    }

    def test_full_review_failed_demotes(self):
        from app.agent.scheduler import _run_full_review_task

        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        mock_record = MagicMock()

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="fr-1", task_type="full_review")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value=self._FR_FAIL), \
             patch("app.agent.scheduler._record_demotion_inline", mock_record), \
             patch("app.agent.scheduler._check_completion_rollup_inline", MagicMock()):
            _run_full_review_task("fr-1", "http://localhost:8008/v1", "model")

        assert "indev" in updated_types
        mock_record.assert_called()

    def test_virtual_passed_records_ready_for_review(self):
        """Virtual merge success → append_task_history with 'ready_for_review'."""
        from app.agent.scheduler import _run_full_review_task
        from app.agent.merge import MergeResult

        mock_append = MagicMock()

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="fr-2", task_type="full_review")), \
             patch("app.database.update_task", MagicMock()), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.database.append_task_history", mock_append), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value=self._FR_PASS), \
             patch("app.agent.merge.execute_merge",
                   return_value=MergeResult(task_id="fr-2", status="virtual_passed")), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_full_review_task("fr-2", "http://localhost:8008/v1", "model")

        mock_append.assert_called_once()
        assert mock_append.call_args[0][1] == "ready_for_review"

    def test_conflict_logs_merge_test_failed(self):
        """Virtual merge conflict → append_task_history with 'merge_test_failed'; no type change."""
        from app.agent.scheduler import _run_full_review_task
        from app.agent.merge import MergeResult

        mock_append = MagicMock()
        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="fr-3", task_type="full_review")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.database.append_task_history", mock_append), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value=self._FR_PASS), \
             patch("app.agent.merge.execute_merge",
                   return_value=MergeResult(task_id="fr-3", status="conflict")), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_full_review_task("fr-3", "http://localhost:8008/v1", "model")

        mock_append.assert_called_once()
        assert mock_append.call_args[0][1] == "merge_test_failed"
        assert "indev" not in updated_types

    def test_test_failure_logs_merge_test_failed(self):
        """Virtual merge test_failure → append_task_history with 'merge_test_failed'; no type change."""
        from app.agent.scheduler import _run_full_review_task
        from app.agent.merge import MergeResult

        mock_append = MagicMock()
        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="fr-4", task_type="full_review")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.database.append_task_history", mock_append), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value=self._FR_PASS), \
             patch("app.agent.merge.execute_merge",
                   return_value=MergeResult(task_id="fr-4", status="test_failure")), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_full_review_task("fr-4", "http://localhost:8008/v1", "model")

        mock_append.assert_called_once()
        assert mock_append.call_args[0][1] == "merge_test_failed"
        assert "indev" not in updated_types

    def test_push_failure_logs_merge_test_failed(self):
        """Virtual merge push_failure → append_task_history with 'merge_test_failed'; no type change."""
        from app.agent.scheduler import _run_full_review_task
        from app.agent.merge import MergeResult

        mock_append = MagicMock()
        updated_types = []

        def _capture(task_id, **kwargs):
            if "type" in kwargs:
                updated_types.append(kwargs["type"])

        with patch("app.database.get_task", return_value=_fake_db_task(task_id="fr-5", task_type="full_review")), \
             patch("app.database.update_task", side_effect=_capture), \
             patch("app.database.create_transition_result", MagicMock()), \
             patch("app.database.get_project_path", return_value=None), \
             patch("app.database.append_task_history", mock_append), \
             patch("app.agent.tools.set_task_git_cwd", MagicMock()), \
             patch("app.agent.full_review.run_full_review_pipeline",
                   return_value=self._FR_PASS), \
             patch("app.agent.merge.execute_merge",
                   return_value=MergeResult(task_id="fr-5", status="push_failure")), \
             patch("app.agent.scheduler._record_demotion_inline", MagicMock()):
            _run_full_review_task("fr-5", "http://localhost:8008/v1", "model")

        mock_append.assert_called_once()
        assert mock_append.call_args[0][1] == "merge_test_failed"
        assert "indev" not in updated_types


# ===========================================================================
# _check_completion_rollup_inline unit tests
# ===========================================================================

class TestCheckCompletionRollupInline:
    def _rollup_task(self, task_id, parent_task_id=None, task_type="completed"):
        t = MagicMock()
        t.id = task_id
        t.type = task_type
        t.parent_task_id = parent_task_id
        return t

    def test_no_parent_is_noop(self):
        from app.agent.scheduler import _check_completion_rollup_inline

        task = self._rollup_task("t1", parent_task_id=None)
        mock_update = MagicMock()

        with patch("app.database.get_task", return_value=task), \
             patch("app.database.update_task", mock_update):
            _check_completion_rollup_inline("t1")

        mock_update.assert_not_called()

    def test_not_all_children_done_noop(self):
        from app.agent.scheduler import _check_completion_rollup_inline

        task = self._rollup_task("t1", parent_task_id="p1")
        parent = self._rollup_task("p1", parent_task_id=None)
        child_done = self._rollup_task("t1", task_type="completed")
        child_indev = self._rollup_task("t2", task_type="indev")
        mock_update = MagicMock()

        def _get_task(task_id):
            return {"t1": task, "p1": parent}.get(task_id)

        with patch("app.database.get_task", side_effect=_get_task), \
             patch("app.database.get_active_child_tasks", return_value=[child_done, child_indev]), \
             patch("app.database.update_task", mock_update):
            _check_completion_rollup_inline("t1")

        mock_update.assert_not_called()

    def test_all_children_completed_marks_parent(self):
        from app.agent.scheduler import _check_completion_rollup_inline

        task = self._rollup_task("t1", parent_task_id="p1")
        parent = self._rollup_task("p1", parent_task_id=None)
        child1 = self._rollup_task("t1", task_type="completed")
        child2 = self._rollup_task("t2", task_type="completed")
        mock_update = MagicMock()

        def _get_task(task_id):
            return {"t1": task, "p1": parent}.get(task_id)

        with patch("app.database.get_task", side_effect=_get_task), \
             patch("app.database.get_active_child_tasks", return_value=[child1, child2]), \
             patch("app.database.update_task", mock_update):
            _check_completion_rollup_inline("t1")

        mock_update.assert_called_once_with("p1", type="completed")

    def test_recursive_grandparent_rollup(self):
        from app.agent.scheduler import _check_completion_rollup_inline

        task = self._rollup_task("t1", parent_task_id="p1")
        parent = self._rollup_task("p1", parent_task_id="gp1")
        grandparent = self._rollup_task("gp1", parent_task_id=None)
        child_of_p = [self._rollup_task("t1", task_type="completed")]
        child_of_gp = [self._rollup_task("p1", task_type="completed")]
        mock_update = MagicMock()

        def _get_task(task_id):
            return {"t1": task, "p1": parent, "gp1": grandparent}.get(task_id)

        def _get_children(parent_id):
            if parent_id == "p1":
                return child_of_p
            if parent_id == "gp1":
                return child_of_gp
            return []

        with patch("app.database.get_task", side_effect=_get_task), \
             patch("app.database.get_active_child_tasks", side_effect=_get_children), \
             patch("app.database.update_task", mock_update):
            _check_completion_rollup_inline("t1")

        assert mock_update.call_count == 2
        update_ids = [c.args[0] for c in mock_update.call_args_list]
        assert "p1" in update_ids
        assert "gp1" in update_ids


# ===========================================================================
# _rescue_stale_jobs
# ===========================================================================

class TestJobRescue:
    def test_rescue_orphaned_arch_gen_job(self):
        """Orphaned 'running' arch_gen job should be marked as 'failed'."""
        job = MagicMock()
        job.id = 123
        job.status = 'running'

        # Mock database functions
        with patch("app.database.get_retriable_arch_gen_jobs", return_value=[job]), \
             patch("app.database.get_retriable_file_summary_jobs", return_value=[]), \
             patch("app.database.get_retriable_research_jobs", return_value=[]), \
             patch("app.database.update_arch_gen_job") as mock_update:

            _rescue_stale_jobs()

            mock_update.assert_called_once()
            args, kwargs = mock_update.call_args
            assert args[0] == 123
            assert kwargs['status'] == 'failed'
            assert "Orphaned" in kwargs['error_message']

    def test_rescue_live_arch_gen_job(self):
        """Live 'running' arch_gen job should NOT be rescued."""
        job = MagicMock()
        job.id = 123
        job.status = 'running'

        session_key = f"arch-gen-123"
        with _active_sessions_lock:
            _active_sessions[session_key] = MagicMock() # Represent a live thread

        try:
            # Mock database functions
            with patch("app.database.get_retriable_arch_gen_jobs", return_value=[job]), \
                 patch("app.database.get_retriable_file_summary_jobs", return_value=[]), \
                 patch("app.database.get_retriable_research_jobs", return_value=[]), \
                 patch("app.database.update_arch_gen_job") as mock_update:

                _rescue_stale_jobs()

                mock_update.assert_not_called()
        finally:
            with _active_sessions_lock:
                _active_sessions.pop(session_key, None)

    def test_rescue_failed_arch_gen_job_after_cooldown(self):
        """Cooled down 'failed' arch_gen job should be reset to 'pending'."""
        job = MagicMock()
        job.id = 456
        job.status = 'failed'
        job.retry_count = 0

        # Mock database functions
        with patch("app.database.get_retriable_arch_gen_jobs", return_value=[job]), \
             patch("app.database.get_retriable_file_summary_jobs", return_value=[]), \
             patch("app.database.get_retriable_research_jobs", return_value=[]), \
             patch("app.database.update_arch_gen_job") as mock_update:

            _rescue_stale_jobs()

            mock_update.assert_called_once_with(456, status='pending', completed_at=None, retry_count=1)

    def test_rescue_orphaned_research_job(self):
        """Orphaned 'running' research job should be marked as 'failed' with findings."""
        job = MagicMock()
        job.id = 789
        job.status = 'running'

        # Mock database functions
        with patch("app.database.get_retriable_research_jobs", return_value=[job]), \
             patch("app.database.get_retriable_file_summary_jobs", return_value=[]), \
             patch("app.database.get_retriable_arch_gen_jobs", return_value=[]), \
             patch("app.database.update_research_job") as mock_update:

            _rescue_stale_jobs()

            mock_update.assert_called_once()
            args, kwargs = mock_update.call_args
            assert args[0] == 789
            assert kwargs['status'] == 'failed'
            assert "Orphaned" in kwargs['findings']

    def test_rescue_orphaned_file_summary_job(self):
        """Orphaned 'running' file_summary job should be marked as 'failed'."""
        job = MagicMock()
        job.id = 111
        job.status = 'running'

        # Mock database functions
        with patch("app.database.get_retriable_file_summary_jobs", return_value=[job]), \
             patch("app.database.get_retriable_research_jobs", return_value=[]), \
             patch("app.database.get_retriable_arch_gen_jobs", return_value=[]), \
             patch("app.database.update_file_summary_job") as mock_update:

            _rescue_stale_jobs()

            mock_update.assert_called_once()
            args, kwargs = mock_update.call_args
            assert args[0] == 111
            assert kwargs['status'] == 'failed'
            assert "Orphaned" in kwargs['error_message']


# ===========================================================================
# Project Failure Throttling
# ===========================================================================

class TestProjectFailureThrottling:
    def test_rescue_marks_project_as_failed(self):
        job = MagicMock()
        job.id = 1
        job.status = 'running'
        job.project = 'ProjectX'

        from app.agent.scheduler import _project_failure_cooldowns
        _project_failure_cooldowns.clear()

        with patch("app.database.get_retriable_arch_gen_jobs", return_value=[job]), \
             patch("app.database.get_retriable_file_summary_jobs", return_value=[]), \
             patch("app.database.get_retriable_research_jobs", return_value=[]), \
             patch("app.database.update_arch_gen_job"):
            _rescue_stale_jobs()
            assert 'ProjectX' in _project_failure_cooldowns

    def test_dispatch_skips_throttled_project(self):
        from app.agent.scheduler import _project_failure_cooldowns, _dispatch_arch_gen_jobs
        _project_failure_cooldowns.clear()
        _project_failure_cooldowns['ProjectX'] = time.time()

        job = MagicMock()
        job.id = 1
        job.project = 'ProjectX'
        job.llm_id = 1

        with patch("app.database.get_pending_arch_gen_jobs", return_value=[job]), \
             patch("app.database.get_llm"), \
             patch("app.agent.scheduler.threading.Thread") as mock_thread:
            _dispatch_arch_gen_jobs(None, {}, {}, {}, {})
            mock_thread.assert_not_called()
        
        _project_failure_cooldowns.clear()

