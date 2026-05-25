"""
Tests for MaestroLoop test-gate and file-containment behaviors (restored from
the deleted ComponentLoop). Gate is opt-in via require_passing_tests=True.
"""

import pytest


class TestMaestroLoopTestGate:
    """_check_gate_for_submit enforces require_passing_tests when configured."""

    def _make_loop(self, require_passing_tests=True, file_manifest=None):
        from app.agent.loop import MaestroLoop
        return MaestroLoop(
            task_id="gate-test",
            require_passing_tests=require_passing_tests,
            file_manifest=file_manifest if file_manifest is not None else ["src/main.py"],
        )

    def _accepted_signal(self):
        return {"__maestro_terminal__": True, "signal": "ACCEPTED", "summary": "done"}

    def _rejected_signal(self):
        return {"__maestro_terminal__": True, "signal": "REJECTED", "summary": "bad"}

    def test_gate_blocks_accepted_when_no_tests_passed(self):
        """ACCEPTED is blocked when require_passing_tests=True and _tests_passed is False."""
        loop = self._make_loop(require_passing_tests=True, file_manifest=["src/main.py"])
        loop._tests_passed = False
        result = loop._check_gate_for_submit(self._accepted_signal())
        assert result is not None
        assert "gate blocked" in result.lower() or "test" in result.lower()

    def test_gate_passes_after_tests_pass(self):
        """ACCEPTED is allowed when _tests_passed=True."""
        loop = self._make_loop(require_passing_tests=True, file_manifest=["src/main.py"])
        loop._tests_passed = True
        result = loop._check_gate_for_submit(self._accepted_signal())
        assert result is None

    def test_gate_disabled_by_default(self):
        """Default MaestroLoop (require_passing_tests=False) never blocks submit_work."""
        loop = self._make_loop(require_passing_tests=False, file_manifest=["src/main.py"])
        loop._tests_passed = False
        result = loop._check_gate_for_submit(self._accepted_signal())
        assert result is None

    def test_gate_skipped_for_nontestable_files(self):
        """Gate does not block when the file manifest contains only non-source files."""
        loop = self._make_loop(
            require_passing_tests=True,
            file_manifest=["README.md", "config.yaml", "docs/spec.txt"],
        )
        loop._tests_passed = False
        result = loop._check_gate_for_submit(self._accepted_signal())
        assert result is None

    def test_rejected_signal_always_passes_gate(self):
        """REJECTED signals are never blocked by the test gate."""
        loop = self._make_loop(require_passing_tests=True, file_manifest=["src/main.py"])
        loop._tests_passed = False
        result = loop._check_gate_for_submit(self._rejected_signal())
        assert result is None


class TestIsTestableComponent:
    """_is_testable_component extension-based heuristic."""

    def test_python_file_is_testable(self):
        from app.agent.loop import _is_testable_component
        assert _is_testable_component(["app/main.py"]) is True

    def test_typescript_file_is_testable(self):
        from app.agent.loop import _is_testable_component
        assert _is_testable_component(["src/index.ts", "src/utils.ts"]) is True

    def test_markdown_only_is_not_testable(self):
        from app.agent.loop import _is_testable_component
        assert _is_testable_component(["README.md", "CHANGELOG.md"]) is False

    def test_mixed_manifest_is_testable(self):
        from app.agent.loop import _is_testable_component
        assert _is_testable_component(["README.md", "src/lib.go"]) is True

    def test_empty_manifest_is_not_testable(self):
        from app.agent.loop import _is_testable_component
        assert _is_testable_component([]) is False
