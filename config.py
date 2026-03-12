"""Configuration management for The Maestro Orchestrator."""

import os

__APP_ID = "maestro-orchestrator"


def get_app_id():
    """Return the application ID."""
    return __APP_ID


class Endpoints:
    """API endpoint configurations."""

    @property
    def LLM_API_BASE(self):
        """Return LLM API base URL from environment or default."""
        return os.getenv("LLM_API_BASE", "http://localhost:8080/v1")

    @property
    def LLM_API_KEY(self):
        """Return LLM API key from environment or default."""
        return os.getenv("LLM_API_KEY", "")


class ProjectConstants:
    """Project-specific constants."""

    MAX_CONTEXT_TOKENS = 100_000
    MAX_FAILURE_RETRIES = 3
    DEFAULT_MODEL = "Qwen-3-Coder-80B"
