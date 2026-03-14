"""
app/agent
---------
Public API for the Maestro agentic subsystem.

Typical usage::

    from app.agent import MaestroLoop, LoopResult, DAGResolver
    from app.agent import dispatch_tool, TOOL_SCHEMAS, TOOL_REGISTRY
    from app.agent import MAESTRO_SYSTEM_PROMPT
    from app.agent.config import LLM_BASE_URL, MAX_TURNS
"""

from app.agent.config import (  # noqa: F401
    LLM_BASE_URL,
    LLM_MODEL,
    MAX_TURNS,
    MAX_TOKENS_PER_TURN,
    MAX_CONSECUTIVE_ERRORS,
    MAX_TASK_RETRIES,
    ARCHIVE_DIR,
    GIT_SAFETY_BRANCH_PREFIX,
    SIGNAL_ACCEPTED,
    SIGNAL_REVERT,
)
from app.agent.dag import DAGResolver  # noqa: F401
from app.agent.loop import LoopResult, MaestroLoop  # noqa: F401
from app.agent.loop import get_loop_status, request_stop  # noqa: F401
from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT  # noqa: F401
from app.agent.tools import (  # noqa: F401
    TOOL_REGISTRY,
    TOOL_SCHEMAS,
    dispatch_tool,
)

__all__ = [
    # Config
    "LLM_BASE_URL",
    "LLM_MODEL",
    "MAX_TURNS",
    "MAX_TOKENS_PER_TURN",
    "MAX_CONSECUTIVE_ERRORS",
    "MAX_TASK_RETRIES",
    "ARCHIVE_DIR",
    "GIT_SAFETY_BRANCH_PREFIX",
    "SIGNAL_ACCEPTED",
    "SIGNAL_REVERT",
    # DAG
    "DAGResolver",
    # Loop
    "LoopResult",
    "MaestroLoop",
    "get_loop_status",
    "request_stop",
    # System prompt
    "MAESTRO_SYSTEM_PROMPT",
    # Tools
    "TOOL_REGISTRY",
    "TOOL_SCHEMAS",
    "dispatch_tool",
]
