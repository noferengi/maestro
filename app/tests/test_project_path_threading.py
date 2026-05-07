"""
Tests for explicit project_path threading through pipeline entry points.

Verifies that:
1. run_shell_security / run_shell_review use an explicit project_path when given
2. Both functions fall back to the ContextVar when no explicit path is given
3. All pipeline entry points accept a project_path keyword argument
"""

from __future__ import annotations

import inspect
import unittest
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# run_shell_security
# ---------------------------------------------------------------------------

class TestRunShellSecurity(unittest.TestCase):

    def test_uses_explicit_project_path(self):
        """subprocess.run should receive the explicit cwd, not the ContextVar value."""
        from app.agent.security_review import run_shell_security

        mock_result = MagicMock()
        mock_result.stdout = "bandit ok"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            run_shell_security("python -m bandit -r . -q --no-show-progress",
                               project_path="/explicit/path")
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs["cwd"], "/explicit/path")

    def test_falls_back_to_context_var(self):
        """When project_path is None, cwd comes from the ContextVar."""
        from app.agent.security_review import run_shell_security
        from app.agent.tools import set_task_git_cwd

        set_task_git_cwd("/contextvar/path")

        mock_result = MagicMock()
        mock_result.stdout = "bandit ok"
        mock_result.stderr = ""

        try:
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                run_shell_security("python -m bandit -r . -q --no-show-progress")
                _, kwargs = mock_run.call_args
                self.assertEqual(kwargs["cwd"], "/contextvar/path")
        finally:
            set_task_git_cwd(None)

    def test_blocklist_still_enforced(self):
        """Non-allowlisted commands are rejected regardless of project_path."""
        from app.agent.security_review import run_shell_security

        result = run_shell_security("rm -rf /", project_path="/some/path")
        self.assertIn("ERROR", result)
        self.assertIn("allowlist", result)


# ---------------------------------------------------------------------------
# run_shell_review
# ---------------------------------------------------------------------------

class TestRunShellReview(unittest.TestCase):

    def test_uses_explicit_project_path(self):
        """subprocess.run should receive the explicit cwd."""
        from app.agent.final_review import run_shell_review

        mock_result = MagicMock()
        mock_result.stdout = "pytest ok"
        mock_result.stderr = ""

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            run_shell_review("python -m pytest", project_path="/explicit/path")
            _, kwargs = mock_run.call_args
            self.assertEqual(kwargs["cwd"], "/explicit/path")

    def test_falls_back_to_context_var(self):
        """When project_path is None, cwd comes from the ContextVar."""
        from app.agent.final_review import run_shell_review
        from app.agent.tools import set_task_git_cwd

        set_task_git_cwd("/contextvar/path")

        mock_result = MagicMock()
        mock_result.stdout = "pytest ok"
        mock_result.stderr = ""

        try:
            with patch("subprocess.run", return_value=mock_result) as mock_run:
                run_shell_review("python -m pytest")
                _, kwargs = mock_run.call_args
                self.assertEqual(kwargs["cwd"], "/contextvar/path")
        finally:
            set_task_git_cwd(None)


# ---------------------------------------------------------------------------
# Pipeline entry point signatures
# ---------------------------------------------------------------------------

class TestPipelineSignatures(unittest.TestCase):
    """Smoke-test that all pipeline entry points accept project_path."""

    def _has_project_path(self, func) -> bool:
        sig = inspect.signature(func)
        return "project_path" in sig.parameters

    def test_run_security_pipeline(self):
        from app.agent.security_review import run_security_pipeline
        self.assertTrue(self._has_project_path(run_security_pipeline))

    def test_run_final_review_pipeline(self):
        from app.agent.final_review import run_final_review_pipeline
        self.assertTrue(self._has_project_path(run_final_review_pipeline))

    def test_run_planning_pipeline(self):
        from app.agent.planning import run_planning_pipeline
        self.assertTrue(self._has_project_path(run_planning_pipeline))

    def test_run_planning_gate(self):
        from app.agent.planning_gate import run_planning_gate
        self.assertTrue(self._has_project_path(run_planning_gate))

    def test_run_conceptual_review(self):
        from app.agent.conceptual_review import run_conceptual_review
        self.assertTrue(self._has_project_path(run_conceptual_review))

    def test_run_optimization_pipeline(self):
        from app.agent.optimization import run_optimization_pipeline
        self.assertTrue(self._has_project_path(run_optimization_pipeline))

    def test_run_dev_orchestrator(self):
        from app.agent.dev_orchestrator import run_dev_orchestrator
        self.assertTrue(self._has_project_path(run_dev_orchestrator))


if __name__ == "__main__":
    unittest.main()
