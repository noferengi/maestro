description = "Fix stale allowed_tools names in built-in agent definitions after tool rename pass (search_files, run_pytest, run_mypy, etc.)"

"""
Fix stale allowed_tools names in five built-in agent definitions.

Migration 0079 seeded these rows with tool names that pre-date the rename
pass. `build_tool_schemas` silently drops unknowns, so the affected agents
were running with an empty tool set for those capabilities.

Stale → current renames applied here:
  search_files      → find_in_files
  run_pytest        → run_test_pytest
  run_mypy          → run_check_mypy
  run_ruff          → run_check_ruff
  run_black_check   → run_check_black
  run_bandit        → run_audit_bandit
  run_pip_audit     → run_audit_pip
  git_restore       → write_git_restore
  git_add           (removed — no equivalent; write_git_commit does git add -A)
  git_unstage       (removed — no equivalent)
"""

import json as _json

_CORRECTED: dict[str, list[str]] = {
    "implementation_agent": [
        "read_file", "write_file", "list_directory", "find_in_files",
        "run_test_pytest", "run_check_mypy", "run_check_ruff", "run_check_black",
        "write_git_restore", "submit_work", "report_tool_bug",
    ],
    "review_agent": [
        "read_file", "find_in_files", "list_directory",
    ],
    "optimization_agent": [
        "read_file", "write_file", "list_directory", "find_in_files", "run_test_pytest",
    ],
    "security_agent": [
        "read_file", "find_in_files", "list_directory", "run_audit_bandit", "run_audit_pip",
    ],
    "final_review_agent": [
        "read_file", "find_in_files", "list_directory",
    ],
}

_OLD: dict[str, list[str]] = {
    "implementation_agent": [
        "read_file", "write_file", "list_directory", "search_files",
        "run_pytest", "run_mypy", "run_ruff", "run_black_check",
        "git_add", "git_restore", "git_unstage",
        "submit_work", "report_tool_bug",
    ],
    "review_agent": [
        "read_file", "search_files", "list_directory",
    ],
    "optimization_agent": [
        "read_file", "write_file", "list_directory", "search_files", "run_pytest",
    ],
    "security_agent": [
        "read_file", "search_files", "list_directory", "run_bandit", "run_pip_audit",
    ],
    "final_review_agent": [
        "read_file", "search_files", "list_directory",
    ],
}


def _update(conn, mapping: dict[str, list[str]]) -> None:
    for name, tools in mapping.items():
        tools_json = _json.dumps(tools)
        if conn.is_postgres:
            conn.execute(
                """
                UPDATE custom_agent_definitions
                SET allowed_tools = CAST(:tools AS jsonb)
                WHERE name = :name AND is_builtin = TRUE
                """,
                {"name": name, "tools": tools_json},
            )
        else:
            conn.execute(
                """
                UPDATE custom_agent_definitions
                SET allowed_tools = :tools
                WHERE name = :name AND is_builtin = 1
                """,
                {"name": name, "tools": tools_json},
            )


def up(conn) -> None:
    _update(conn, _CORRECTED)


def down(conn) -> None:
    _update(conn, _OLD)
