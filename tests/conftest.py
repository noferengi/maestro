"""
tests/conftest.py
-----------------
Shared fixtures for the Maestro test suite.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Redirect all DB I/O to test.db before any test module imports database.
# Mirrors the same guard in app/tests/conftest.py — both directories need it
# so that whichever conftest is loaded first, the env var is set.
_TEST_DB = Path(__file__).parent.parent / "data" / "test.db"
os.environ.setdefault("MAESTRO_TEST_DB", str(_TEST_DB))

from app.agent.mock_llm import MockLLM


@pytest.fixture
def mock_llm_pass():
    """MockLLM that returns a LIKELY/pass verdict."""
    return MockLLM(scenario="pass")


@pytest.fixture
def mock_llm_fail():
    """MockLLM that returns a REJECTED verdict."""
    return MockLLM(scenario="fail")


@pytest.fixture
def mock_llm_needs_research():
    """MockLLM that returns NEEDS_RESEARCH then LIKELY on second life."""
    return MockLLM(scenario="needs_research")


@pytest.fixture
def mock_llm_exhaust_lives():
    """MockLLM that always returns NEEDS_RESEARCH (never resolves)."""
    return MockLLM(scenario="exhaust_lives")


@pytest.fixture
def mock_llm_tool_then_verdict():
    """MockLLM that issues a tool call, then renders a verdict."""
    return MockLLM(scenario="tool_then_verdict")


@pytest.fixture
def mock_llm_blocked_tool():
    """MockLLM that tries to call a write tool (should be blocked)."""
    return MockLLM(scenario="blocked_tool")


@pytest.fixture
def sample_task_context():
    """A sample context dict for research agent tests."""
    return {
        "task_id": "task-42",
        "task_title": "Add WebSocket support",
        "task_description": "Add real-time WebSocket endpoints for live board updates.",
        "stage": "scope_analysis",
    }


@pytest.fixture
def sample_all_tasks():
    """A list of sample tasks for intake pipeline tests."""
    return [
        {
            "id": "task-1",
            "title": "Setup database",
            "type": "completed",
            "description": "Initialize SQLite database with SQLAlchemy.",
        },
        {
            "id": "task-2",
            "title": "Add drag-and-drop",
            "type": "development",
            "description": "Implement drag-and-drop reordering for kanban cards.",
        },
        {
            "id": "task-3",
            "title": "Agent loop",
            "type": "planning",
            "description": "Build the MaestroLoop agentic orchestrator.",
        },
    ]
