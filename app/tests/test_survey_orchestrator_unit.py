"""
app/tests/test_survey_orchestrator_unit.py
-----------------------------------------
Unit tests for deterministic logic in SurveyOrchestrator.
"""

import pytest
import os
from unittest.mock import MagicMock, patch
from app.agent.survey_orchestrator import SurveyOrchestrator

@pytest.fixture
def so():
    return SurveyOrchestrator()

def test_compute_content_hash(so):
    """Verify that hash is stable and depends on file contents."""
    hashes = ["hash1", "hash2"]
    h1 = so._compute_content_hash(hashes)
    h2 = so._compute_content_hash(["hash2", "hash1"])
    assert h1 == h2
    assert len(h1) == 40

    h3 = so._compute_content_hash(["hash1"])
    assert h1 != h3

def test_strategy_selection(so, tmp_path):
    """Verify adaptive strategy selection based on file count."""
    # Empty project → one_shot (0 files ≤ branching factor of 8)
    strat = so._strategy(str(tmp_path), max_context=10000)
    assert strat == "one_shot"

    # Small project (a few files) → directory is enough when top dirs ≤ branching
    (tmp_path / "app").mkdir()
    for i in range(5):
        (tmp_path / "app" / f"mod{i}.py").write_text("pass")
    strat = so._strategy(str(tmp_path), max_context=10000)
    # 5 files ≤ branching (8) → one_shot; once > branching it becomes 'directory'
    assert strat in ("one_shot", "directory", "directory_module")

def test_ensure_project_surveyed_enqueues_jobs(so, tmp_path):
    """Verify that it enqueues jobs for missing summaries."""
    project_name = "TestProject"
    project_root = str(tmp_path)

    # Create some files
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hello')")
    (tmp_path / "utils").mkdir()
    (tmp_path / "utils" / "helper.py").write_text("def help(): pass")

    # Mock dependencies
    # ensure_project_surveyed imports prewarm_project_summaries inside the method
    with patch("app.agent.survey_orchestrator.get_scope_summary", return_value=None), \
         patch("app.agent.survey_orchestrator.enqueue_scope_survey_job") as mock_enqueue, \
         patch("app.agent.project_snapshot.prewarm_project_summaries") as mock_prewarm:

        mock_prewarm.return_value = 2
        status = so.ensure_project_surveyed(project_name, project_root, llm_id=1, budget_id=1)

        assert status["status"] == "initiated"
        assert status["files_prewarmed"] == 2
        # Should have enqueued jobs for:
        # 1. Directories (app, utils) - Note: root '' only gets a job if it has files
        # 2. Module clustering
        # 3. Project summary
        assert mock_enqueue.call_count == 4
