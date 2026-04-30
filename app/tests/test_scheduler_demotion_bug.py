
import os
import sys
import time
from unittest.mock import MagicMock, patch
import pytest

# Add app and root to sys.path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import app.agent.scheduler as sched_mod
from app.agent.scheduler import _tick, _task_to_mini_dict

def _fake_db_task(task_id="t1", task_type="idea", llm_id=1, budget_id=1, project="TestProject"):
    task = MagicMock()
    task.id = task_id
    task.type = task_type
    task.position = 0
    task.prerequisites = []
    task.llm_id = llm_id
    task.budget_id = budget_id
    task.project = project
    task.description = "Test description"
    task.title = "Test Task"
    task.parent_task_id = None
    task.intake_exhausted_at = None
    return task

def _fake_llm(llm_id=1):
    llm = MagicMock()
    llm.id = llm_id
    llm.model = "test-model"
    llm.parallel_sessions = 2
    return llm

class TestSchedulerDemotionBug:
    @pytest.fixture(autouse=True)
    def clean_scheduler_state(self):
        from app.agent.scheduler import _active_sessions, _active_sessions_lock, _llm_session_counts, _llm_counts_lock, _failed_cooldowns
        with _active_sessions_lock:
            _active_sessions.clear()
        with _llm_counts_lock:
            _llm_session_counts.clear()
        _failed_cooldowns.clear()
        yield

    def test_idea_task_with_previous_pass_is_dispatched(self):
        """
        Verify that an IDEA task with a previous 'passed' transition result is NOW DISPATCHED.
        This confirms the fix for the demotion trap.
        """
        task_id = "idea-fixed-1"
        ready = [{"id": task_id, "type": "idea", "position": 0, "prerequisites": []}]

        fake_task = _fake_db_task(task_id=task_id, task_type="idea")
        fake_llm = _fake_llm(llm_id=1)

        # Mock a previous successful transition
        mock_transition_result = MagicMock()
        mock_transition_result.outcome = "passed"

        with patch("app.database.get_all_tasks", return_value=[]), \
             patch("app.agent.dag.DAGResolver") as MockDAG, \
             patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.get_llm", return_value=fake_llm), \
             patch("app.database.get_transition_results", return_value=[mock_transition_result]), \
             patch("app.database.budget_has_capacity", return_value=True), \
             patch("app.database.get_project_path", return_value="/tmp"), \
             patch("app.database.get_active_pip_resolution_jobs_for_task", return_value=[]), \
             patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:

            MockDAG.return_value.get_ready_tasks.return_value = ready
            mock_thread_cls.return_value.start = lambda: None

            # We need to mock a lot of other things that _tick calls
            with patch("app.agent.scheduler._dispatch_file_summary_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_arch_gen_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_scope_survey_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_pip_resolution_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_dreamer", return_value=None), \
                 patch("app.agent.scheduler._rescue_stale_jobs", return_value=None), \
                 patch("app.agent.scheduler._run_subdivision_recovery", return_value=None):

                sched_mod._tick()

            # Expect it to be DISPATCHED
            mock_thread_cls.assert_called()

    def test_idea_task_without_previous_pass_is_dispatched(self):
        """Verify that a normal IDEA task is dispatched."""
        task_id = "idea-ok-1"
        ready = [{"id": task_id, "type": "idea", "position": 0, "prerequisites": []}]

        fake_task = _fake_db_task(task_id=task_id, task_type="idea")
        fake_llm = _fake_llm(llm_id=1)

        with patch("app.database.get_all_tasks", return_value=[]), \
             patch("app.agent.dag.DAGResolver") as MockDAG, \
             patch("app.database.get_task", return_value=fake_task), \
             patch("app.database.get_llm", return_value=fake_llm), \
             patch("app.database.get_transition_results", return_value=[]), \
             patch("app.database.budget_has_capacity", return_value=True), \
             patch("app.database.get_project_path", return_value="/tmp"), \
             patch("app.database.get_active_pip_resolution_jobs_for_task", return_value=[]), \
             patch("app.agent.scheduler.threading.Thread") as mock_thread_cls:

            MockDAG.return_value.get_ready_tasks.return_value = ready
            mock_thread_cls.return_value.start = lambda: None

            with patch("app.agent.scheduler._dispatch_file_summary_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_arch_gen_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_scope_survey_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_pip_resolution_jobs", return_value=None), \
                 patch("app.agent.scheduler._dispatch_dreamer", return_value=None), \
                 patch("app.agent.scheduler._rescue_stale_jobs", return_value=None), \
                 patch("app.agent.scheduler._run_subdivision_recovery", return_value=None):

                sched_mod._tick()

            # Expect it to be dispatched
            mock_thread_cls.assert_called()
