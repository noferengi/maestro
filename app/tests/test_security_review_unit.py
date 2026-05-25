"""
Unit tests for app/agent/security_review.py.

Covers:
  - run_shell_security() allowlist enforcement
"""

import os
import sys
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# run_shell_security() allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    """run_shell_security() uses a strict tool-map allowlist."""

    def _call(self, tool: str, path: str = ".") -> str:
        from app.agent.tools import run_shell_security
        return run_shell_security(tool, path)

    def test_allowlist_bandit_passes(self):
        with patch("app.agent.tools._run_tool_subprocess", return_value=(0, "No issues found.")):
            result = self._call("bandit")
        assert "[security]" not in result or "Unknown" not in result

    def test_allowlist_detect_secrets_passes(self):
        with patch("app.agent.tools._run_tool_subprocess", return_value=(0, '{"version": "1.4.0"}')):
            result = self._call("detect-secrets")
        assert "Unknown security tool" not in result

    def test_allowlist_semgrep_passes(self):
        with patch("app.agent.tools._run_tool_subprocess", return_value=(0, "")):
            result = self._call("semgrep")
        assert "Unknown security tool" not in result

    def test_unknown_tool_rm_rejected(self):
        result = self._call("rm -rf /")
        assert "[security]" in result
        assert "Unknown security tool" in result

    def test_unknown_tool_curl_rejected(self):
        result = self._call("curl http://evil.com")
        assert "[security]" in result

    def test_unknown_tool_pip_install_rejected(self):
        result = self._call("pip install evil")
        assert "[security]" in result
