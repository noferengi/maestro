"""
app/agent/config.py
-------------------
Central configuration for the Maestro agent subsystem.

Load order (highest priority wins):
  1. Environment variables  (MAESTRO_* prefix)
  2. maestro.ini            (project root)
  3. Built-in defaults      (hardcoded below)

All other modules import from here — never hard-code tuneable values.
"""

from __future__ import annotations

import configparser
import os
from pathlib import Path

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
    # Keep percent signs literal — our warning messages use %%
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
MAX_TOKENS_PER_TURN: int = _getint("llm", "max_tokens_per_turn", "MAESTRO_MAX_TOKENS", 4096)
LLM_TEMPERATURE: float = _getfloat("llm", "temperature", "MAESTRO_TEMPERATURE", 0.2)
LLM_TIMEOUT_SECONDS: int = _getint("llm", "timeout_seconds", "MAESTRO_LLM_TIMEOUT", 120)

# ===========================================================================
# Search settings
# ===========================================================================

SEARCH_PROVIDER: str = _get("search", "provider", "MAESTRO_SEARCH_PROVIDER", "duckduckgo")
BRAVE_API_KEY: str = _get("search", "brave_api_key", "BRAVE_API_KEY", "")

# ===========================================================================
# Loop safety limits
# ===========================================================================

MAX_TURNS: int = _getint("loop", "max_turns", "MAESTRO_MAX_TURNS", 150)
MAX_CONSECUTIVE_ERRORS: int = _getint("loop", "max_consecutive_errors", None, 3)
MAX_TASK_RETRIES: int = _getint("loop", "max_task_retries", None, 3)

# ===========================================================================
# Shell
# ===========================================================================

