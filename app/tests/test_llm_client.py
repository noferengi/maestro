"""
Unit tests for app/agent/llm_client.py.

Covers call_llm() argument enforcement, HTTP payload construction,
token accounting, and the best-effort budget logging path.
All tests mock httpx.AsyncClient - no real HTTP calls are made.
"""

import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, call, patch

import httpx
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _reset_endpoint_states():
    """Clear per-endpoint backoff state between tests so sleeps don't leak."""
    import app.agent.llm_client as lc
    lc._endpoint_states.clear()
    yield
    lc._endpoint_states.clear()


def _mock_http_response(body: dict, status: int = 200) -> MagicMock:
    """Build a mock httpx response."""
    m = MagicMock()
    m.status_code = status
    m.json.return_value = body
    if status >= 400:
        m.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=m
        )
    else:
        m.raise_for_status = MagicMock()
    return m


def _make_mock_client(post_return):
    """Return a mock httpx.AsyncClient class whose POST returns post_return."""
    mock_instance = AsyncMock()
    mock_instance.post = AsyncMock(return_value=post_return)
    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_instance


def _ok_body(prompt_tokens: int = 77, completion_tokens: int = 33) -> dict:
    return {
        "choices": [{"message": {"content": "hello", "tool_calls": None}}],
        "usage": {"prompt_tokens": prompt_tokens, "completion_tokens": completion_tokens},
        "model": "mock-model",
    }


def _messages():
    return [{"role": "user", "content": "hello"}]


# ---------------------------------------------------------------------------
# 1–2. Argument enforcement (budget_id / llm_id required)
# ---------------------------------------------------------------------------


class TestArgumentEnforcement:
    def test_missing_budget_id_raises_value_error(self):
        from app.agent.llm_client import call_llm

        with pytest.raises(ValueError, match="budget_id"):
            _run(call_llm(_messages(), llm_id=1, budget_id=None))

    def test_missing_llm_id_raises_value_error(self):
        from app.agent.llm_client import call_llm

        with pytest.raises(ValueError, match="llm_id"):
            _run(call_llm(_messages(), llm_id=None, budget_id=1))


# ---------------------------------------------------------------------------
# 3. Successful call returns parsed JSON
# ---------------------------------------------------------------------------


class TestSuccessfulCall:
    def test_successful_call_returns_parsed_json(self):
        from app.agent.llm_client import call_llm

        body = _ok_body()
        mock_cls, _ = _make_mock_client(_mock_http_response(body))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                result = _run(call_llm(_messages(), llm_id=1, budget_id=1))

        assert result == body


# ---------------------------------------------------------------------------
# 4–5. Base URL resolution
# ---------------------------------------------------------------------------


class TestBaseUrl:
    def test_uses_default_base_url_from_config(self):
        from app.agent.llm_client import call_llm
        from app.agent.config import LLM_BASE_URL

        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1))

        call_args = mock_instance.post.call_args
        url_called = call_args.args[0]
        assert LLM_BASE_URL in url_called

    def test_custom_base_url_overrides_config(self):
        from app.agent.llm_client import call_llm

        custom_url = "http://other:9999/v1"
        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1, base_url=custom_url))

        call_args = mock_instance.post.call_args
        url_called = call_args.args[0]
        assert url_called.startswith(custom_url)


# ---------------------------------------------------------------------------
# 6–7. Tools in payload
# ---------------------------------------------------------------------------


class TestToolsPayload:
    def test_tools_included_in_payload_when_provided(self):
        from app.agent.llm_client import call_llm

        tools = [{"type": "function", "function": {"name": "read_file"}}]
        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1, tools=tools))

        payload = mock_instance.post.call_args.kwargs["json"]
        assert "tools" in payload
        assert payload["tools"] == tools

    def test_tools_absent_from_payload_when_none(self):
        from app.agent.llm_client import call_llm

        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1, tools=None))

        payload = mock_instance.post.call_args.kwargs["json"]
        assert "tools" not in payload


# ---------------------------------------------------------------------------
# 8. response_format in payload
# ---------------------------------------------------------------------------


class TestResponseFormat:
    def test_response_format_included_when_provided(self):
        from app.agent.llm_client import call_llm

        fmt = {"type": "json_object"}
        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1, response_format=fmt))

        payload = mock_instance.post.call_args.kwargs["json"]
        assert payload.get("response_format") == fmt


# ---------------------------------------------------------------------------
# 9–10. Budget entry recording
# ---------------------------------------------------------------------------


class TestBudgetLogging:
    def test_budget_entry_created_with_correct_tokens(self):
        from app.agent.llm_client import call_llm

        body = _ok_body(prompt_tokens=77, completion_tokens=33)
        mock_cls, _ = _make_mock_client(_mock_http_response(body))
        mock_create = MagicMock()

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", mock_create):
                _run(call_llm(_messages(), llm_id=5, budget_id=9, task_id="t1"))

        mock_create.assert_called_once()
        kwargs = mock_create.call_args.kwargs
        assert kwargs["prompt_cost"] == 77
        assert kwargs["generation_cost"] == 33
        assert kwargs["llm_id"] == 5
        assert kwargs["budget_id"] == 9

    def test_tool_calls_in_response_count_as_turns(self):
        from app.agent.llm_client import call_llm

        body = {
            "choices": [
                {
                    "message": {
                        "content": None,
                        "tool_calls": [
                            {"id": "c1", "type": "function"},
                            {"id": "c2", "type": "function"},
                            {"id": "c3", "type": "function"},
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 40, "completion_tokens": 30},
            "model": "mock",
        }
        mock_cls, _ = _make_mock_client(_mock_http_response(body))
        mock_create = MagicMock()

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", mock_create):
                _run(call_llm(_messages(), llm_id=1, budget_id=1))

        kwargs = mock_create.call_args.kwargs
        assert kwargs["tool_calls"] == 3


# ---------------------------------------------------------------------------
# 11–12. HTTP error propagation
# ---------------------------------------------------------------------------


class TestHttpErrors:
    def test_http_4xx_raises_http_status_error(self):
        """4xx errors propagate immediately - call_llm does NOT retry client errors."""
        from app.agent.llm_client import call_llm

        mock_cls, _ = _make_mock_client(_mock_http_response({}, status=422))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                with pytest.raises(httpx.HTTPStatusError):
                    _run(call_llm(_messages(), llm_id=1, budget_id=1))

    def test_http_timeout_raises_timeout_exception(self):
        from app.agent.llm_client import call_llm

        mock_instance = AsyncMock()
        mock_instance.post = AsyncMock(
            side_effect=httpx.ReadTimeout("timeout", request=MagicMock())
        )
        mock_cls = MagicMock()
        mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
        mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", mock_cls):
            with pytest.raises(httpx.TimeoutException):
                _run(call_llm(_messages(), llm_id=1, budget_id=1))


# ---------------------------------------------------------------------------
# 13. Budget logging failure is swallowed
# ---------------------------------------------------------------------------


class TestBudgetLoggingFailure:
    def test_budget_logging_failure_does_not_propagate(self):
        from app.agent.llm_client import call_llm

        body = _ok_body()
        mock_cls, _ = _make_mock_client(_mock_http_response(body))
        exploding_create = MagicMock(side_effect=Exception("DB down"))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", exploding_create):
                result = _run(call_llm(_messages(), llm_id=1, budget_id=1))

        # Budget entry failure must not surface - call_llm must still return result
        assert result == body
