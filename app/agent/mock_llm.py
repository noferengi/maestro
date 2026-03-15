"""
app/agent/mock_llm.py
---------------------
Dictionary-based mock LLM for testing the research agent and intake pipeline.

Implements the same OpenAI chat-completions response format as a real LLM
endpoint, but returns canned responses based on pattern matching against
the conversation messages.

Usage::

    mock = MockLLM(scenario="pass")
    response = mock.complete(messages, tools=None)
    # response has the same shape as POST /v1/chat/completions

As an httpx mock, patch ``httpx.AsyncClient.post`` to call
``mock.handle_post(url, **kwargs)`` which returns a fake httpx.Response.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock


# ---------------------------------------------------------------------------
# Scenario presets
# ---------------------------------------------------------------------------

# Each scenario defines a list of responses the mock will return in sequence.
# Each response can be:
#   - A plain text response (content only, no tool calls)
#   - A tool call response (tool_calls list)
#   - A verdict JSON (for research agent terminal output)

_VERDICT_PASS = json.dumps({
    "verdict": "LIKELY",
    "confidence": 95,
    "justification": "The task is well-defined and the codebase supports it.",
    "findings": "Codebase has all necessary infrastructure in place.",
})

_VERDICT_FAIL = json.dumps({
    "verdict": "REJECTED",
    "confidence": 25,
    "justification": "Fundamental blocker found: the required API does not exist.",
    "findings": "No matching API endpoint or module exists.",
})

_VERDICT_NOT_SUITABLE = json.dumps({
    "verdict": "NOT_SUITABLE",
    "confidence": 55,
    "justification": "Task is too vague and lacks clear success criteria.",
    "findings": "Could not find enough context to determine feasibility.",
})

_VERDICT_NEEDS_RESEARCH = json.dumps({
    "verdict": "NEEDS_RESEARCH",
    "confidence": 65,
    "justification": "Insufficient information to determine feasibility.",
    "findings": "Need to investigate external dependency compatibility.",
})

_VERDICT_POSSIBLE = json.dumps({
    "verdict": "POSSIBLE",
    "confidence": 82,
    "justification": "Task is feasible but requires some refactoring.",
    "findings": "Found relevant modules but they need adaptation.",
})

_VERDICT_TIE_PASS = json.dumps({
    "verdict": "LIKELY",
    "confidence": 93,
    "justification": "After investigation, the pass voters were correct. Evidence supports feasibility.",
    "findings": "Resolved disagreement in favour of proceeding.",
    "resolved_disagreements": ["Dependency exists and is compatible"],
})


# ---------------------------------------------------------------------------
# Intake pipeline canned responses (structured JSON for _call_llm)
# ---------------------------------------------------------------------------

_SCOPE_RESPONSE_PASS = {
    "scope": "medium",
    "complexity": 5,
    "decomposition_needed": False,
    "subtasks": [],
    "affected_areas": ["app/agent/", "app/web/"],
    "effort": "moderate",
    "vote": {
        "verdict": "LIKELY",
        "confidence": 0.92,
        "justification": "Task is well-defined with clear scope.",
    },
}

_SCOPE_RESPONSE_REJECTED = {
    "scope": "epic",
    "complexity": 10,
    "decomposition_needed": True,
    "subtasks": ["too", "many", "things"],
    "affected_areas": ["everything"],
    "effort": "major",
    "vote": {
        "verdict": "REJECTED",
        "confidence": 0.15,
        "justification": "Task is fundamentally unfeasible.",
    },
}

_SCOPE_RESPONSE_NEEDS_RESEARCH = {
    "scope": "large",
    "complexity": 7,
    "decomposition_needed": False,
    "subtasks": [],
    "affected_areas": ["unknown"],
    "effort": "significant",
    "vote": {
        "verdict": "NEEDS_RESEARCH",
        "confidence": 0.65,
        "justification": "Cannot determine scope without more investigation.",
    },
}

_FEASIBILITY_RESPONSE_PASS = {
    "feasibility_rating": 0.85,
    "ambiguities": [],
    "external_dependencies": [],
    "risks": ["minor test coverage gap"],
    "codebase_readiness": "ready",
    "vote": {
        "verdict": "POSSIBLE",
        "confidence": 0.80,
        "justification": "Codebase is ready, minor risks exist.",
    },
}

_CONFLICT_RESPONSE_PASS = {
    "file_conflicts": [],
    "semantic_conflicts": [],
    "priority_conflicts": [],
    "resource_conflicts": [],
    "vote": {
        "verdict": "LIKELY",
        "confidence": 0.95,
        "justification": "No conflicts detected with existing tasks.",
    },
}

_CONFLICT_RESPONSE_NOT_SUITABLE = {
    "file_conflicts": [
        {"task_id": "task-99", "task_title": "Competing feature",
         "shared_files": ["app/main.py"], "severity": "high"},
    ],
    "semantic_conflicts": [],
    "priority_conflicts": [],
    "resource_conflicts": [],
    "vote": {
        "verdict": "NOT_SUITABLE",
        "confidence": 0.55,
        "justification": "High-severity file conflict with existing task.",
    },
}


# ---------------------------------------------------------------------------
# Pattern rules
# ---------------------------------------------------------------------------

@dataclass
class PatternRule:
    """A pattern-to-response mapping for the mock LLM."""
    pattern: str            # Regex to match against the concatenated messages
    response_content: str   # Text content to return
    tool_calls: list[dict] | None = None  # Optional tool calls to include
    prompt_tokens: int = 50
    completion_tokens: int = 100


# ---------------------------------------------------------------------------
# MockLLM
# ---------------------------------------------------------------------------

class MockLLM:
    """
    A deterministic mock LLM that returns canned responses based on
    pattern matching against prompt content.

    Parameters
    ----------
    scenario : str
        One of "pass", "fail", "needs_research", "tie", "not_suitable",
        "tool_then_verdict", "exhaust_lives".
    custom_rules : list[PatternRule] | None
        Additional pattern rules to check before the defaults.
    """

    def __init__(
        self,
        scenario: str = "pass",
        custom_rules: list[PatternRule] | None = None,
    ) -> None:
        self.scenario = scenario
        self.custom_rules = custom_rules or []
        self.call_count = 0
        self.total_prompt_tokens = 0
        self.total_completion_tokens = 0
        self.call_log: list[dict] = []  # Record of all calls made

        # Build the scenario-specific response queue
        self._response_queue: list[dict] = self._build_response_queue(scenario)
        self._queue_index = 0

    # ------------------------------------------------------------------
    # Response queue builders
    # ------------------------------------------------------------------

    def _build_response_queue(self, scenario: str) -> list[dict]:
        """Build ordered response list for the given scenario."""
        if scenario == "pass":
            return [self._text_response(_VERDICT_PASS)]
        elif scenario == "fail":
            return [self._text_response(_VERDICT_FAIL)]
        elif scenario == "not_suitable":
            return [self._text_response(_VERDICT_NOT_SUITABLE)]
        elif scenario == "needs_research":
            return [
                self._text_response(_VERDICT_NEEDS_RESEARCH),
                self._text_response(_VERDICT_PASS),
            ]
        elif scenario == "tie":
            return [self._text_response(_VERDICT_TIE_PASS)]
        elif scenario == "tool_then_verdict":
            return [
                self._tool_call_response("read_file", {"path": "app/main.py"}),
                self._text_response(_VERDICT_PASS),
            ]
        elif scenario == "exhaust_lives":
            # Always returns NEEDS_RESEARCH, never a confident verdict
            return [self._text_response(_VERDICT_NEEDS_RESEARCH)] * 10
        elif scenario == "blocked_tool":
            # Try to use a write tool (should be blocked by research agent)
            return [
                self._tool_call_response("write_file", {"path": "hack.py", "content": "pwned"}),
                self._text_response(_VERDICT_PASS),
            ]
        elif scenario == "scope_pass":
            return [self._text_response(json.dumps(_SCOPE_RESPONSE_PASS))]
        elif scenario == "scope_rejected":
            return [self._text_response(json.dumps(_SCOPE_RESPONSE_REJECTED))]
        elif scenario == "scope_needs_research":
            return [self._text_response(json.dumps(_SCOPE_RESPONSE_NEEDS_RESEARCH))]
        elif scenario == "feasibility_pass":
            return [self._text_response(json.dumps(_FEASIBILITY_RESPONSE_PASS))]
        elif scenario == "conflict_pass":
            return [self._text_response(json.dumps(_CONFLICT_RESPONSE_PASS))]
        elif scenario == "conflict_not_suitable":
            return [self._text_response(json.dumps(_CONFLICT_RESPONSE_NOT_SUITABLE))]
        elif scenario == "intake_all_pass":
            # Scope -> Static (skipped) -> Conflict -> Feasibility
            return [
                self._text_response(json.dumps(_SCOPE_RESPONSE_PASS)),
                self._text_response(json.dumps(_CONFLICT_RESPONSE_PASS)),
                self._text_response(json.dumps(_FEASIBILITY_RESPONSE_PASS)),
            ]
        elif scenario == "intake_rejected":
            return [
                self._text_response(json.dumps(_SCOPE_RESPONSE_REJECTED)),
            ]
        elif scenario == "intake_tie":
            # Scope=LIKELY, Conflict=NOT_SUITABLE, Feasibility=POSSIBLE
            # => 2 pass vs 1 fail + static_analysis(POSSIBLE) = 3 pass vs 1 fail -> passed
            # Need: Scope=LIKELY, Conflict=NOT_SUITABLE, Static=NOT_SUITABLE, Feasibility=POSSIBLE
            # That's 2 pass vs 2 fail -> tie
            scope_likely = dict(_SCOPE_RESPONSE_PASS)
            scope_likely["vote"] = {"verdict": "LIKELY", "confidence": 0.93, "justification": "Looks good."}

            conflict_ns = dict(_CONFLICT_RESPONSE_NOT_SUITABLE)

            feas_possible = dict(_FEASIBILITY_RESPONSE_PASS)
            feas_possible["vote"] = {"verdict": "POSSIBLE", "confidence": 0.80, "justification": "Feasible."}

            return [
                self._text_response(json.dumps(scope_likely)),
                self._text_response(json.dumps(conflict_ns)),
                self._text_response(json.dumps(feas_possible)),
                # Tiebreaker response
                self._text_response(_VERDICT_TIE_PASS),
            ]
        elif scenario == "intake_needs_research":
            scope_nr = dict(_SCOPE_RESPONSE_NEEDS_RESEARCH)
            return [
                self._text_response(json.dumps(scope_nr)),
                self._text_response(json.dumps(_CONFLICT_RESPONSE_PASS)),
                self._text_response(json.dumps(_FEASIBILITY_RESPONSE_PASS)),
                # Research agent response (for the needs_research handler)
                self._text_response(_VERDICT_PASS),
            ]
        else:
            raise ValueError(f"Unknown scenario: {scenario!r}")

    @staticmethod
    def _text_response(content: str, prompt_tokens: int = 50, completion_tokens: int = 100) -> dict:
        """Build a standard text-only LLM response."""
        return {
            "id": "mock-completion-001",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": content,
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    @staticmethod
    def _tool_call_response(
        tool_name: str,
        arguments: dict,
        tool_call_id: str = "call_mock_001",
        prompt_tokens: int = 40,
        completion_tokens: int = 30,
    ) -> dict:
        """Build a response with a single tool call."""
        return {
            "id": "mock-completion-002",
            "object": "chat.completion",
            "model": "mock-model",
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": tool_name,
                                    "arguments": json.dumps(arguments),
                                },
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        }

    # ------------------------------------------------------------------
    # Core completion method
    # ------------------------------------------------------------------

    def complete(self, messages: list[dict], tools: list[dict] | None = None) -> dict:
        """
        Produce a mock LLM response.

        First checks custom_rules for pattern matches, then falls back
        to the scenario's response queue.
        """
        self.call_count += 1

        # Concatenate all message content for pattern matching
        full_text = " ".join(
            str(m.get("content", "")) for m in messages if m.get("content")
        )

        # Log the call
        self.call_log.append({
            "call_number": self.call_count,
            "messages": messages,
            "tools": tools,
            "full_text_preview": full_text[:200],
        })

        # Check custom rules first
        for rule in self.custom_rules:
            if re.search(rule.pattern, full_text, re.IGNORECASE):
                response = self._text_response(
                    rule.response_content,
                    rule.prompt_tokens,
                    rule.completion_tokens,
                )
                if rule.tool_calls:
                    response["choices"][0]["message"]["tool_calls"] = rule.tool_calls
                    response["choices"][0]["message"]["content"] = None
                self.total_prompt_tokens += rule.prompt_tokens
                self.total_completion_tokens += rule.completion_tokens
                return response

        # Fall back to the response queue
        if self._queue_index < len(self._response_queue):
            response = self._response_queue[self._queue_index]
            self._queue_index += 1
        else:
            # Repeat the last response if the queue is exhausted
            response = self._response_queue[-1]

        usage = response.get("usage", {})
        self.total_prompt_tokens += usage.get("prompt_tokens", 0)
        self.total_completion_tokens += usage.get("completion_tokens", 0)

        return response

    # ------------------------------------------------------------------
    # httpx integration — mock POST handler
    # ------------------------------------------------------------------

    async def handle_post(self, url: str, **kwargs) -> MagicMock:
        """
        Drop-in replacement for httpx.AsyncClient.post().

        Returns a MagicMock that behaves like an httpx.Response:
        - .status_code = 200
        - .json() returns the mock response dict
        - .raise_for_status() is a no-op
        """
        payload = kwargs.get("json", {})
        messages = payload.get("messages", [])
        tools = payload.get("tools", None)

        result = self.complete(messages, tools)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = result
        mock_response.raise_for_status = MagicMock()
        return mock_response

    def get_async_client_mock(self) -> AsyncMock:
        """
        Return an AsyncMock suitable for patching httpx.AsyncClient.

        Usage in tests::

            mock_llm = MockLLM(scenario="pass")
            with patch("httpx.AsyncClient") as mock_client_cls:
                mock_client_cls.return_value.__aenter__.return_value.post = mock_llm.handle_post
                result = await some_function_that_calls_llm()
        """
        mock_client = AsyncMock()
        mock_client.__aenter__.return_value.post = self.handle_post
        return mock_client


# ---------------------------------------------------------------------------
# Convenience factory
# ---------------------------------------------------------------------------

def create_mock_llm(scenario: str = "pass", **kwargs) -> MockLLM:
    """Factory function to create a configured MockLLM."""
    return MockLLM(scenario=scenario, **kwargs)
