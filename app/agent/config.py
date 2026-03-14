"""
app/agent/config.py
-------------------
Central configuration for the Maestro agent subsystem.
All LLM endpoints, safety limits, and filesystem constants live here.
Import this module everywhere rather than hard-coding values.
"""

import os

# ---------------------------------------------------------------------------
# LLM / API settings
# ---------------------------------------------------------------------------

# Base URL for the llama.cpp OpenAI-compatible server
LLM_BASE_URL: str = os.getenv("MAESTRO_LLM_BASE_URL", "http://localhost:8008/v1")

# Model name as exposed by the llama.cpp server
LLM_MODEL: str = os.getenv("MAESTRO_LLM_MODEL", "omnicoder-9b")

# Per-request generation settings
MAX_TOKENS_PER_TURN: int = int(os.getenv("MAESTRO_MAX_TOKENS", "4096"))
LLM_TEMPERATURE: float = float(os.getenv("MAESTRO_TEMPERATURE", "0.2"))
LLM_TIMEOUT_SECONDS: int = int(os.getenv("MAESTRO_LLM_TIMEOUT", "120"))

# ---------------------------------------------------------------------------
# Loop safety limits
# ---------------------------------------------------------------------------

# Hard cap on agent turns before declaring MAX_TURNS termination
MAX_TURNS: int = int(os.getenv("MAESTRO_MAX_TURNS", "150"))

# Number of consecutive tool errors before triggering REVERT_TO_DESIGN
MAX_CONSECUTIVE_ERRORS: int = 3

# Number of task-level retries before triggering REVERT_TO_DESIGN
MAX_TASK_RETRIES: int = 3

# Hard timeout (seconds) for run_shell calls
SHELL_TIMEOUT_SECONDS: int = int(os.getenv("MAESTRO_SHELL_TIMEOUT", "30"))

# ---------------------------------------------------------------------------
# Filesystem paths
# ---------------------------------------------------------------------------

# Project root — everything stays inside this directory
PROJECT_ROOT: str = os.getenv(
    "MAESTRO_PROJECT_ROOT",
    os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
)

# Archive directory for soft-deleted files (relative to PROJECT_ROOT)
ARCHIVE_DIR: str = os.path.join(PROJECT_ROOT, ".archive")

# ---------------------------------------------------------------------------
# Git settings
# ---------------------------------------------------------------------------

# All agent work branches are namespaced under this prefix
GIT_SAFETY_BRANCH_PREFIX: str = "maestro/task-"

# Branches the agent is allowed to checkout (in addition to maestro/* branches)
GIT_ALLOWED_BASE_BRANCHES: list[str] = ["main", "master"]

# ---------------------------------------------------------------------------
# Agent status values (canonical set used across tools + loop)
# ---------------------------------------------------------------------------

STATUS_PENDING: str = "PENDING"
STATUS_ACTIVE: str = "ACTIVE"
STATUS_VERIFYING: str = "VERIFYING"
STATUS_ACCEPTED: str = "ACCEPTED"
STATUS_REJECTED: str = "REJECTED"

# Signal emitted by the agent when it wants the loop to revert to design phase
SIGNAL_REVERT: str = "REVERT_TO_DESIGN"
SIGNAL_ACCEPTED: str = "ACCEPTED"
