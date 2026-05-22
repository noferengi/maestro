"""
app/agent/tool_success_store.py
--------------------------------
Session-scoped, per-task store tracking which deterministic tools have been
called and whether they succeeded.

State per tool:
  None  — never called in this session
  False — called, returned failure
  True  — called, returned success

The store is in-memory only (a plain dict). It resets when reset() is called
at the start of each agent session dispatch.  There is no DB persistence:
a re-dispatched task always starts fresh, which is correct behaviour.
"""

from __future__ import annotations
import logging
import threading

logger = logging.getLogger(__name__)

# ---- Store ----------------------------------------------------------------
# {task_id: {tool_name: bool | None}}
_store: dict[str, dict[str, bool | None]] = {}
_lock = threading.Lock()


# Tools tracked. Extend this set when new deterministic-execution tools are added.
TRACKED_TOOLS: frozenset[str] = frozenset({
    # Math sandbox
    "run_lean4", "run_sympy", "run_coq",
    # Test runners
    "run_test_pytest", "run_test_unittest",
    "run_test_cargo", "run_test_go", "run_test_npm",
    # Linters / type checkers
    "run_check_mypy", "run_check_ruff", "run_check_black",
    # Build
    "run_build_tsc", "run_build_cargo", "run_build_go",
    "run_build_npm", "run_build_make", "run_build_gradle", "run_build_mvn",
    # Security audit
    "run_audit_bandit", "run_audit_pip", "run_audit_semgrep", "run_audit_npm",
})


def reset(task_id: str) -> None:
    """Clear the session record for a task. Call at session dispatch start."""
    with _lock:
        _store[task_id] = {}
    logger.debug("[tool_success_store] reset task=%s", task_id)


def record(task_id: str, tool_name: str, succeeded: bool) -> None:
    """Record a tool call result. Only tracks tools in TRACKED_TOOLS."""
    if tool_name not in TRACKED_TOOLS:
        return
    with _lock:
        if task_id not in _store:
            _store[task_id] = {}
        _store[task_id][tool_name] = succeeded
    logger.debug(
        "[tool_success_store] task=%s tool=%s succeeded=%s",
        task_id, tool_name, succeeded,
    )


def query(task_id: str, tool_name: str) -> bool | None:
    """
    Return None (never called), False (failed), or True (succeeded).
    """
    with _lock:
        return _store.get(task_id, {}).get(tool_name, None)


def query_group(task_id: str, tools: list[str]) -> bool:
    """True if at least one tool in *tools* succeeded for this session."""
    with _lock:
        rec = _store.get(task_id, {})
        return any(rec.get(t) is True for t in tools)


def get_all(task_id: str) -> dict[str, bool | None]:
    """Return a snapshot of the full record for a task."""
    with _lock:
        return dict(_store.get(task_id, {}))


# ---- Success predicates ---------------------------------------------------

# Prefix injected by _run_tool_subprocess (see tools.py Change A).
_EXIT_PREFIX = "[EXIT:"

# Sandbox tools (run_lean4 / run_sympy / run_coq) return JSON with an "ok" key.
_SANDBOX_TOOLS: frozenset[str] = frozenset({"run_lean4", "run_sympy", "run_coq"})


def infer_success(tool_name: str, result: str) -> bool:
    """
    Parse a tool's string result and return True iff the external binary
    reported success.

    Two categories:
    - Sandbox tools: result is JSON; success = {"ok": true, ...}
    - Subprocess tools: result begins with [EXIT:0] (injected by
      _run_tool_subprocess).  Any other exit code is failure.
    - Fallback: result that starts with "ERROR:" is always failure.
    """
    if result.startswith("ERROR:"):
        return False

    if tool_name in _SANDBOX_TOOLS:
        import json
        try:
            parsed = json.loads(result)
            return bool(parsed.get("ok", False))
        except Exception:
            return False

    # Subprocess tools
    if result.startswith(_EXIT_PREFIX):
        bracket = result.index("]")
        code_str = result[len(_EXIT_PREFIX):bracket]
        try:
            return int(code_str) == 0
        except ValueError:
            return False

    # No exit prefix → old format or unexpected output; treat as failure.
    return False
