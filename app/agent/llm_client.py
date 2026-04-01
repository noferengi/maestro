"""
app/agent/llm_client.py
-----------------------
Centralised LLM HTTP client for all Maestro subsystems.

Every LLM call in the project — intake pipeline, research agent,
MaestroLoop — goes through this module.  Callers can override the
endpoint, model, temperature, and optional payload fields (tools,
response_format, etc.) per call.

When ``budget_id`` is provided, the call is automatically logged to the
``budget_entries`` table with full prompt/response payloads for cost
tracking and dataset building.
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    MAX_TOKENS_PER_TURN,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Per-endpoint connection backoff
#
# ConnectError / ConnectTimeout are infrastructure problems (LLM server not
# running yet), not application errors.  They are logged at WARNING, never
# ERROR.  After _BACKOFF_FREE_TRIES consecutive connection failures the
# endpoint enters a cooldown that doubles on each further failure, starting
# at _BACKOFF_BASE_DELAY seconds and capped at _BACKOFF_MAX_DELAY seconds
# (15 minutes).  A successful call resets the state entirely.
# ---------------------------------------------------------------------------

_BACKOFF_FREE_TRIES: int = 10        # attempts logged at WARNING with no delay
_BACKOFF_BASE_DELAY: float = 3.0     # first backoff duration (seconds)
_BACKOFF_MAX_DELAY: float = 900.0    # cap: 15 minutes


@dataclass
class _EndpointState:
    fail_count: int = 0
    next_allowed: float = 0.0   # monotonic timestamp; 0 = not in cooldown
    delay: float = field(default=_BACKOFF_BASE_DELAY)


_endpoint_states: dict[str, _EndpointState] = {}
_ep_lock = threading.Lock()


async def _stream_llm_response(
    url: str,
    payload: dict,
    idle_timeout: float,
) -> dict:
    """POST to a streaming chat/completions endpoint with per-chunk idle timeout.

    The ``idle_timeout`` clock runs only while waiting for the **next** SSE
    chunk — queue wait, backoff sleeps, and retry delays never count.  If the
    LLM goes silent for ``idle_timeout`` seconds mid-generation, an
    ``httpx.ReadTimeout`` is raised (treated as a stuck/looping model).

    Returns a reconstructed response dict in standard non-streaming shape.
    """
    stream_payload = {
        **payload,
        "stream": True,
        "stream_options": {"include_usage": True},  # request usage in final chunk
    }

    # No httpx read timeout — rely entirely on our asyncio per-chunk timeout so
    # the two timers don't race.  Connect timeout stays short (3 s) so we detect
    # a down server the same way as in non-streaming mode.
    http_timeout = httpx.Timeout(connect=3.0, read=None, write=30.0, pool=5.0)

    accumulated_content: list[str] = []
    finish_reason: str | None = None
    response_id: str | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0

    async with httpx.AsyncClient(timeout=http_timeout) as client:
        async with client.stream(
            "POST", url,
            json=stream_payload,
            headers={"Content-Type": "application/json"},
        ) as response:
            if not response.is_success:
                await response.aread()
                snippet = response.text[:500] if response.text else "(empty body)"
                logger.debug(
                    "LLM stream to %s returned %d: %s",
                    url, response.status_code, snippet,
                )
                response.raise_for_status()

            lines_aiter = response.aiter_lines()
            try:
                while True:
                    try:
                        line = await asyncio.wait_for(
                            lines_aiter.__anext__(),
                            timeout=idle_timeout,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise httpx.ReadTimeout(
                            f"No token from {url} for {idle_timeout:.0f}s — LLM may be stuck"
                        )

                    if not line or not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if data_str == "[DONE]":
                        break

                    try:
                        chunk = json.loads(data_str)
                    except json.JSONDecodeError:
                        continue

                    if response_id is None:
                        response_id = chunk.get("id")

                    for choice in chunk.get("choices", []):
                        delta = choice.get("delta", {})
                        content = delta.get("content")
                        if content:
                            accumulated_content.append(content)
                        fr = choice.get("finish_reason")
                        if fr:
                            finish_reason = fr

                    # Some servers emit usage in the final chunk
                    usage = chunk.get("usage") or {}
                    if usage.get("prompt_tokens"):
                        prompt_tokens = usage["prompt_tokens"]
                    if usage.get("completion_tokens"):
                        completion_tokens = usage["completion_tokens"]
            finally:
                await lines_aiter.aclose()

    return {
        "id": response_id,
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": "".join(accumulated_content),
            },
            "finish_reason": finish_reason,
        }],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def call_llm(
    messages: list[dict],
    *,
    base_url: str | None = None,
    model: str | None = None,
    temperature: float | None = None,
    max_tokens: int | None = None,
    timeout: int | None = None,
    tools: list[dict] | None = None,
    tool_choice: str | None = None,
    response_format: dict | None = None,
    grammar: str | None = None,      # GBNF grammar string for llama.cpp constrained generation
    stream: bool = False,            # Use SSE streaming with per-chunk idle timeout
    stream_idle_timeout: float | None = None,  # Seconds of silence → stuck LLM abort
    # Budget tracking — when provided, the call is logged automatically
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> dict:
    """
    POST to an OpenAI-compatible ``/chat/completions`` endpoint.

    Returns the **raw** parsed JSON response from the server — the same
    shape you'd get from ``response.json()``.

    Parameters
    ----------
    messages
        The conversation messages list.
    base_url
        Root of the OpenAI-compatible API (e.g. ``http://host:port/v1``).
        Defaults to the global ``LLM_BASE_URL`` from config.
    model
        Model identifier sent in the payload.
        Defaults to the global ``LLM_MODEL`` from config.
    temperature
        Sampling temperature.  Defaults to ``LLM_TEMPERATURE``.
    max_tokens
        Max completion tokens.  Defaults to ``MAX_TOKENS_PER_TURN``.
    timeout
        HTTP read timeout in seconds (non-streaming only).
        Defaults to ``LLM_TIMEOUT_SECONDS``.
    tools
        Optional list of OpenAI tool schemas.  Omitted from the payload
        when *None*.
    tool_choice
        Tool-choice strategy (e.g. ``"auto"``).  Omitted when *None*.
    response_format
        Response format hint (e.g. ``{"type": "json_object"}``).
        Omitted when *None*.
    stream
        When True, uses SSE streaming mode.  The idle timeout clock runs
        only while waiting for the next chunk — queue wait and retry sleeps
        do not count.  Returns a reconstructed dict in standard shape.
    stream_idle_timeout
        Seconds without a new token before raising ``httpx.ReadTimeout``
        (stuck LLM detection).  Defaults to ``LLM_TIMEOUT_SECONDS``.
        Only meaningful when ``stream=True``.
    task_id
        Task ID for budget logging (optional but recommended).
    llm_id
        **Required.** LLM endpoint ID — every call must reference an endpoint.
    budget_id
        **Required.** Budget ID — every call must be tracked.  Tokens and
        full payloads are logged to the ``budget_entries`` table.

    Returns
    -------
    dict
        The full JSON body returned by the server.

    Raises
    ------
    httpx.HTTPStatusError
        On non-2xx responses.
    httpx.ReadTimeout
        On request timeout (non-streaming) or idle timeout (streaming).
    """
    if budget_id is None:
        raise ValueError("call_llm() requires budget_id — every LLM call must be tracked.")
    if llm_id is None:
        raise ValueError("call_llm() requires llm_id — every LLM call must reference an endpoint.")

    resolved_url = base_url or LLM_BASE_URL
    resolved_model = model or LLM_MODEL

    payload: dict[str, Any] = {
        "model": resolved_model,
        "messages": messages,
        "temperature": temperature if temperature is not None else LLM_TEMPERATURE,
        "max_tokens": max_tokens or MAX_TOKENS_PER_TURN,
    }

    if tools is not None:
        payload["tools"] = tools
    if tool_choice is not None:
        payload["tool_choice"] = tool_choice
    if response_format is not None:
        payload["response_format"] = response_format
    if grammar is not None:
        payload["grammar"] = grammar

    url = f"{resolved_url}/chat/completions"
    logger.debug("LLM %s -> %s  model=%s", "stream" if stream else "call", url, resolved_model)

    # Non-streaming timeout: short connect to detect down server, long read for slow completions.
    _http_timeout = httpx.Timeout(
        connect=3.0,
        read=float(timeout or LLM_TIMEOUT_SECONDS),
        write=30.0,
        pool=5.0,
    )
    # Streaming idle timeout: seconds of token silence before aborting (stuck LLM).
    _idle_timeout: float = stream_idle_timeout or float(timeout or LLM_TIMEOUT_SECONDS)

    while True:
        # Backoff gate — if endpoint is cooling down, sleep until it clears (never raise).
        # All concurrent callers cooperate on the shared next_allowed timestamp.
        with _ep_lock:
            _state = _endpoint_states.get(resolved_url)
            remaining = (_state.next_allowed - time.monotonic()) if _state else 0.0
        if remaining > 0:
            logger.debug(
                "LLM endpoint %s in backoff (%.0fs remaining), sleeping.",
                resolved_url, remaining,
            )
            await asyncio.sleep(remaining)
            continue  # re-check after sleep

        try:
            if stream:
                # Streaming: per-chunk idle timeout; clock runs only during generation.
                result = await _stream_llm_response(url, payload, _idle_timeout)
            else:
                async with httpx.AsyncClient(timeout=_http_timeout) as client:
                    response = await client.post(
                        url,
                        json=payload,
                        headers={"Content-Type": "application/json"},
                    )
                    if not response.is_success:
                        body_snippet = response.text[:500] if response.text else "(empty body)"
                        logger.debug(
                            "LLM call to %s returned %d: %s",
                            url, response.status_code, body_snippet,
                        )
                    response.raise_for_status()
                    result = response.json()

            # Success — reset backoff state for this endpoint
            with _ep_lock:
                if resolved_url in _endpoint_states:
                    prev = _endpoint_states.pop(resolved_url)
                    if prev.fail_count > 0:
                        logger.info(
                            "LLM endpoint %s is back online (was down for %d attempt(s)).",
                            resolved_url, prev.fail_count,
                        )
            break  # exit retry loop

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Infrastructure problem: server not running.  Log at WARNING, not ERROR.
            # Update shared backoff state so all concurrent callers cooperate.
            with _ep_lock:
                st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                st.fail_count += 1
                if st.fail_count <= _BACKOFF_FREE_TRIES:
                    wait = _BACKOFF_BASE_DELAY
                    logger.warning(
                        "LLM endpoint %s unreachable (attempt %d/%d), retrying in %.0fs: %s",
                        resolved_url, st.fail_count, _BACKOFF_FREE_TRIES, wait, exc,
                    )
                else:
                    wait = st.delay
                    logger.warning(
                        "LLM endpoint %s unreachable (attempt %d), backing off %.0fs.",
                        resolved_url, st.fail_count, wait,
                    )
                    st.next_allowed = time.monotonic() + wait
                    st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)
            await asyncio.sleep(wait)
            # loop continues → retry after sleep

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                # Proxy/server overload — treat identically to a connection error:
                # back off and retry rather than propagating to the caller.  The
                # proxy messages like "proxy error: Could not establish connection"
                # arrive as 500s, not ConnectErrors, so they bypassed the backoff
                # gate entirely before this handler was added.
                with _ep_lock:
                    st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                    st.fail_count += 1
                    if st.fail_count <= _BACKOFF_FREE_TRIES:
                        wait = _BACKOFF_BASE_DELAY
                        logger.debug(
                            "LLM endpoint %s returned %d (attempt %d/%d), retrying in %.0fs.",
                            resolved_url, exc.response.status_code,
                            st.fail_count, _BACKOFF_FREE_TRIES, wait,
                        )
                    else:
                        wait = st.delay
                        logger.debug(
                            "LLM endpoint %s returned %d (attempt %d), backing off %.0fs.",
                            resolved_url, exc.response.status_code, st.fail_count, wait,
                        )
                        st.next_allowed = time.monotonic() + wait
                        st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)
                await asyncio.sleep(wait)
                # loop continues → retry after sleep
            else:
                # 4xx: genuine request error — propagate immediately.
                logger.error("LLM call failed to %s: %r (str: '%s')", url, exc, exc)
                raise

        except Exception as exc:
            logger.error("LLM call failed to %s: %r (str: '%s')", url, exc, exc)
            raise  # ReadTimeout, JSON errors, etc. propagate to caller

    # Log every call to budget_entries
    _log_budget_entry(
        result, messages,
        task_id=task_id, llm_id=llm_id, budget_id=budget_id,
    )

    return result


def _log_budget_entry(
    response: dict,
    messages: list[dict],
    *,
    task_id: str | None,
    llm_id: int | None,
    budget_id: int | None,
) -> None:
    """Persist a budget entry from an LLM response. Best-effort, never raises."""
    try:
        from app.database import create_budget_entry

        usage = response.get("usage", {})
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)

        # Count tool calls in the response (assistant message may have tool_calls array)
        tool_call_count = 0
        for choice in response.get("choices", []):
            msg = choice.get("message", {})
            tc = msg.get("tool_calls")
            if tc:
                tool_call_count += len(tc)
        # Every response is at least 1 LLM turn
        total_turns = max(1, tool_call_count)

        # Serialize payloads
        prompt_json = json.dumps(messages, ensure_ascii=False, default=str)
        response_json = json.dumps(response, ensure_ascii=False, default=str)

        entry = create_budget_entry(
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            prompt_cost=prompt_tokens,
            generation_cost=completion_tokens,
            tool_calls=total_turns,
            prompt_data=prompt_json,
            response_data=response_json,
        )
        if entry and budget_id is not None:
            from app.database import get_llm, create_expense
            remote_call_id = response.get("id")     # e.g. "chatcmpl-abc123"
            pp_rate = 0.0
            tg_rate = 0.0
            if llm_id is not None:
                llm_obj = get_llm(llm_id)
                if llm_obj is not None:
                    pp_rate = getattr(llm_obj, 'cost_per_million_prompt_tokens', 0.0) or 0.0
                    tg_rate = getattr(llm_obj, 'cost_per_million_completion_tokens', 0.0) or 0.0
            pp_uc = int(prompt_tokens * pp_rate * 100)
            tg_uc = int(completion_tokens * tg_rate * 100)
            create_expense(
                budget_entry_id=entry.id, budget_id=budget_id, llm_id=llm_id,
                task_id=task_id, remote_call_id=remote_call_id,
                prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                prompt_cost_microcents=pp_uc, completion_cost_microcents=tg_uc,
            )
    except Exception:
        logger.debug("Failed to log budget entry", exc_info=True)
