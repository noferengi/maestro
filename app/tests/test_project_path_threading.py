"""
Tests for explicit project_path threading through pipeline entry points.

Verifies that:
1. run_shell_security uses an explicit project_path when given
2. run_shell_security falls back to the ContextVar when no explicit path is given
3. Active pipeline entry points accept a project_path keyword argument
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
        """_run_tool_subprocess should receive the explicit cwd, not the ContextVar value."""
        from app.agent.tools import run_shell_security

        with patch("app.agent.tools._run_tool_subprocess", return_value=(0, "bandit ok")) as mock_run:
            run_shell_security("bandit", ".", project_path="/explicit/path")
            args, kwargs = mock_run.call_args
            # Second positional arg is cwd
            self.assertEqual(args[1], "/explicit/path")

    def test_falls_back_to_context_var(self):
        """When project_path is None, cwd comes from the ContextVar."""
        from app.agent.tools import run_shell_security
        from app.agent.tools import set_task_git_cwd

        set_task_git_cwd("/contextvar/path")

        try:
            with patch("app.agent.tools._run_tool_subprocess", return_value=(0, "bandit ok")) as mock_run:
                run_shell_security("bandit", ".")
                args, kwargs = mock_run.call_args
                self.assertEqual(args[1], "/contextvar/path")
        finally:
            set_task_git_cwd(None)

    def test_unknown_tool_is_rejected(self):
        """Unknown tool names are rejected with a [security] message."""
        from app.agent.tools import run_shell_security

        result = run_shell_security("rm -rf /", project_path="/some/path")
        self.assertIn("[security]", result)
        self.assertIn("Unknown security tool", result)


# ---------------------------------------------------------------------------
# Pipeline entry point signatures
# ---------------------------------------------------------------------------

class TestPipelineSignatures(unittest.TestCase):
    """Smoke-test that all pipeline entry points accept project_path."""

    def _has_project_path(self, func) -> bool:
        sig = inspect.signature(func)
        return "project_path" in sig.parameters

    def test_run_planning_pipeline(self):
        from app.agent.planning import run_planning_pipeline
        self.assertTrue(self._has_project_path(run_planning_pipeline))

    def test_run_planning_gate(self):
        from app.agent.planning_gate import run_planning_gate
        self.assertTrue(self._has_project_path(run_planning_gate))


if __name__ == "__main__":
    unittest.main()