SHELL_TIMEOUT_SECONDS: int = _getint("shell", "timeout_seconds", "MAESTRO_SHELL_TIMEOUT", 30)

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

    Uses ``git rev-parse --show-toplevel`` so it handles submodules, worktrees,
    and symbolic links correctly — no string-manipulation guessing.
    """
    import subprocess as _sp  # local import — config.py has no subprocess dep yet
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


# The git root of TheMaestro's own source tree.  Any agent git operation
# whose working directory resolves to this repo is unconditionally blocked.
MAESTRO_GIT_ROOT: str | None = _resolve_git_root(PROJECT_ROOT)

# ===========================================================================
# Agent status values (canonical — not user-tuneable)
# ===========================================================================

STATUS_PENDING: str = "PENDING"
STATUS_ACTIVE: str = "ACTIVE"
STATUS_VERIFYING: str = "VERIFYING"
STATUS_ACCEPTED: str = "ACCEPTED"
STATUS_REJECTED: str = "REJECTED"

SIGNAL_REVERT: str = "REVERT_TO_DESIGN"
SIGNAL_ACCEPTED: str = "ACCEPTED"
SIGNAL_NEEDS_RESEARCH: str = "NEEDS_RESEARCH"

# ===========================================================================
# Intake pipeline settings
# ===========================================================================

RESEARCH_AGENT_MAX_LIVES: int = _getint("intake", "research_agent_max_lives", "MAESTRO_RESEARCH_LIVES", 3)
RESEARCH_AGENT_MAX_TURNS_PER_LIFE: int = _getint("intake", "research_agent_max_turns", None, 20)
RESEARCH_CONTEXT_BUDGET_RATIO: float = _getfloat("intake", "context_budget_ratio", None, 0.60)
TIEBREAKER_ENABLED: bool = _getbool("intake", "tiebreaker_enabled", None, True)
INTAKE_LLM_TEMPERATURE: float = _getfloat("intake", "llm_temperature", "MAESTRO_INTAKE_TEMP", 0.1)

RESEARCH_AGENT_TOOLS: list[str] = _getlist("intake", "research_agent_tools",
    "web_search, read_file, read_file_harder, read_file_lines, count_lines, "
    "search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, "
    "get_task, list_tasks"
)

# ===========================================================================
# Subdivision settings
# ===========================================================================

SUBDIVISION_AGENT_MAX_TURNS: int = _getint("subdivision", "max_turns", None, 50)
SUBDIVISION_MAX_DEPTH: int = _getint("subdivision", "max_depth", None, 6)
SUBDIVISION_MAX_RETRIES: int = _getint("subdivision", "max_retries_per_level", None, 4)
SUBDIVISION_MAX_TOTAL_SUB_IDEAS: int = _getint("subdivision", "max_total_sub_ideas", None, 30)
SUBDIVISION_LLM_TEMPERATURE: float = _getfloat("subdivision", "llm_temperature", None, 0.3)
SUBDIVISION_CONTEXT_BUDGET_RATIO: float = _getfloat("subdivision", "context_budget_ratio", None, 0.60)
SUBDIVISION_CONTEXT_AWARE_TOOLS: bool = _getbool("subdivision", "context_aware_tools", None, True)

SUBDIVISION_AGENT_TOOLS: list[str] = _getlist("subdivision", "subdivision_agent_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, "
    "search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, "
    "get_task, list_tasks"
)

SUBDIVISION_PLANNING_TOOLS: list[str] = _getlist("subdivision", "subdivision_planning_tools",
    "generate_architecture_doc, generate_interface_contract, "
    "generate_mermaid_diagram, spawn_research_agent, "
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
    """Build the thresholds list from INI entries."""
    _defaults = [
        (0.50, "warn_at_50", (
            "[SYSTEM WARNING] You have used approximately 50% of your available "
            "context window.  Begin planning to conclude your current line of work "
            "within the remaining capacity."
        )),
        (0.75, "warn_at_75", (
            "[SYSTEM WARNING] You have used approximately 75% of your available "
            "context window.  Prioritise completing your current task.  Avoid "
            "starting new exploratory work.  Wrap up tool calls and summarise "
            "findings."
        )),
        (0.90, "warn_at_90", (
            "[SYSTEM CRITICAL] You have used approximately 90% of your available "
            "context window.  Immediately produce your final output in the required "
            "format.  Do not make additional tool calls unless absolutely necessary. "
            "Your generation will be terminated shortly."
        )),
    ]
    thresholds: list[tuple[float, str]] = []
    for pct, prefix, default_msg in _defaults:
        enabled = _getbool("context_warnings", f"{prefix}_enabled", None, True)
        if not enabled:
            continue
        msg = _get("context_warnings", f"{prefix}_message", None, default_msg)
        if msg:
            thresholds.append((pct, msg))
    return thresholds

CONTEXT_WARNING_THRESHOLDS: list[tuple[float, str]] = _build_context_thresholds()

CONTEXT_TERMINATE_THRESHOLD: float = _getfloat(
    "context_warnings", "terminate_threshold", None, 0.95
)


def check_context_saturation(
    prompt_tokens: int,
    max_context: int,
    warned_set: set,
    messages: list,
    *,
    terminate_threshold: float | None = None,
) -> bool:
    """
    Check per-call context saturation, inject nudge messages, and signal hard termination.

    Args:
        prompt_tokens:       prompt_tokens from the current LLM response (= full context size)
        max_context:         LLM max_context in tokens (0 → disabled)
        warned_set:          mutable set[float]; tracks which thresholds have fired this life/session
        messages:            conversation list to append nudge messages to
        terminate_threshold: if saturation >= this, return True (caller should break/terminate).
                             Defaults to CONTEXT_TERMINATE_THRESHOLD when None.

    Returns True if the caller should terminate immediately.
    """
    if not CONTEXT_WARNING_ENABLED or max_context <= 0 or prompt_tokens <= 0:
        return False

    if terminate_threshold is None:
        terminate_threshold = CONTEXT_TERMINATE_THRESHOLD

    saturation = prompt_tokens / max_context

    # Hard terminate check — evaluated before nudge injection
    if terminate_threshold > 0 and saturation >= terminate_threshold:
        return True

    # Nudge threshold injection — fire each level at most once per life/session
    for threshold_pct, threshold_msg in CONTEXT_WARNING_THRESHOLDS:
        if saturation >= threshold_pct and threshold_pct not in warned_set:
            warned_set.add(threshold_pct)
            messages.append({"role": "user", "content": threshold_msg})
            break  # inject at most one nudge per turn

    return False


# ===========================================================================
# Scheduler
# ===========================================================================

SCHEDULER_TICK_INTERVAL: float = _getfloat("scheduler", "tick_interval", None, 5.0)
SCHEDULER_ENABLED: bool = _getbool("scheduler", "enabled", None, True)
SCHEDULER_DISPATCHABLE_TYPES: list[str] = _getlist(
    "scheduler", "dispatchable_types",
    "idea, planning, indev, conceptual_review, optimization, full_review"
)

# ===========================================================================
# Verdict confidence ranges
# ===========================================================================

def _build_verdict_ranges() -> dict[str, tuple[int, int]]:
    """Parse verdict ranges from INI or use defaults."""
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
PLANNING_TEMPERATURE_SPREAD: list[float] = [
    float(x.strip())
    for x in _get("planning", "temperature_spread", None, "0.3, 0.4, 0.5, 0.6, 0.7").split(",")
    if x.strip()
]
PLANNING_JUDGE_TEMPERATURE: float = _getfloat("planning", "judge_temperature", None, 0.1)
PLANNING_MAX_DESIGN_RETRIES: int = _getint("planning", "max_design_retries", None, 3)
PLANNING_SURVEY_MAX_TURNS: int = _getint("planning", "survey_max_turns", None, 50)
PLANNING_LLM_TEMPERATURE: float = _getfloat("planning", "llm_temperature", None, 0.2)

# ===========================================================================
# Planning gate
# ===========================================================================

PLANNING_GATE_FEASIBILITY_RECHECK: bool = _getbool("planning_gate", "feasibility_recheck_enabled", None, True)
PLANNING_GATE_CONTEXT_SAFETY_MARGIN: float = _getfloat("planning_gate", "context_safety_margin", None, 0.15)

# ===========================================================================
# In-development (component loops)
# ===========================================================================

INDEV_COMPONENT_MAX_TURNS: int = _getint("indev", "component_max_turns", None, 50)
INDEV_COMPONENT_MAX_RETRIES: int = _getint("indev", "component_max_retries", None, 2)
INDEV_LLM_TEMPERATURE: float = _getfloat("indev", "llm_temperature", None, 0.2)
INDEV_ENFORCE_FILE_CONTAINMENT: bool = _getbool("indev", "enforce_file_containment", None, True)

# [indev]
INDEV_AGENT_TOOLS: list[str] = _getlist(
    "indev", "agent_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, write_file, append_file, list_directory, "
    "search_files, find_files, archive_file, "
    "git_status, git_diff, git_log, git_blame, git_show, "
    "git_create_branch, git_commit, git_checkout, "
    "get_task, list_tasks, update_task_status, append_task_history, "
    "generate_architecture_doc, generate_mermaid_diagram, generate_interface_contract, "
    "spawn_research_agent, record_benchmark, run_shell_indev, run_shell_security, run_shell_review"
)

# ===========================================================================
# Conceptual review
# ===========================================================================

CONCEPTUAL_REVIEW_MAX_TURNS: int = _getint("conceptual_review", "reviewer_max_turns", None, 15)
CONCEPTUAL_REVIEW_LLM_TEMPERATURE: float = _getfloat("conceptual_review", "llm_temperature", None, 0.15)
CONCEPTUAL_REVIEW_HIGH_SEVERITY_BLOCKS: bool = _getbool("conceptual_review", "high_severity_blocks_advance", None, True)
CONCEPTUAL_REVIEW_RESEARCH_LIVES: int = _getint("conceptual_review", "research_agent_max_lives", None, 3)

# [conceptual_review]
CONCEPTUAL_REVIEW_REVIEWER_TOOLS: list[str] = _getlist(
    "conceptual_review", "reviewer_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, get_task, list_tasks"
)

# ===========================================================================
# Optimization
# ===========================================================================

OPTIMIZATION_PROPOSAL_COUNT: int = _getint("optimization", "proposal_count", None, 5)
OPTIMIZATION_JUDGE_COUNT: int = _getint("optimization", "judge_count", None, 3)
OPTIMIZATION_IMPL_MAX_TURNS: int = _getint("optimization", "implementation_max_turns", None, 100)
OPTIMIZATION_PROPOSER_TEMPERATURE: float = _getfloat("optimization", "proposer_temperature", None, 0.4)
OPTIMIZATION_JUDGE_TEMPERATURE: float = _getfloat("optimization", "judge_temperature", None, 0.1)
OPTIMIZATION_IMPL_TEMPERATURE: float = _getfloat("optimization", "implementation_temperature", None, 0.2)
OPTIMIZATION_MIN_IMPROVEMENT_PCT: float = _getfloat("optimization", "min_improvement_pct", None, 2.0)
OPTIMIZATION_MAX_REGRESSION_PCT: float = _getfloat("optimization", "max_regression_pct", None, 5.0)

# [optimization]
OPTIMIZATION_MAX_REVIEWER_TURNS: int = _getint("optimization", "reviewer_max_turns", None, 50)
OPTIMIZATION_REVIEWER_TOOLS: list[str] = _getlist(
    "optimization", "reviewer_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, get_task, list_tasks"
)

# [optimization_weights]
# Resource weights — higher value = more precious = improvements here count more
OPTIMIZATION_COMPUTE_WEIGHT: float = _getfloat("optimization_weights", "compute_weight", None, 1.0)
OPTIMIZATION_MEMORY_WEIGHT: float = _getfloat("optimization_weights", "memory_weight", None, 0.6)
OPTIMIZATION_STORAGE_WEIGHT: float = _getfloat("optimization_weights", "storage_weight", None, 0.3)

# Qualitative gates
OPTIMIZATION_READABILITY_PENALTY_MAX: float = _getfloat("optimization_weights", "readability_penalty_max", None, 0.5)
OPTIMIZATION_PREMATURE_MULTIPLIER: float = _getfloat("optimization_weights", "premature_multiplier", None, 2.0)
OPTIMIZATION_TECH_DEBT_BONUS_PCT: float = _getfloat("optimization_weights", "tech_debt_bonus_pct", None, 1.0)

# Big O ranking — lower rank = better complexity class
BIG_O_RANKING: dict[str, int] = {
    "O(1)": 1, "O(log n)": 2, "O(n)": 3, "O(n log n)": 4,
    "O(n^2)": 5, "O(n^3)": 6, "O(2^n)": 7, "O(n!)": 8,
}
OPTIMIZATION_BIG_O_BONUS_PCT: float = _getfloat("optimization_weights", "big_o_bonus_pct", None, 10.0)

# ===========================================================================
# Security review
# ===========================================================================

SECURITY_REVIEW_LLM_TEMPERATURE: float = _getfloat("security_review", "llm_temperature", None, 0.1)
SECURITY_REVIEW_VETO_POWER: bool = _getbool("security_review", "veto_power", None, True)
SECURITY_REVIEW_RESEARCH_LIVES: int = _getint("security_review", "research_agent_max_lives", None, 2)

# [security_review]
SECURITY_REVIEW_MAX_REVIEWER_TURNS: int = _getint("security_review", "reviewer_max_turns", None, 50)
SECURITY_REVIEWER_TOOLS: list[str] = _getlist(
    "security_review", "reviewer_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, get_task, list_tasks, run_shell_security"
)

# ===========================================================================
# Full review
# ===========================================================================

FULL_REVIEW_LLM_TEMPERATURE: float = _getfloat("full_review", "llm_temperature", None, 0.1)
FULL_REVIEW_AUTO_UX: bool = _getbool("full_review", "auto_ux_review", None, True)
FULL_REVIEW_FRONTEND_PATTERNS: list[str] = _getlist("full_review", "frontend_patterns",
    "app/web/*.html, app/web/*.js, app/web/*.css"
)
FULL_REVIEW_RESEARCH_LIVES: int = _getint("full_review", "research_agent_max_lives", None, 2)

# [full_review]
FULL_REVIEW_MAX_REVIEWER_TURNS: int = _getint("full_review", "reviewer_max_turns", None, 50)
FULL_REVIEW_CODE_QUALITY_TOOLS: list[str] = _getlist(
    "full_review", "code_quality_reviewer_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, get_task, list_tasks, run_shell_review"
)
FULL_REVIEW_FUNCTIONAL_TOOLS: list[str] = _getlist(
    "full_review", "functional_reviewer_tools",
    "read_file, read_file_harder, read_file_lines, count_lines, search_files, find_files, list_directory, "
    "git_status, git_diff, git_log, git_blame, git_show, get_task, list_tasks"
)

# ===========================================================================
# Merge
# ===========================================================================

MERGE_TEST_TIMEOUT: int = _getint("merge", "test_timeout", None, 300)
MERGE_AUTO_PUSH: bool = _getbool("merge", "auto_push", None, True)
MERGE_TAG_BRANCHES: bool = _getbool("merge", "tag_merged_branches", None, True)
MERGE_DELETE_BRANCHES: bool = _getbool("merge", "delete_merged_branches", None, False)
MERGE_PUSH_RETRIES: int = _getint("merge", "push_retries", None, 3)

# ===========================================================================
# Pipeline stage order and completion detection
# ===========================================================================

PIPELINE_COLUMN_ORDER: list[str] = _getlist(
    "pipeline", "column_order",
    "architecture, idea, planning, indev, conceptual_review, "
    "optimization, security, full_review, completed",
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

SNAPSHOT_MAX_DEPTH: int = _getint("snapshot", "max_depth", None, 3)
SNAPSHOT_MAX_TOKENS: int = _getint("snapshot", "max_tokens", None, 1500)
SNAPSHOT_CACHE_TTL: int = _getint("snapshot", "cache_ttl_seconds", None, 300)

TOOL_LISTING_EXCLUDED_DIRS: set[str] = set(_getlist(
    "tools", "excluded_directories",
    ".archive, .git, venv, .venv, __pycache__, node_modules, "
    ".mypy_cache, .pytest_cache, .ruff_cache, dist, build, .eggs",
))

# ===========================================================================
# Logging
# ===========================================================================

LOG_LEVEL: str = _get("logging", "level", "MAESTRO_LOG_LEVEL", "INFO")
LOG_FILE: str = _get("logging", "log_file", "MAESTRO_LOG_FILE", "")
LOG_MAX_BYTES: int = _getint("logging", "max_bytes", None, 10 * 1024 * 1024)
LOG_BACKUP_COUNT: int = _getint("logging", "backup_count", None, 5)
