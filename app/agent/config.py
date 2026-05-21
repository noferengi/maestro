"""
app/agent/config.py
-------------------
Central configuration for the Maestro agent subsystem.

Load order (highest priority wins):
  1. Environment variables  (MAESTRO_* prefix)
  2. maestro.ini            (project root)
  3. Built-in defaults      (hardcoded below)

All other modules import from here - never hard-code tuneable values.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if it exists
load_dotenv()

# ---------------------------------------------------------------------------
# Locate and parse maestro.ini
# ---------------------------------------------------------------------------

_PROJECT_ROOT_FALLBACK = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..")
)

_INI_PATH = os.path.join(
    os.getenv("MAESTRO_PROJECT_ROOT", _PROJECT_ROOT_FALLBACK),
    "maestro.ini",
)

_cfg = configparser.ConfigParser(
    # Allow : in values (URLs) without treating it as a delimiter
    delimiters=("=",),
    # Keep percent signs literal - our warning messages use %%
    interpolation=None,
)
# Preserve case in keys
_cfg.optionxform = str  # type: ignore[assignment]
_cfg.read(_INI_PATH, encoding="utf-8")


def _get(section: str, key: str, env_var: str | None, fallback: str) -> str:
    """Resolve a config value: env → ini → fallback."""
    if env_var:
        env_val = os.getenv(env_var)
        if env_val is not None:
            return env_val
    return _cfg.get(section, key, fallback=fallback)


def _getint(section: str, key: str, env_var: str | None, fallback: int) -> int:
    return int(_get(section, key, env_var, str(fallback)))


def _getfloat(section: str, key: str, env_var: str | None, fallback: float) -> float:
    return float(_get(section, key, env_var, str(fallback)))


def _getbool(section: str, key: str, env_var: str | None, fallback: bool) -> bool:
    raw = _get(section, key, env_var, str(fallback)).strip().lower()
    return raw in ("true", "1", "yes", "on")


def _getlist(section: str, key: str, fallback: str) -> list[str]:
    """Parse a comma-separated list from the INI (no env override)."""
    raw = _cfg.get(section, key, fallback=fallback)
    return [item.strip() for item in raw.split(",") if item.strip()]


# ===========================================================================
# LLM / API settings
# ===========================================================================

LLM_BASE_URL: str = _get("llm", "base_url", "MAESTRO_LLM_BASE_URL", "http://localhost:8008/v1")
LLM_MODEL: str = _get("llm", "model", "MAESTRO_LLM_MODEL", "omnicoder-9b")
MAX_TOKENS_PER_TURN: int = _getint("llm", "max_tokens_per_turn", "MAESTRO_MAX_TOKENS", 32768)
LLM_TIMEOUT_SECONDS: int = _getint("llm", "timeout_seconds", "MAESTRO_LLM_TIMEOUT", 120)

# ===========================================================================
# Search settings
# ===========================================================================

SEARCH_PROVIDER: str = _get("search", "provider", "MAESTRO_SEARCH_PROVIDER", "duckduckgo")
BRAVE_API_KEY: str = _get("search", "brave_api_key", "BRAVE_API_KEY", "")
TAVILY_API_KEY: str = _get("search", "tavily_api_key", "TAVILY_API_KEY", "")

# ===========================================================================
# Loop safety limits
# ===========================================================================

MAX_TURNS: int = _getint("loop", "max_turns", "MAESTRO_MAX_TURNS", 200)
MAX_CONSECUTIVE_ERRORS: int = _getint("loop", "max_consecutive_errors", None, 3)
MAX_TASK_RETRIES: int = _getint("loop", "max_task_retries", None, 3)

# ===========================================================================
# Custom agent defaults
# ===========================================================================

CUSTOM_AGENT_DEFAULT_MAX_TURNS: int = _getint(
    "custom_agent", "default_max_turns", "MAESTRO_CUSTOM_AGENT_DEFAULT_MAX_TURNS", 200
)

# ===========================================================================
# Shell
# ===========================================================================

SHELL_TIMEOUT_SECONDS: int = _getint("shell", "timeout_seconds", "MAESTRO_SHELL_TIMEOUT", 600)

# ===========================================================================
# Filesystem paths
# ===========================================================================

PROJECT_ROOT: str = _get("paths", "project_root", "MAESTRO_PROJECT_ROOT", "") or _PROJECT_ROOT_FALLBACK
ARCHIVE_DIR: str = os.path.join(
    PROJECT_ROOT,
    _get("paths", "archive_dir", None, ".archive"),
)

# ===========================================================================
# Git settings
# ===========================================================================

GIT_SAFETY_BRANCH_PREFIX: str = _get("git", "branch_prefix", None, "maestro/task-")
GIT_ALLOWED_BASE_BRANCHES: list[str] = _getlist("git", "allowed_base_branches", "main, master")


def _resolve_git_root(path: str) -> str | None:
    """
    Return the absolute, normalised git repository root that contains *path*,
    or None if *path* is not inside any git repository.
    """
    import subprocess as _sp
    try:
        result = _sp.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return os.path.normcase(os.path.normpath(result.stdout.strip()))
    except Exception:
        pass
    return None


MAESTRO_GIT_ROOT: str | None = _resolve_git_root(PROJECT_ROOT)

# ===========================================================================
# Database settings
# ===========================================================================

USE_POSTGRES: bool = _getbool("database", "use_postgres", "MAESTRO_USE_POSTGRES", False)

if not USE_POSTGRES:
    raise RuntimeError(
        "SQLite is no longer supported. "
        "Set MAESTRO_USE_POSTGRES=true and MAESTRO_DATABASE_URL in .env."
    )

# Production database (app user — no DDL privileges)
DATABASE_URL: str = _get("database", "url", "MAESTRO_DATABASE_URL", "postgresql://localhost/maestro_db")
# Admin database (DDL privileges — used only by migration runner)
ADMIN_DATABASE_URL: str = _get("database", "admin_url", "MAESTRO_ADMIN_DATABASE_URL", DATABASE_URL)

# Test database — separate PostgreSQL DB on the same server.
# Used when MAESTRO_TEST=1 is set (by the test suite).
TEST_DATABASE_URL: str = os.environ.get("MAESTRO_TEST_DATABASE_URL", "")
TEST_ADMIN_DATABASE_URL: str = os.environ.get("MAESTRO_TEST_ADMIN_DATABASE_URL", TEST_DATABASE_URL)

# ===========================================================================
# Agent status values
# ===========================================================================

STATUS_PENDING: str = "PENDING"
STATUS_ACTIVE: str = "ACTIVE"
STATUS_VERIFYING: str = "VERIFYING"
STATUS_ACCEPTED: str = "ACCEPTED"
STATUS_REJECTED: str = "REJECTED"

SIGNAL_REVERT: str = "REVERT_TO_DESIGN"
SIGNAL_ACCEPTED: str = "ACCEPTED"
SIGNAL_REJECTED: str = "REJECTED"
SIGNAL_NEEDS_HUMAN: str = "NEEDS_HUMAN"
SIGNAL_CONSULT: str = "CONSULT"
SIGNAL_NEEDS_RESEARCH: str = "NEEDS_RESEARCH"
SIGNAL_CONTEXT_TOO_LARGE: str = "CONTEXT_TOO_LARGE"

# ===========================================================================
# Intake pipeline settings
# ===========================================================================

RESEARCH_AGENT_MAX_LIVES: int = _getint("intake", "research_agent_max_lives", "MAESTRO_RESEARCH_LIVES", 3)
RESEARCH_AGENT_MAX_TURNS_PER_LIFE: int = _getint("intake", "research_agent_max_turns", None, 100)
RESEARCH_CONTEXT_BUDGET_RATIO: float = _getfloat("intake", "context_budget_ratio", None, 0.60)
TIEBREAKER_ENABLED: bool = _getbool("intake", "tiebreaker_enabled", None, True)

RESEARCH_AGENT_TOOLS: list[str] = _getlist("intake", "research_agent_tools",
    "web_search, web_fetch, read_file, read_file_metadata, "
    "read_last_output, find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, submit_work"
    )


# ===========================================================================
# Subdivision settings
# ===========================================================================

SUBDIVISION_AGENT_MAX_TURNS: int = _getint("subdivision", "max_turns", None, 100)
SUBDIVISION_MAX_DEPTH: int = _getint("subdivision", "max_depth", None, 6)
SUBDIVISION_MAX_RETRIES: int = _getint("subdivision", "max_retries_per_level", None, 4)
SUBDIVISION_MAX_TOTAL_SUB_IDEAS: int = _getint("subdivision", "max_total_sub_ideas", None, 30)
SUBDIVISION_CONTEXT_BUDGET_RATIO: float = _getfloat("subdivision", "context_budget_ratio", None, 0.60)
SUBDIVISION_CONTEXT_AWARE_TOOLS: bool = _getbool("subdivision", "context_aware_tools", None, True)

SUBDIVISION_AGENT_TOOLS: list[str] = _getlist("subdivision", "subdivision_agent_tools",
    "read_file, read_file_metadata, "
    "read_last_output, find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, submit_work"
    )


SUBDIVISION_PLANNING_TOOLS: list[str] = _getlist("subdivision", "subdivision_planning_tools",
    "write_arch_doc, write_interface_contract, "
    "write_mermaid, spawn_research_agent, "
    "list_directory, find_files, get_task, list_tasks"
)

# ===========================================================================
# LLM capacity limits
# ===========================================================================

MIN_PARALLEL_SESSIONS: int = _getint("capacity", "min_parallel_sessions", None, 1)
MAX_PARALLEL_SESSIONS: int = _getint("capacity", "max_parallel_sessions", None, 1024)
MIN_CONTEXT_SIZE: int = _getint("capacity", "min_context_size", None, 1)
MAX_CONTEXT_SIZE: int = _getint("capacity", "max_context_size", None, 2 * 1024 * 1024)

# ===========================================================================
# Context window warnings
# ===========================================================================

CONTEXT_WARNING_ENABLED: bool = _getbool("context_warnings", "enabled", None, True)

def _build_context_thresholds() -> list[tuple[float, str]]:
    _defaults = [
        (0.50, "warn_at_50", "[SYSTEM WARNING] Used 50% context."),
        (0.75, "warn_at_75", "[SYSTEM WARNING] Used 75% context."),
        (0.90, "warn_at_90", "[SYSTEM CRITICAL] Used 90% context."),
    ]
    thresholds: list[tuple[float, str]] = []
    for pct, prefix, default_msg in _defaults:
        if _getbool("context_warnings", f"{prefix}_enabled", None, True):
            msg = _get("context_warnings", f"{prefix}_message", None, default_msg)
            thresholds.append((pct, msg))
    return thresholds

CONTEXT_WARNING_THRESHOLDS: list[tuple[float, str]] = _build_context_thresholds()
CONTEXT_TERMINATE_THRESHOLD: float = _getfloat("context_warnings", "terminate_threshold", None, 0.95)

def check_context_saturation(
    prompt_tokens: int,
    max_context: int,
    warned_set: set,
    messages: list,
    *,
    terminate_threshold: float | None = None,
) -> bool:
    if not CONTEXT_WARNING_ENABLED or max_context <= 0 or prompt_tokens <= 0:
        return False
    if terminate_threshold is None:
        terminate_threshold = CONTEXT_TERMINATE_THRESHOLD
    saturation = prompt_tokens / max_context
    if terminate_threshold > 0 and saturation >= terminate_threshold:
        return True
    for threshold_pct, threshold_msg in CONTEXT_WARNING_THRESHOLDS:
        if saturation >= threshold_pct and threshold_pct not in warned_set:
            warned_set.add(threshold_pct)
            messages.append({"role": "user", "content": threshold_msg})
            break
    return False

# ===========================================================================
# Turn budget warnings
# ===========================================================================

TURN_WARNING_ENABLED: bool = _getbool("turn_warnings", "enabled", None, True)

def check_turn_saturation(
    current_turn: int,
    max_turns: int,
    warned_set: set,
    messages: list,
) -> bool:
    """
    Injects a system warning when the agent is running low on tool-call turns.
    Thresholds: last 25, last 5.
    """
    if not TURN_WARNING_ENABLED or max_turns <= 0:
        return False
    
    remaining = max_turns - current_turn
    # Thresholds are "remaining turns"
    thresholds = [25, 5]
    
    for t in thresholds:
        # If we have reached or dropped below the threshold, and haven't warned for it yet
        if remaining <= t and t not in warned_set:
            warned_set.add(t)
            if t <= 5:
                msg = (
                    f"[SYSTEM WARNING — CRITICAL] You have {remaining} tool-call turns remaining. "
                    "You MUST call submit_work NOW. "
                    "If your implementation is complete and tests pass, call submit_work(signal='ACCEPTED', summary='...'). "
                    "If you cannot finish in time, call submit_work(signal='REVERT_TO_DESIGN', summary='...reason...') "
                    "rather than being cut off silently. Do NOT make further exploratory tool calls."
                )
            else:
                msg = (
                    f"[SYSTEM WARNING] You have {remaining} tool-call turns remaining. "
                    "Focus on completing and validating your changes. "
                    "If you cannot finish within the remaining budget, call "
                    "submit_work(signal='REVERT_TO_DESIGN', summary='...reason...') rather than being cut off."
                )
            messages.append({"role": "user", "content": msg})
            return True
    return False

# ===========================================================================
# Verdict confidence ranges
# ===========================================================================

def _build_verdict_ranges() -> dict[str, tuple[int, int]]:
    _defaults = {
        "rejected":       (0, 50),
        "not_suitable":   (51, 60),
        "needs_research": (61, 75),
        "possible":       (76, 91),
        "likely":         (92, 100),
    }
    result: dict[str, tuple[int, int]] = {}
    for name, (dmin, dmax) in _defaults.items():
        raw = _cfg.get("verdicts", name, fallback=f"{dmin}, {dmax}")
        parts = [int(x.strip()) for x in raw.split(",")]
        result[name.upper()] = (parts[0], parts[1])
    return result

VERDICT_RANGES: dict[str, tuple[int, int]] = _build_verdict_ranges()

# ===========================================================================
# Planning pipeline
# ===========================================================================

PLANNING_BEST_OF_N: int = _getint("planning", "best_of_n", None, 5)
PLANNING_MAX_FILES: int = _getint("planning", "max_files", None, 8)
PLANNING_MAX_STEPS: int = _getint("planning", "max_steps", None, 6)
PLANNING_JUDGE_MAX_TOKENS: int = _getint("planning", "judge_max_tokens", None, 32768)
PLANNING_MAX_DESIGN_RETRIES: int = _getint("planning", "max_design_retries", None, 3)
PLANNING_MAX_REJECTIONS: int = _getint("planning", "max_rejections", None, 5)
PLANNING_SURVEY_MAX_TURNS: int = _getint("planning", "survey_max_turns", None, 100)

PLANNING_GATE_FEASIBILITY_RECHECK: bool = _getbool("planning_gate", "feasibility_recheck_enabled", None, True)
PLANNING_GATE_CONTEXT_SAFETY_MARGIN: float = _getfloat("planning_gate", "context_safety_margin", None, 0.15)

# ===========================================================================
# In-development (component loops)
# ===========================================================================

INDEV_COMPONENT_MAX_TURNS: int = _getint("indev", "component_max_turns", None, 100)
INDEV_COMPONENT_MAX_RETRIES: int = _getint("indev", "component_max_retries", None, 2)
INDEV_ENFORCE_FILE_CONTAINMENT: bool = _getbool("indev", "enforce_file_containment", None, True)
# After all batches complete but the full test suite fails, try targeted test-fix
# loops before demoting to PLANNING.  Each loop gets up to TEST_FIX_MAX_TURNS turns
# to read the failure output, edit files, and re-run tests.
INDEV_TEST_FIX_MAX_RETRIES: int = _getint("indev", "test_fix_max_retries", None, 2)
INDEV_TEST_FIX_MAX_TURNS: int = _getint("indev", "test_fix_max_turns", None, 30)

INDEV_AGENT_TOOLS: list[str] = _getlist("indev", "agent_tools",
    "read_file, read_file_metadata, read_last_output, "
    "write_file, append_file, patch_file, move_file, list_directory, "
    "find_in_files, find_files, find_symbol, find_callers, find_imports_of, write_archive, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, read_diff_stat, "
    "write_git_branch, write_git_commit, write_git_checkout, write_git_restore, "
    "get_task, list_tasks, write_task_status, write_task_history, "
    "write_arch_doc, write_mermaid, write_interface_contract, "
    "spawn_research_agent, write_benchmark, "
    "run_test_pytest, run_check_mypy, run_check_ruff, run_check_black, run_test_unittest, "
    "run_test_npm, run_test_cargo, run_test_go, read_test_summary, "
    "run_build_make, run_build_cargo, run_build_go, run_build_npm, run_build_tsc, "
    "run_build_gradle, run_build_mvn, "
    "run_deps_pip, run_deps_npm, run_deps_cargo, "
    "consult_maestro, report_tool_bug, submit_work, "
    "query_episodes, "
    "ask_agent, list_active_sessions"
)

# ===========================================================================
# Conceptual review
# ===========================================================================

CONCEPTUAL_REVIEW_MAX_TURNS: int = _getint("conceptual_review", "reviewer_max_turns", None, 100)
CONCEPTUAL_REVIEW_HIGH_SEVERITY_BLOCKS: bool = _getbool("conceptual_review", "high_severity_blocks_advance", None, True)
CONCEPTUAL_REVIEW_RESEARCH_LIVES: int = _getint("conceptual_review", "research_agent_max_lives", None, 3)

CONCEPTUAL_REVIEW_REVIEWER_TOOLS: list[str] = _getlist("conceptual_review", "reviewer_tools",
    "read_file, read_file_metadata, read_last_output, "
    "find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, report_tool_bug, submit_work, "
    "query_episodes"
    )


# ===========================================================================
# Optimization
# ===========================================================================

OPTIMIZATION_PROPOSAL_COUNT: int = _getint("optimization", "proposal_count", None, 5)
OPTIMIZATION_JUDGE_COUNT: int = _getint("optimization", "judge_count", None, 3)
OPTIMIZATION_IMPL_MAX_TURNS: int = _getint("optimization", "implementation_max_turns", None, 100)
OPTIMIZATION_MIN_IMPROVEMENT_PCT: float = _getfloat("optimization", "min_improvement_pct", None, 2.0)
OPTIMIZATION_MAX_REGRESSION_PCT: float = _getfloat("optimization", "max_regression_pct", None, 5.0)
OPTIMIZATION_MAX_REVIEWER_TURNS: int = _getint("optimization", "reviewer_max_turns", None, 100)

OPTIMIZATION_REVIEWER_TOOLS: list[str] = _getlist("optimization", "reviewer_tools",
    "read_file, read_file_metadata, read_last_output, "
    "find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, report_tool_bug, submit_work"
    )


OPTIMIZATION_COMPUTE_WEIGHT: float = _getfloat("optimization_weights", "compute_weight", None, 1.0)
OPTIMIZATION_MEMORY_WEIGHT: float = _getfloat("optimization_weights", "memory_weight", None, 0.6)
OPTIMIZATION_STORAGE_WEIGHT: float = _getfloat("optimization_weights", "storage_weight", None, 0.3)
OPTIMIZATION_READABILITY_PENALTY_MAX: float = _getfloat("optimization_weights", "readability_penalty_max", None, 0.5)
OPTIMIZATION_PREMATURE_MULTIPLIER: float = _getfloat("optimization_weights", "premature_multiplier", None, 2.0)
OPTIMIZATION_TECH_DEBT_BONUS_PCT: float = _getfloat("optimization_weights", "tech_debt_bonus_pct", None, 1.0)

BIG_O_RANKING: dict[str, int] = {
    "O(1)": 1, "O(log n)": 2, "O(n)": 3, "O(n log n)": 4,
    "O(n^2)": 5, "O(n^3)": 6, "O(2^n)": 7, "O(n!)": 8,
}
OPTIMIZATION_BIG_O_BONUS_PCT: float = _getfloat("optimization_weights", "big_o_bonus_pct", None, 10.0)

# ===========================================================================
# Security review
# ===========================================================================

SECURITY_REVIEW_VETO_POWER: bool = _getbool("security_review", "veto_power", None, True)
SECURITY_REVIEW_RESEARCH_LIVES: int = _getint("security_review", "research_agent_max_lives", None, 2)
SECURITY_REVIEW_MAX_REVIEWER_TURNS: int = _getint("security_review", "reviewer_max_turns", None, 100)

SECURITY_REVIEWER_TOOLS: list[str] = _getlist("security_review", "reviewer_tools",
    "read_file, read_file_metadata, read_last_output, "
    "find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, report_tool_bug, submit_work, "
    "run_audit_bandit, run_audit_pip, run_audit_semgrep, run_audit_npm"
)

# ===========================================================================
# Final review (AI stage before human review)
# ===========================================================================

FINAL_REVIEW_AUTO_UX: bool = _getbool("final_review", "auto_ux_review", None, True)
FINAL_REVIEW_FRONTEND_PATTERNS: list[str] = _getlist("final_review", "frontend_patterns", "app/web/*.html, app/web/*.js, app/web/*.css")
FINAL_REVIEW_RESEARCH_LIVES: int = _getint("final_review", "research_agent_max_lives", None, 2)
FINAL_REVIEW_MAX_REVIEWER_TURNS: int = _getint("final_review", "reviewer_max_turns", None, 100)

FINAL_REVIEW_CODE_QUALITY_TOOLS: list[str] = _getlist("final_review", "code_quality_reviewer_tools",
    "read_file, read_file_metadata, read_last_output, "
    "find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, report_tool_bug, submit_work, "
    "run_test_pytest, run_check_mypy, run_check_ruff, run_check_black, read_test_summary, "
    "query_episodes"
)
FINAL_REVIEW_FUNCTIONAL_TOOLS: list[str] = _getlist("final_review", "functional_reviewer_tools",
    "read_file, read_file_metadata, read_last_output, "
    "find_in_files, find_files, list_directory, "
    "read_git_status, read_git_diff, read_git_log, read_git_blame, read_git_show, "
    "get_task, list_tasks, report_tool_bug, submit_work, "
    "query_episodes"
    )


# ===========================================================================
# Merge pipeline (COMPLETED stage — deterministic git merge to main)
# ===========================================================================

MERGE_TEST_TIMEOUT: int  = _getint("merge", "test_timeout",        "MAESTRO_MERGE_TEST_TIMEOUT", 300)
MERGE_AUTO_PUSH: bool    = _getbool("merge", "auto_push",          "MAESTRO_MERGE_AUTO_PUSH",    True)
MERGE_TAG_BRANCHES: bool = _getbool("merge", "tag_merged_branches", None,                        True)
MERGE_DELETE_BRANCHES: bool = _getbool("merge", "delete_merged_branches", None,                  False)
MERGE_PUSH_RETRIES: int  = _getint("merge", "push_retries",        None,                         3)

# ===========================================================================
# Pipeline stage order and completion detection
# ===========================================================================

PIPELINE_COLUMN_ORDER: list[str] = _getlist(
    "pipeline", "column_order",
    "architecture, idea, planning, indev, conceptual_review, "
    "optimization, security, final_review, completed",
)

PIPELINE_DONE_STATUSES: frozenset[str] = frozenset(
    _getlist("pipeline", "done_statuses", "completed, accepted")
)

# ===========================================================================
# Research jobs (background + inline)
# ===========================================================================

RESEARCH_JOB_MAX_CONCURRENT: int = _getint("research_jobs", "max_concurrent", None, 3)
RESEARCH_JOB_TIMEOUT_SECONDS: int = _getint("research_jobs", "timeout_seconds", None, 300)
RESEARCH_JOB_PRIORITY_DEPTH_PENALTY: float = _getfloat("research_jobs", "depth_penalty", None, 10.0)

# ===========================================================================
# Tool behaviour limits
# ===========================================================================

TOOL_MAX_SEARCH_RESULTS: int = _getint("tools", "max_search_results", None, 200)
TOOL_MAX_GIT_LOG_ENTRIES: int = _getint("tools", "max_git_log_entries", None, 100)
GIT_TIMEOUT_SECONDS: int = _getint("tools", "git_timeout_seconds", None, 30)

SNAPSHOT_MAX_DEPTH: int = _getint("snapshot", "max_depth", None, 4)
SNAPSHOT_MAX_TOKENS: int = _getint("snapshot", "max_tokens", None, 32768)
SNAPSHOT_CACHE_TTL: int = _getint("snapshot", "cache_ttl_seconds", None, 300)
SNAPSHOT_CONTEXT_RATIO: float = _getfloat("snapshot", "context_ratio", None, 0.12)

# ===========================================================================
# Survey / Summary settings
# ===========================================================================

SUMMARY_CONTEXT_RATIO: float = _getfloat("survey", "summary_context_ratio", None, 0.10)
SUMMARY_MAX_FILE_SIZE: int = _getint("survey", "max_file_size_bytes", None, 1024 * 1024)
SURVEY_VERDICT_MAX_TOKENS: int = _getint("survey", "verdict_max_tokens", None, 32768)
SURVEY_SUMMARY_MAX_TOKENS: int = _getint("survey", "summary_max_tokens", None, 32768)
SURVEY_STALENESS_ENABLED: bool = _getbool("survey", "staleness_enabled", None, True)
SURVEY_STALENESS_CHECK_RATIO: float = _getfloat("survey", "staleness_check_ratio", None, 0.05)
SURVEY_MAX_CONCURRENT_JOBS: int = _getint("survey", "max_concurrent_scope_jobs", None, 3)
SURVEY_DIRECTORY_MAX_FILES: int = _getint("survey", "directory_max_files", None, 100)
SURVEY_MODULE_TARGET_FILES: int = _getint("survey", "module_target_files", None, 30)

TOOL_LISTING_EXCLUDED_DIRS: set[str] = set(_getlist(
    "tools", "excluded_directories",
    # 'logs' intentionally absent — log directories should be visible in listings
    # (agents may legitimately inspect them); they are excluded from auto-summarization
    # by the size cap in enqueue_file_summary, not by directory exclusion.
    ".archive, .git, venv, .venv, __pycache__, node_modules, .mypy_cache, .pytest_cache, .ruff_cache, dist, build, .eggs",
))

# ===========================================================================
# Logging
# ===========================================================================

LOG_LEVEL: str = _get("logging", "level", "MAESTRO_LOG_LEVEL", "INFO")
LOG_FILE: str = _get("logging", "log_file", "MAESTRO_LOG_FILE", "")
LOG_MAX_BYTES: int = _getint("logging", "max_bytes", None, 10 * 1024 * 1024)
LOG_BACKUP_COUNT: int = _getint("logging", "backup_count", None, 5)

# ===========================================================================
# Scheduler
# ===========================================================================

SCHEDULER_TICK_INTERVAL: float = _getfloat("scheduler", "tick_interval", None, 5.0)
SCHEDULER_ENABLED: bool = _getbool("scheduler", "enabled", None, True)
MODEL_BLOCK_TIMEOUT_MINUTES: int = _getint(
    "scheduler", "model_block_timeout_minutes", "MAESTRO_MODEL_BLOCK_TIMEOUT_MINUTES", 30
)
SCHEDULER_DISPATCHABLE_TYPES: list[str] = _getlist(
    "scheduler", "dispatchable_types",
    "idea, planning, indev, conceptual_review, optimization, security, final_review, reflection_agent"
)
FILE_SUMMARY_WAIT_TIMEOUT: float = _getfloat("scheduler", "file_summary_wait_timeout", None, 300.0)
FILE_SUMMARY_STREAM_IDLE_TIMEOUT: float = _getfloat("scheduler", "file_summary_stream_idle_timeout", None, 30.0)

# ===========================================================================
# PIP (Performance Improvement Plan) settings
# ===========================================================================

PIP_RESOLUTION_MAX_TURNS: int = _getint("pip", "resolution_max_turns", None, 100)

# ===========================================================================
# Planning Correction Agent settings
# ===========================================================================

CORRECTION_MAX_TURNS: int = _getint("correction", "max_turns", None, 100)
CORRECTION_SKIP_AFTER_FAILURES: int = _getint("correction", "correction_skip_after_failures", None, 2)

# ===========================================================================
# Maestro — autonomous project orchestrator and resurrection agent
# ===========================================================================

MAESTRO_ENABLED: bool        = _getbool("maestro", "enabled",              "MAESTRO_ORCHESTRATOR_ENABLED", False)
MAESTRO_STALL_TICKS: int     = _getint ("maestro", "stall_ticks",          None,                      60)
MAESTRO_MAX_RESURRECTIONS: int = _getint("maestro", "max_cards_to_resurrect", None,                   3)
MAESTRO_MAX_NEW_CARDS: int   = _getint ("maestro", "max_new_cards",        None,                      2)
MAESTRO_DECIDE_MAX_TOKENS: int = _getint("maestro", "decide_max_tokens",   None,                      32768)
MAESTRO_SURVEY_TOOLS: list[str] = _getlist("maestro", "survey_tools", "get_project_summary, get_directory_summary, get_module_summary, list_scope_summaries")

# ===========================================================================
# Arch Gen — architecture card population agent
# ===========================================================================

ARCH_GEN_MAX_TOKENS: int = _getint("arch_gen", "max_tokens", None, 32768)

# ===========================================================================
# Orchestration / ConsultAgent
# ===========================================================================

def _getint_optional(section: str, key: str, env_var: str | None) -> "int | None":
    """Return int from config or None if not set / empty."""
    if env_var:
        env_val = os.getenv(env_var)
        if env_val is not None and env_val.strip():
            return int(env_val.strip())
    raw = _cfg.get(section, key, fallback="").strip()
    return int(raw) if raw else None


# ===========================================================================
# Autopilot objectives
# ===========================================================================

AUTOPILOT_MAX_OBJECTIVES_PER_TICK: int = _getint("autopilot", "max_objectives_per_tick", None, 2)
AUTOPILOT_SPIN_DEMOTION_THRESHOLD: int = _getint("autopilot", "spin_demotion_threshold", None, 2)
AUTOPILOT_SPIN_CARD_THRESHOLD: int     = _getint("autopilot", "spin_card_threshold", None, 2)
AUTOPILOT_ASSESSMENT_MAX_TURNS: int    = _getint("autopilot", "assessment_max_turns", None, 5)

# ===========================================================================
# Episodic Memory (Gap 7)
# ===========================================================================

EPISODIC_MEMORY_ENABLED: bool = _getbool(
    "episodic_memory", "enabled", "MAESTRO_EPISODIC_MEMORY_ENABLED", False
)
EPISODIC_MEMORY_EMBEDDING_LLM_ID: "int | None" = _getint_optional(
    "episodic_memory", "embedding_llm_id", None
)
EPISODIC_MEMORY_EMBEDDING_DIM: int = _getint(
    "episodic_memory", "embedding_dim", None, 1536
)
EPISODIC_MEMORY_DECAY_HALF_LIFE_DAYS: int = _getint(
    "episodic_memory", "decay_half_life_days", None, 90
)
EPISODIC_MEMORY_KEEPALIVE_EXTENSION_DAYS: int = _getint(
    "episodic_memory", "keepalive_extension_days", None, 14
)
EPISODIC_MEMORY_AUTO_INJECT_K: int = _getint(
    "episodic_memory", "auto_inject_k", None, 3
)

# ===========================================================================
# Training data pipeline (Gap 11)
# ===========================================================================

TRAINING_EXPORT_THRESHOLD: int = _getint("training", "export_threshold", "MAESTRO_TRAINING_EXPORT_THRESHOLD", 100)
TRAINING_EXPORT_MAX_PER_RUN: int = _getint("training", "export_max_per_run", "MAESTRO_TRAINING_EXPORT_MAX_PER_RUN", 1000)
TRAINING_EXPORT_DIR: str = _get("training", "export_dir", "MAESTRO_TRAINING_EXPORT_DIR", "data/training_exports")
TRAINING_DEDUP_MAX: int = _getint("training", "dedup_fingerprint_max", "MAESTRO_TRAINING_DEDUP_MAX", 3)


# ===========================================================================
# Maestro capabilities (Gap 4 — autonomy toggles)
# ===========================================================================

from dataclasses import dataclass as _dataclass


@_dataclass
class MaestroCapabilities:
    """Runtime autonomy flags read from [maestro_capabilities] in maestro.ini."""
    can_create_objectives: bool
    can_complete_objectives: bool
    can_create_cards: bool
    max_objectives_per_tick: int
    can_self_modify: bool
    can_auto_merge_human_review: bool
    can_auto_merge_self_modification: bool

    @classmethod
    def from_config(cls) -> "MaestroCapabilities":
        return cls(
            can_create_objectives          = _getbool("maestro_capabilities", "can_create_objectives",          None, False),
            can_complete_objectives        = _getbool("maestro_capabilities", "can_complete_objectives",        None, True),
            can_create_cards               = _getbool("maestro_capabilities", "can_create_cards",               None, True),
            max_objectives_per_tick        = _getint( "maestro_capabilities", "max_objectives_per_tick",        None, 2),
            can_self_modify                = _getbool("maestro_capabilities", "can_self_modify",                None, False),
            can_auto_merge_human_review    = _getbool("maestro_capabilities", "can_auto_merge_human_review",    None, False),
            can_auto_merge_self_modification = _getbool("maestro_capabilities", "can_auto_merge_self_modification", None, False),
        )


MAESTRO_CAPABILITIES: MaestroCapabilities = MaestroCapabilities.from_config()

# Gap 5 — self-modification constants
SELF_MODIFICATION_PROJECT: str = "_maestro_self"
SELF_MOD_INTEGRATION_BRANCH: str = "maestro/self-improvement"
SELF_MOD_REVERT_VOTE_THRESHOLD: int = _getint("maestro_capabilities", "revert_vote_threshold", None, 3)

# ===========================================================================
# Orchestration / ConsultAgent
# ===========================================================================

ORCHESTRATION_LLM_ID: "int | None" = _getint_optional(
    "orchestration", "maestro_llm_id", "MAESTRO_ORCHESTRATOR_LLM_ID"
)
CONSULT_MAX_CALLS_PER_SESSION: int = _getint("orchestration", "consult_max_calls_per_session", None, 3)
CONSULT_AGENT_MAX_TURNS: int = _getint("orchestration", "consult_agent_max_turns", None, 5)
ASK_AGENT_MAX_DEPTH: int = _getint("orchestration", "ask_max_depth", "MAESTRO_ASK_MAX_DEPTH", 3)

# ===========================================================================
# Reflection agent
# ===========================================================================

REFLECTION_MAX_TURNS: int = _getint(
    "reflection", "max_turns", "MAESTRO_REFLECTION_MAX_TURNS", 150
)
REFLECTION_MAX_HISTORY_TURNS: int = _getint(
    "reflection", "max_history_turns", "MAESTRO_REFLECTION_MAX_HISTORY_TURNS", 20
)
REFLECTION_CONFIDENCE_THRESHOLD: float = _getfloat(
    "reflection", "confidence_threshold", "MAESTRO_REFLECTION_CONFIDENCE_THRESHOLD", 0.7
)

# ===========================================================================
# Server admin
# ===========================================================================

# When True, POST /api/admin/restart is active and the MCP restart_server tool works.
# SECURITY: this endpoint triggers a forced process exit — never enable on a
# publicly accessible server.  Default: False.  Override in maestro.ini or env.
SERVER_ALLOW_REMOTE_RESTART: bool = _getbool(
    "server", "allow_remote_restart", "MAESTRO_ALLOW_REMOTE_RESTART", False
)


# ===========================================================================
# LLM routing resolver (GAP 10)
# ===========================================================================

DEFAULT_LLM_ID: "int | None" = _getint_optional("scheduler", "default_llm_id", "MAESTRO_DEFAULT_LLM_ID")


def resolve_llm_for_task(task, stage_key: str) -> "int | None":
    """Return the llm_id to use for dispatching *task* at *stage_key*.

    Resolution order:
      1. task.llm_id if human-pinned (task.llm_pinned = True)
      2. project_llm_routing entry for stage_key
      3. project.llm_id
      4. DEFAULT_LLM_ID from ini (None if not set)
    """
    if getattr(task, 'llm_pinned', False) and task.llm_id:
        return task.llm_id

    from app.database.crud_projects import get_routing_table, get_project_by_id
    if task.project_id:
        routing = get_routing_table(task.project_id)
        if stage_key in routing:
            return routing[stage_key]
        project = get_project_by_id(task.project_id)
        if project and project.llm_id:
            return project.llm_id

    return DEFAULT_LLM_ID
