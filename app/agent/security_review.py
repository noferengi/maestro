"""
app/agent/security_review.py
------------------------------
Allowlisted security scanner shell used by the security stage node executor and agent tools.

The SecurityPipeline class has been removed — security review is now handled by the
voting_panel node type configured in pipeline_stages. This file retains only
run_shell_security() which is called by tools.py for bandit/semgrep/pip-audit/npm-audit.
"""

from __future__ import annotations

import logging
import sys

from app.agent.config import PROJECT_ROOT
from app.agent.tools import _task_git_cwd

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowlisted security scanner shell
# ---------------------------------------------------------------------------

_SECURITY_TOOL_BUILDERS: dict = {
    "bandit":         lambda path: [sys.executable, "-m", "bandit", "-r", path],
    "safety":         lambda path: [sys.executable, "-m", "safety", "check"],
    "pip-audit":      lambda path: [sys.executable, "-m", "pip_audit"],
    "detect-secrets": lambda path: [sys.executable, "-m", "detect_secrets", "scan"],
    "semgrep":        lambda path: ["semgrep", "--config", "auto", path],
    "trivy":          lambda path: ["trivy", "fs", path],
    "npm-audit":      lambda path: ["npm", "audit", "--json"],
}


def run_shell_security(tool: str, path: str = ".", *, project_path: str | None = None) -> str:
    """Run a whitelisted security scanner with shell=False.

    tool: one of bandit | safety | pip-audit | detect-secrets | semgrep | trivy | npm-audit
    path: relative path within the project to scan (validated).
    """
    from app.agent.config import SHELL_TIMEOUT_SECONDS
    from app.agent.tools import _validate_tool_path, _run_tool_subprocess

    cwd = project_path or _task_git_cwd.get() or PROJECT_ROOT

    builder = _SECURITY_TOOL_BUILDERS.get(tool.lower().strip())
    if builder is None:
        known = ", ".join(_SECURITY_TOOL_BUILDERS)
        logger.warning("[security] run_shell_security rejected tool=%r (known: %s)", tool, known)
        return f"[security] Unknown security tool {tool!r}. Known tools: {known}"

    safe_path = _validate_tool_path(path, f"run_shell_security:{tool}")
    if safe_path is None:
        return f"[security] path {path!r} rejected"

    args = builder(safe_path)
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: {tool} timed out after {SHELL_TIMEOUT_SECONDS}s")
    return out if out else "(no output)"
