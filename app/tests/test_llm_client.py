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
from contextlib import asynccontextmanager

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
    m.is_success = (status < 400)
    m.json.return_value = body
    m.aread = AsyncMock()  # Must be awaitable
    if status >= 400:
        m.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=MagicMock(), response=m
        )
    else:
        m.raise_for_status = MagicMock()
    return m


def _make_mock_client(post_return):
    """Return a mock httpx.AsyncClient class whose POST and stream return post_return (or SSE wrap)."""
    mock_instance = AsyncMock()
    mock_instance.post = AsyncMock(return_value=post_return)

    @asynccontextmanager
    async def mock_stream(method, url, **kwargs):
        # Record the call in post() so existing tests that check call_args still pass.
        # Handle cases where post_return is a side_effect (exception).
        try:
            resp = await mock_instance.post(url, **kwargs)
        except Exception:
            # If post raises, stream must also raise the same exception.
            # We don't yield here; the exception propagates up.
            resp = await mock_instance.post(url, **kwargs)
            yield resp # should not be reached
            return

        if not resp.is_success:
            # Handle cases where post_return.json() fails (e.g. status 500)
            yield resp
            return

        try:
            full_json = resp.json()
        except Exception:
            yield resp
            return

        choice = full_json["choices"][0]
        msg = choice["message"]
        delta = {}
        if msg.get("content"):
            delta["content"] = msg["content"]
        if msg.get("tool_calls"):
            delta["tool_calls"] = [
                {**tc, "index": i} for i, tc in enumerate(msg["tool_calls"])
            ]

        chunk = {
            "id": full_json.get("id", "mock-id"),
            "choices": [{
                "index": 0,
                "delta": delta,
                "finish_reason": choice.get("finish_reason", "stop")
            }],
            "usage": full_json.get("usage", {"prompt_tokens": 0, "completion_tokens": 0})
        }

        mock_resp = MagicMock()
        mock_resp.is_success = resp.is_success
        mock_resp.status_code = resp.status_code
        mock_resp.aread = AsyncMock()  # Must be awaitable
        if not mock_resp.is_success:
            mock_resp.raise_for_status.side_effect = resp.raise_for_status.side_effect

        async def aiter_lines():
            yield f"data: {json.dumps(chunk)}"
            yield "data: [DONE]"

        mock_resp.aiter_lines = aiter_lines
        yield mock_resp

    mock_instance.stream = mock_stream

    mock_cls = MagicMock()
    mock_cls.return_value.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_cls.return_value.__aexit__ = AsyncMock(return_value=False)
    return mock_cls, mock_instance


def _ok_body(prompt_tokens: int = 77, completion_tokens: int = 33) -> dict:
    return {
        "id": "mock-id",
        "object": "chat.completion",
        "choices": [{"index": 0, "message": {"content": "hello", "role": "assistant"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens
        },
        "model": "omnicoder-9b",
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

        # call_llm sends content=bytes (ascii JSON), not json=dict
        raw = mock_instance.post.call_args.kwargs["content"]
        payload = json.loads(raw.decode("ascii"))
        assert "tools" in payload
        assert payload["tools"] == tools

    def test_tools_absent_from_payload_when_none(self):
        from app.agent.llm_client import call_llm

        mock_cls, mock_instance = _make_mock_client(_mock_http_response(_ok_body()))

        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.database.create_budget_entry", MagicMock()):
                _run(call_llm(_messages(), llm_id=1, budget_id=1, tools=None))

        raw = mock_instance.post.call_args.kwargs["content"]
        payload = json.loads(raw.decode("ascii"))
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

        raw = mock_instance.post.call_args.kwargs["content"]
        payload = json.loads(raw.decode("ascii"))
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

    def test_http_timeout_retries_then_raises_runtime_error(self):
        """call_llm retries on ReadTimeout; after max_retries it raises RuntimeError."""
        from app.agent.llm_client import call_llm

        mock_cls, mock_instance = _make_mock_client(None)
        mock_instance.post.side_effect = httpx.ReadTimeout("timeout", request=MagicMock())

        # max_retries=1 caps the loop; asyncio.sleep is mocked to skip real waits
        with patch("httpx.AsyncClient", mock_cls):
            with patch("app.agent.llm_client.asyncio.sleep", AsyncMock()):
                with pytest.raises(RuntimeError, match="timed out"):
                    _run(call_llm(_messages(), llm_id=1, budget_id=1, max_retries=1))


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


# ---------------------------------------------------------------------------
# 14. _sanitize_messages — single-brace identifier escaping
# ---------------------------------------------------------------------------


class TestSanitizeSingleBraces:
    def _sanitize(self, messages):
        from app.agent.llm_client import _sanitize_messages
        return _sanitize_messages(messages)

    def test_url_path_param_replaced(self):
        """URL path parameters like {task_id} are replaced with [task_id]."""
        msgs = [{"role": "user", "content": "Call /api/tasks/{task_id}/diff for details."}]
        result = self._sanitize(msgs)
        assert result[0]["content"] == "Call /api/tasks/[task_id]/diff for details."

    def test_attribute_access_replaced(self):
        """{response.status} style attribute-access patterns are replaced."""
        msgs = [{"role": "user", "content": "Check {response.status} for the code."}]
        result = self._sanitize(msgs)
        assert result[0]["content"] == "Check [response.status] for the code."

    def test_json_object_unchanged(self):
        """JSON objects like {"key": "val"} are NOT matched — key starts with quote."""
        content = 'Send {"key": "value"} as the body.'
        msgs = [{"role": "user", "content": content}]
        result = self._sanitize(msgs)
        assert result[0]["content"] == content

    def test_double_braces_safe_after_both_passes(self):
        """{{var}} is broken by _JINJA2_PAIRS into { {var} }, then {var} → [var] by the
        single-brace pass. The final output { [var] } contains no Jinja2-special sequences."""
        msgs = [{"role": "user", "content": "Template: {{var}} is safe."}]
        result = self._sanitize(msgs)
        content = result[0]["content"]
        # Both passes ran: no double braces remain, no raw {var} remains.
        assert "{{" not in content
        assert "{var}" not in content
        # The final form is fully safe for llama.cpp's Jinja2 renderer.
        assert "{ [var] }" in content

    def test_debug_logged_on_substitution(self):
        """A DEBUG message is emitted when single-brace identifiers are escaped."""
        import logging
        msgs = [{"role": "user", "content": "See /api/tasks/{task_id}/status."}]
        with patch("app.agent.llm_client.logger") as mock_logger:
            self._sanitize(msgs)
            assert mock_logger.debug.called
            call_args = mock_logger.debug.call_args[0]
            assert "single-brace identifier" in call_args[0].lower() or "single-brace" in call_args[0]

    def test_no_substitution_when_no_single_braces(self):
        """Messages without single-brace identifiers are returned unchanged."""
        content = "No braces here at all."
        msgs = [{"role": "user", "content": content}]
        result = self._sanitize(msgs)
        assert result[0]["content"] == content
        # Should be the same dict object (no copy made)
        assert result[0] is msgs[0]

    def test_positional_format_strings_unchanged(self):
        """{0}, {1} positional format strings are NOT matched — digit is not [A-Za-z_]."""
        content = "Format: {0} and {1} should be untouched."
        msgs = [{"role": "user", "content": content}]
        result = self._sanitize(msgs)
        assert result[0]["content"] == content
