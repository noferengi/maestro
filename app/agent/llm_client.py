"""
app/agent/llm_client.py
-----------------------
Centralised LLM HTTP client for all Maestro subsystems.

Every LLM call in the project - intake pipeline, research agent,
MaestroLoop - goes through this module.  Callers can override the
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
import random
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
#
# Per-endpoint dispatch stagger
#
# Concurrent callers reserve a time slot before sending so requests never
# arrive at the LLM server in the same millisecond window.  Each reservation
# bumps next_dispatch_at by _MIN_DISPATCH_GAP, creating an implicit FIFO
# queue.  Callers sleep for their reserved gap then send — no 500 hammering.
#
# Model-switch detection
#
# When a caller uses a different llm_id than the previous request on the same
# endpoint URL, the underlying model may need to load.  The current request
# fires immediately, but next_dispatch_at is pushed forward by
# _MODEL_LOAD_DELAY so subsequent callers queue behind the load window.
# ---------------------------------------------------------------------------

_BACKOFF_FREE_TRIES: int = 10        # attempts logged at WARNING with no delay
_BACKOFF_BASE_DELAY: float = 3.0     # first backoff duration (seconds)
_BACKOFF_MAX_DELAY: float = 900.0    # cap: 15 minutes
_MIN_DISPATCH_GAP: float = 0.15      # seconds between consecutive dispatches to same endpoint
_MODEL_LOAD_DELAY: float = 10.0      # seconds to gate subsequent requests after a model switch


@dataclass
class _EndpointState:
    fail_count: int = 0
    next_allowed: float = 0.0       # monotonic timestamp; 0 = not in cooldown
    delay: float = field(default=_BACKOFF_BASE_DELAY)
    next_dispatch_at: float = 0.0   # earliest slot available for next dispatch
    last_llm_id: "int | None" = None  # llm_id of the most recently dispatched request


_endpoint_states: dict[str, _EndpointState] = {}
_ep_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Graceful shutdown flag
#
# Set via signal_shutdown() from the FastAPI lifespan before stop_scheduler().
# call_llm() checks this at the top of every retry iteration so in-flight
# backoff loops exit cleanly instead of spinning through interpreter teardown.
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()


class ShutdownError(Exception):
    """Raised when an LLM call is aborted because the server is shutting down."""
    pass


def signal_shutdown() -> None:
    """Signal all in-flight LLM calls to abort on their next retry check."""
    _shutdown_event.set()


def is_shutting_down() -> bool:
    """Return True once signal_shutdown() has been called."""
    return _shutdown_event.is_set()


async def _stream_llm_response(
    url: str,
    payload: dict,
    idle_timeout: float,
) -> dict:
    """POST to a streaming chat/completions endpoint with per-chunk idle timeout.

    The ``idle_timeout`` clock runs only while waiting for the **next** SSE
    chunk - queue wait, backoff sleeps, and retry delays never count.  If the
    LLM goes silent for ``idle_timeout`` seconds mid-generation, an
    ``httpx.ReadTimeout`` is raised (treated as a stuck/looping model).

    Returns a reconstructed response dict in standard non-streaming shape.
    """
    stream_payload = {
        **payload,
        "stream": True,
        "stream_options": {"include_usage": True},  # request usage in final chunk
    }

    # No httpx read timeout - rely entirely on our asyncio per-chunk timeout so
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
                logger.warning(
                    "LLM stream to %s returned %d\nPayload:\n%s\nResponse: %s",
                    url, response.status_code,
                    _describe_payload(payload), snippet,
                )
                response.raise_for_status()

            lines_aiter = response.aiter_lines()
            try:
                while True:
                    if _shutdown_event.is_set():
                        raise ShutdownError("Server is shutting down")

                    try:
                        line = await asyncio.wait_for(
                            lines_aiter.__anext__(),
                            timeout=idle_timeout,
                        )
                    except StopAsyncIteration:
                        break
                    except asyncio.TimeoutError:
                        raise httpx.ReadTimeout(
                            f"No token from {url} for {idle_timeout:.0f}s - LLM may be stuck"
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


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of *messages* with problematic bytes/sequences removed or escaped.

    llama.cpp's chat-template engine fails with "Failed to parse input at pos N"
    for two distinct content issues:

    1. **Null bytes** (``\\x00``) — file-read tool results are the most common source;
       binary or mixed-encoding files can contain null bytes that survive Python's
       ``errors='replace'`` round-trip, which ``json.dumps`` serialises as ``\\u0000``.
       Fix: strip them entirely.

    2. **Jinja2 template delimiters** (``{{``, ``}}``, ``{%``, ``{#``) — LLM-generated
       content (e.g. design rationale containing Python f-string examples, template
       code, or dict comprehensions) can contain these sequences.  When they appear
       inside a chat-template variable expansion the Jinja2 parser chokes at the
       position of the delimiter.  Fix: insert a zero-width space between the two
       characters so the sequence is no longer a valid Jinja2 token.

    Only ``content`` fields that are plain strings are touched; tool_call objects,
    lists, and other types are left unchanged.
    """
    # Jinja2 two-character delimiters that must be broken up.
    _JINJA2_PAIRS = [("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("{#", "{ #")]

    result = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, str):
            result.append(msg)
            continue

        role = msg.get("role", "?")
        tool_call_id = msg.get("tool_call_id", "")
        id_hint = f" tool_call_id={tool_call_id!r}" if tool_call_id else ""
        changed = False

        if "\x00" in content:
            count = content.count("\x00")
            logger.warning(
                "Stripping %d null byte(s) from message[%d] (role=%s%s, len=%d) "
                "before sending to LLM — source likely a binary/mixed-encoding file.",
                count, i, role, id_hint, len(content),
            )
            content = content.replace("\x00", "")
            changed = True

        for raw, safe in _JINJA2_PAIRS:
            if raw in content:
                count = content.count(raw)
                logger.warning(
                    "Escaping %d Jinja2 delimiter(s) %r in message[%d] (role=%s%s, len=%d) "
                    "before sending to LLM — source likely LLM-generated content with "
                    "template syntax (f-strings, dict comprehensions, Jinja templates).",
                    count, raw, i, role, id_hint, len(content),
                )
                content = content.replace(raw, safe)
                changed = True

        if changed:
            msg = {**msg, "content": content}
        result.append(msg)
    return result


def _describe_payload(payload: dict) -> str:
    """Return a compact diagnostic string describing a chat/completions payload.

    Emitted alongside 500 / parse-error log messages so you can immediately see
    which message triggered the failure without needing to capture raw payloads.

    Example output::

        model=Qwen3p5-Omnicoder  messages=5  tools=8
          [0] system       312 chars
          [1] user         208 chars
          [2] assistant    (tool_calls=1)
          [3] tool         3847 chars  [NULL_BYTES:2  CTRL_CHARS:5]
          [4] assistant    0 chars
    """
    lines: list[str] = []
    model = payload.get("model", "?")
    messages = payload.get("messages", [])
    tools = payload.get("tools") or []
    lines.append(f"model={model}  messages={len(messages)}  tools={len(tools)}")

    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []

        if content is None and tool_calls:
            lines.append(f"  [{i}] {role:<12} (tool_calls={len(tool_calls)})")
        elif isinstance(content, str):
            flags: list[str] = []
            null_count = content.count("\x00")
            if null_count:
                flags.append(f"NULL_BYTES:{null_count}")
            ctrl_count = sum(
                1 for c in content if ord(c) < 32 and c not in "\n\r\t"
            )
            if ctrl_count:
                flags.append(f"CTRL_CHARS:{ctrl_count}")
            jinja2_count = sum(
                content.count(d) for d in ("{{", "}}", "{%", "{#")
            )
            if jinja2_count:
                flags.append(f"JINJA2_DELIMITERS:{jinja2_count}")
            flag_str = f"  [{' '.join(flags)}]" if flags else ""
            lines.append(f"  [{i}] {role:<12} {len(content)} chars{flag_str}")
        else:
            lines.append(f"  [{i}] {role:<12} (content type={type(content).__name__})")

    return "\n".join(lines)


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
    stream_idle_timeout: float | None = None,  # Seconds of silence -> stuck LLM abort
    # Budget tracking - when provided, the call is logged automatically
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    # Diagnostic label - appears in all retry/error log messages
    agent_name: str | None = None,
) -> dict:
    """
    POST to an OpenAI-compatible ``/chat/completions`` endpoint.

    Returns the **raw** parsed JSON response from the server - the same
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
        only while waiting for the next chunk - queue wait and retry sleeps
        do not count.  Returns a reconstructed dict in standard shape.
    stream_idle_timeout
        Seconds without a new token before raising ``httpx.ReadTimeout``
        (stuck LLM detection).  Defaults to ``LLM_TIMEOUT_SECONDS``.
        Only meaningful when ``stream=True``.
    task_id
        Task ID for budget logging (optional but recommended).
    llm_id
        **Required.** LLM endpoint ID - every call must reference an endpoint.
    budget_id
        **Required.** Budget ID - every call must be tracked.  Tokens and
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
        raise ValueError("call_llm() requires budget_id - every LLM call must be tracked.")
    if llm_id is None:
        raise ValueError("call_llm() requires llm_id - every LLM call must reference an endpoint.")

    resolved_url = base_url or LLM_BASE_URL
    resolved_model = model or LLM_MODEL

    # Strip null bytes from all message content strings before sending.
    # Null bytes in tool-result or file-content messages cause llama.cpp's
    # chat-template engine to fail with "Failed to parse input at pos N".
    messages = _sanitize_messages(messages)

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

    # ── Dispatch stagger ──────────────────────────────────────────────────────
    # Reserve a time slot so concurrent callers don't all arrive at the server
    # in the same millisecond.  Each caller atomically claims the next available
    # slot and sleeps until it opens.  This turns simultaneous burst dispatches
    # into a ~150ms-apart queue without any request ever failing due to collision.
    #
    # Model-switch: if this call uses a different llm_id than the last one on
    # this endpoint, the model may need to load.  Let THIS request through
    # immediately but push all subsequent slots _MODEL_LOAD_DELAY seconds out.
    with _ep_lock:
        st = _endpoint_states.setdefault(resolved_url, _EndpointState())
        _now = time.monotonic()

        if llm_id is not None and st.last_llm_id is not None and st.last_llm_id != llm_id:
            # Model switch: fire immediately, gate the next caller for the load window
            _reserved_at = _now
            st.next_dispatch_at = _now + _MODEL_LOAD_DELAY
            logger.info(
                "LLM endpoint %s: model switch (LLM %d → %d); "
                "%.0fs gap reserved for model load.",
                resolved_url, st.last_llm_id, llm_id, _MODEL_LOAD_DELAY,
            )
        else:
            # Normal stagger: claim the next available slot
            _reserved_at = max(_now, st.next_dispatch_at)
            st.next_dispatch_at = _reserved_at + _MIN_DISPATCH_GAP

        if llm_id is not None:
            st.last_llm_id = llm_id

    # Bail out before sleeping if shutdown was already signalled
    if _shutdown_event.is_set():
        raise ShutdownError("Server is shutting down")

    _stagger_sleep = _reserved_at - _now
    if _stagger_sleep > 0:
        logger.debug(
            "LLM dispatch stagger: %.2fs before sending to %s (queue depth ~%.0f)",
            _stagger_sleep, resolved_url, _stagger_sleep / _MIN_DISPATCH_GAP,
        )
        await asyncio.sleep(_stagger_sleep)
    # ─────────────────────────────────────────────────────────────────────────

    # Diagnostic tag prepended to all retry/error log messages so operators can
    # immediately see which agent triggered the failure without digging into payloads.
    _agent_label = f"[{agent_name}]" if agent_name else "[unknown agent]"

    while True:
        # Exit immediately if shutdown has been signalled.
        if _shutdown_event.is_set():
            raise ShutdownError("Server is shutting down")

        # Backoff gate - if endpoint is cooling down, sleep until it clears (never raise).
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
                    response.raise_for_status()
                    result = response.json()

            # Success - reset backoff state for this endpoint
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
                        "%s LLM endpoint %s unreachable (attempt %d/%d), retrying in %.0fs: %s",
                        _agent_label, resolved_url, st.fail_count, _BACKOFF_FREE_TRIES, wait, exc,
                    )
                else:
                    wait = st.delay
                    logger.warning(
                        "%s LLM endpoint %s unreachable (attempt %d), backing off %.0fs.",
                        _agent_label, resolved_url, st.fail_count, wait,
                    )
                    st.next_allowed = time.monotonic() + wait
                    st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)
            await asyncio.sleep(wait + random.uniform(0, wait * 0.5))
            # loop continues → retry after sleep

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                body_text = exc.response.text or ""
                payload_desc = _describe_payload(payload)

                # llama.cpp emits "Failed to parse input" for two distinct reasons:
                #
                # 1. Bad content in the payload (null bytes, control chars, Jinja2
                #    template delimiters) — the request will fail identically on every
                #    retry.  Non-retryable.  _sanitize_messages() prevents this in
                #    practice, but if something slips through, the corresponding flag
                #    ("NULL_BYTES", "CTRL_CHARS", "JINJA2_DELIMITERS") will appear in
                #    payload_desc and we fast-fail immediately.
                #
                # 2. Batch-slot / KV-cache contention — a transient capacity issue
                #    when concurrent requests fill the server's batch or context
                #    budget.  Retrying after backoff usually succeeds.
                #
                # Distinguish by checking whether the payload description flags any
                # suspicious content.  If clean → retry normally.
                _NONRETRYABLE_FLAGS = ("NULL_BYTES", "CTRL_CHARS", "JINJA2_DELIMITERS")
                if "Failed to parse input" in body_text and any(
                    f in payload_desc for f in _NONRETRYABLE_FLAGS
                ):
                    logger.warning(
                        "%s LLM endpoint %s returned %d (non-retryable: malformed content "
                        "in payload).\nError: %s\nPayload:\n%s",
                        _agent_label, url, exc.response.status_code, body_text[:300], payload_desc,
                    )
                    raise

                # Transient server error (overload, KV-cache full, proxy hiccup).
                # Back off and retry; log at WARNING so it's visible but not alarming.
                with _ep_lock:
                    st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                    st.fail_count += 1
                    if st.fail_count <= _BACKOFF_FREE_TRIES:
                        wait = _BACKOFF_BASE_DELAY
                        logger.warning(
                            "%s LLM endpoint %s returned %d (attempt %d/%d), retrying in "
                            "%.0fs.\nPayload:\n%s\nResponse: %s",
                            _agent_label, resolved_url, exc.response.status_code,
                            st.fail_count, _BACKOFF_FREE_TRIES, wait,
                            payload_desc, body_text[:300],
                        )
                    else:
                        wait = st.delay
                        logger.warning(
                            "%s LLM endpoint %s returned %d (attempt %d), backing off "
                            "%.0fs.\nPayload:\n%s\nResponse: %s",
                            _agent_label, resolved_url, exc.response.status_code, st.fail_count, wait,
                            payload_desc, body_text[:300],
                        )
                        st.next_allowed = time.monotonic() + wait
                        st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)
                await asyncio.sleep(wait + random.uniform(0, wait * 0.5))
                # loop continues → retry after sleep
            else:
                # 4xx: genuine request error - propagate immediately.
                logger.error(
                    "%s LLM call to %s returned %d.\nPayload:\n%s\nResponse: %s",
                    _agent_label, url, exc.response.status_code,
                    _describe_payload(payload), exc.response.text[:300],
                )
                raise

        except httpx.ReadTimeout as exc:
            # Server alive but too slow to respond within the timeout window.
            # Treat identically to a 5xx: back off and retry rather than
            # propagating immediately to the job scheduler.
            # (ConnectError/ConnectTimeout are caught above; ReadTimeout means
            # the connection was established but the server went silent.)
            with _ep_lock:
                st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                st.fail_count += 1
                if st.fail_count <= _BACKOFF_FREE_TRIES:
                    wait = _BACKOFF_BASE_DELAY
                    logger.warning(
                        "%s LLM endpoint %s timed out (attempt %d/%d), retrying in %.0fs.",
                        _agent_label, resolved_url, st.fail_count, _BACKOFF_FREE_TRIES, wait,
                    )
                else:
                    wait = st.delay
                    logger.warning(
                        "%s LLM endpoint %s timed out (attempt %d), backing off %.0fs.",
                        _agent_label, resolved_url, st.fail_count, wait,
                    )
                    st.next_allowed = time.monotonic() + wait
                    st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)
            await asyncio.sleep(wait + random.uniform(0, wait * 0.5))
            # loop continues → retry after sleep

        except Exception as exc:
            # RuntimeError("cannot schedule new futures after shutdown/interpreter shutdown")
            # is raised by asyncio's ThreadPoolExecutor (used for DNS resolution) once the
            # interpreter has begun tearing down.  Treat it as a clean shutdown signal so
            # agents exit quietly rather than logging a cascade of ERROR lines.
            if isinstance(exc, RuntimeError) and "cannot schedule new futures" in str(exc):
                raise ShutdownError(f"Interpreter shutting down: {exc}") from exc
            logger.error("LLM call failed to %s: %r (str: '%s')", url, exc, exc)
            raise  # JSON decode errors, etc. propagate to caller

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
