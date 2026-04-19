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
import uuid
from contextvars import ContextVar
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
# Session identity context vars
# ---------------------------------------------------------------------------
# Each agent sets these once at the start of its run via set_llm_session_context().
# call_llm() uses them as the fallback when session_id/agent_name are not passed
# explicitly, so all LLM calls within an agent run share the same session_id
# without every call site needing to thread it through manually.

_ctx_session_id: ContextVar[str | None] = ContextVar('llm_session_id', default=None)
_ctx_agent_name: ContextVar[str | None] = ContextVar('llm_agent_name', default=None)


def set_llm_session_context(agent_name: str, session_id: str | None = None) -> str:
    """Set the session identity for all subsequent call_llm() calls on this task.

    Call this once at the start of each agent run (in run() or the thread entry
    point).  Returns the session_id so callers can log it if needed.

    A fresh UUID is generated when session_id is None.
    """
    sid = session_id or str(uuid.uuid4())
    _ctx_session_id.set(sid)
    _ctx_agent_name.set(agent_name)
    return sid


# ---------------------------------------------------------------------------
# Context-size guard
# ---------------------------------------------------------------------------

class ContextTooLargeError(Exception):
    """Raised when the prompt is estimated to exceed the model's context window.

    This is a **normal outcome**, not an infrastructure error.  Callers must
    treat it as a clean abort signal — do not retry, do not add more messages.
    The only sensible responses are: break the task into smaller pieces, or
    report the task as too large to process.
    """
    def __init__(self, estimated_tokens: int, max_context: int):
        self.estimated_tokens = estimated_tokens
        self.max_context = max_context
        super().__init__(
            f"Prompt estimated at {estimated_tokens:,} tokens exceeds context window "
            f"of {max_context:,} tokens — aborting before sending to LLM."
        )


# Cache max_context by llm_id so we don't hit the DB on every call.
# LLM configs are static during a server run; no TTL needed.
_llm_context_cache: dict[int, int] = {}

def _get_llm_max_context(llm_id: int) -> int | None:
    """Return the max_context for an LLM endpoint (DB-cached)."""
    if llm_id in _llm_context_cache:
        return _llm_context_cache[llm_id]
    try:
        from app.database import get_llm as _get_llm_rec
        rec = _get_llm_rec(llm_id)
        if rec and rec.max_context:
            _llm_context_cache[llm_id] = rec.max_context
            return rec.max_context
    except Exception:
        pass
    return None


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
_BACKOFF_MAX_DELAY: float = 900.0    # cap for connection errors: 15 minutes
_BACKOFF_RESPONSE_MAX_DELAY: float = 60.0  # cap for response/timeout errors: 1 minute
_MIN_DISPATCH_GAP: float = 0.15      # seconds between consecutive dispatches to same endpoint
_MODEL_LOAD_DELAY: float = 10.0      # seconds to gate subsequent requests after a model switch


@dataclass
class _EndpointState:
    fail_count_connect: int = 0      # ConnectError / ConnectTimeout (Server DOWN)
    fail_count_response: int = 0     # ReadTimeout / 5xx / Parse Error (Server OVERLOADED / BAD PROMPT)
    next_allowed: float = 0.0        # monotonic timestamp; 0 = not in cooldown
    delay: float = field(default=_BACKOFF_BASE_DELAY)
    next_dispatch_at: float = 0.0    # earliest slot available for next dispatch
    last_llm_id: "int | None" = None  # llm_id of the most recently dispatched request

    @property
    def fail_count(self) -> int:
        """Total failure count for backward compatibility in logs."""
        return self.fail_count_connect + self.fail_count_response


_endpoint_states: dict[str, _EndpointState] = {}
_ep_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Per-endpoint concurrency semaphores
#
# Each LLM server has a fixed number of KV-cache slots (parallel_sessions).
# _endpoint_semaphores gates concurrent in-flight HTTP requests so we never
# send more requests than the server can service simultaneously.  The semaphore
# is a threading.Semaphore (not asyncio) so it works across the multiple
# independent event loops created by scheduler worker threads.  Callers acquire
# via run_in_executor so the waiting coroutine yields the event loop instead of
# blocking its OS thread.
#
# Capacity is read from llm.parallel_sessions on first use and cached.  Restart
# the server to pick up changes to LLM configuration.
# ---------------------------------------------------------------------------

_endpoint_semaphores: dict[str, threading.Semaphore] = {}

def _get_or_create_semaphore(base_url: str, llm_id: int) -> threading.Semaphore:
    """Return the concurrency semaphore for *base_url*, creating it if needed."""
    with _ep_lock:
        if base_url in _endpoint_semaphores:
            return _endpoint_semaphores[base_url]
        if llm_id not in _llm_capacity_cache:
            try:
                from app.database import get_llm
                llm_obj = get_llm(llm_id)
                cap = int(llm_obj.parallel_sessions) if llm_obj and llm_obj.parallel_sessions else 1
            except Exception:
                cap = 1
            _llm_capacity_cache[llm_id] = cap
        capacity = _llm_capacity_cache[llm_id]
        sem = threading.Semaphore(capacity)
        _endpoint_semaphores[base_url] = sem
        logger.info(
            "LLM endpoint %s: concurrency semaphore created (%d slot(s), LLM %d).",
            base_url, capacity, llm_id,
        )
        return sem

_llm_capacity_cache: dict[int, int] = {}

def invalidate_llm_cache(llm_id: int | None = None):
    """Clear the context cache, capacity cache, and semaphores for an LLM (or all)."""
    with _ep_lock:
        if llm_id is not None:
            _llm_context_cache.pop(llm_id, None)
            _llm_capacity_cache.pop(llm_id, None)
            # Semaphores are indexed by base_url. Since multiple LLM IDs can share a URL,
            # and we don't want to hit the DB here to find the URL, we clear all
            # semaphores. In-flight calls continue using their existing semaphore
            # objects; new calls will create fresh ones with updated capacity.
            _endpoint_semaphores.clear()
        else:
            _llm_context_cache.clear()
            _llm_capacity_cache.clear()
            _endpoint_semaphores.clear()


def update_llm_context_cache(llm_id: int, max_context: int | None):
    """Update or remove an LLM's max_context in the cache directly."""
    if max_context is None:
        _llm_context_cache.pop(llm_id, None)
    else:
        _llm_context_cache[llm_id] = max_context


# ---------------------------------------------------------------------------
# Graceful shutdown flags
#
# Two-phase shutdown:
#   Phase 1 — signal_shutdown(): sets _shutdown_event.  In-flight calls exit at
#     their next between-chunk check (effectively within one token interval for
#     streaming calls).  No new work is dispatched.
#   Phase 2 — signal_force_shutdown(): sets _force_shutdown_event.  The streaming
#     poll loop checks this flag every _SHUTDOWN_POLL_SLICE seconds even while
#     waiting for the first token, providing a hard-deadline interrupt for calls
#     that are still in the prompt-processing (first-token latency) window.
# ---------------------------------------------------------------------------

_shutdown_event = threading.Event()
_force_shutdown_event = threading.Event()

# Maximum seconds between shutdown-flag checks while waiting for the next SSE
# chunk.  Applies only when _shutdown_event or _force_shutdown_event is set and
# the LLM has not yet produced a token.
_SHUTDOWN_POLL_SLICE: float = 1.0


class ShutdownError(Exception):
    """Raised when an LLM call is aborted because the server is shutting down."""
    pass


class PipelineAbortedError(Exception):
    """Raised when a pipeline stage must abort due to an infrastructure failure.

    Distinct from ShutdownError (deliberate shutdown) and application errors
    (JSON parse, schema mismatch). The task's stage is left unchanged so the
    scheduler can re-dispatch when the LLM endpoint recovers.
    """
    def __init__(self, stage: str, cause: Exception):
        self.stage = stage
        self.cause = cause
        super().__init__(f"Stage '{stage}' aborted due to infra error: {cause}")


def signal_shutdown() -> None:
    """Phase-1 shutdown: signal in-flight calls to abort at their next check."""
    _shutdown_event.set()


def signal_force_shutdown() -> None:
    """Phase-2 shutdown: interrupt streaming waits within _SHUTDOWN_POLL_SLICE seconds."""
    _force_shutdown_event.set()


def is_shutting_down() -> bool:
    """Return True once signal_shutdown() has been called."""
    return _shutdown_event.is_set()


def is_force_shutdown() -> bool:
    """Return True once signal_force_shutdown() has been called."""
    return _force_shutdown_event.is_set()


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

    import json as _json
    async with httpx.AsyncClient(timeout=http_timeout) as client:
        async with client.stream(
            "POST", url,
            content=_json.dumps(stream_payload, ensure_ascii=True).encode("ascii"),
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
                    if _shutdown_event.is_set() or _force_shutdown_event.is_set():
                        raise ShutdownError("Server is shutting down")

                    # Slice the idle-timeout wait into _SHUTDOWN_POLL_SLICE-second
                    # windows so the shutdown flag is checked even during first-token
                    # latency (prompt-processing window).  The else-branch of the
                    # inner while fires when elapsed >= idle_timeout (no break).
                    elapsed = 0.0
                    got_stop = False
                    line = ""
                    while elapsed < idle_timeout:
                        if _shutdown_event.is_set() or _force_shutdown_event.is_set():
                            raise ShutdownError("Server is shutting down")
                        try:
                            line = await asyncio.wait_for(
                                lines_aiter.__anext__(),
                                timeout=min(_SHUTDOWN_POLL_SLICE, idle_timeout - elapsed),
                            )
                            break  # got a chunk
                        except StopAsyncIteration:
                            got_stop = True
                            break
                        except asyncio.TimeoutError:
                            elapsed += _SHUTDOWN_POLL_SLICE
                    else:
                        raise httpx.ReadTimeout(
                            f"No token from {url} for {idle_timeout:.0f}s - LLM may be stuck"
                        )

                    if got_stop:
                        break

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


def extract_text_response(response: dict) -> str:
    """Extract the best available text from an OpenAI-compatible chat completion response.

    Handles two layouts produced by thinking models (Qwen3, QwQ, DeepSeek-R1)
    running under llama.cpp with ``enable_thinking``:

    - Normal layout: ``choices[0].message.content`` holds the visible text.
    - Split layout: ``choices[0].message.reasoning_content`` holds the raw
      thinking block and ``content`` is empty.  In this case we fall back to
      ``reasoning_content`` and strip the ``<think>…</think>`` wrapper so the
      caller gets the naked inner text (which may itself contain a JSON object).

    Always returns a string (never None).
    """
    msg = response.get("choices", [{}])[0].get("message", {})
    content = msg.get("content") or ""
    if content.strip():
        return content
    # Fallback: some thinking models put all output in reasoning_content and
    # leave content empty.  Strip the <think> wrapper to get the inner text.
    reasoning = msg.get("reasoning_content") or ""
    if reasoning.strip():
        return _strip_thinking_blocks(reasoning)
    return content  # still empty — caller must handle this


def _strip_thinking_blocks(content: str) -> str:
    """Remove model-internal reasoning blocks from assistant content.

    Thinking models (Qwen3, QwQ, DeepSeek-R1, …) emit reasoning wrapped in
    <think>…</think> tags before their actual response.  These blocks are
    model-private — the model re-generates its own reasoning on every turn and
    does NOT need the prior turns' thinking in the conversation history.

    Keeping them in history:
      - Wastes context tokens on noise the model already "knows".
      - Confuses non-thinking models that receive the same history.
      - Can cause llama.cpp Jinja2 parse errors for deeply-nested content.

    Patterns stripped (case-insensitive, dotall):
      <think>…</think>          — Qwen3 / QwQ / DeepSeek-R1
      <thinking>…</thinking>    — some fine-tunes
    """
    import re as _re
    _THINK_RE = _re.compile(
        r'<(think|thinking)>.*?</\1>',
        _re.IGNORECASE | _re.DOTALL,
    )
    stripped = _THINK_RE.sub("", content).strip()
    return stripped


def _sanitize_messages(messages: list[dict]) -> list[dict]:
    """Return a copy of *messages* with problematic bytes/sequences removed or escaped.

    Sanitization is ALWAYS performed to prevent llama.cpp Jinja2 parse errors.
    - system/user roles: Log a WARNING to help debug prompt/brief issues.
    - assistant/tool roles: Silent (corrects legacy or "garbage" data).

    Additionally, <think>/<thinking> blocks are stripped from assistant messages
    so that thinking-model reasoning is never forwarded to downstream models
    (whether thinking or non-thinking).
    """
    _JINJA2_PAIRS = [("{{", "{ {"), ("}}", "} }"), ("{%", "{ %"), ("{#", "{ #")]
    _UNICODE_REPLACEMENTS = [
        ("\u2014", " -- "), ("\u2013", " - "), ("\u2192", " -> "), ("\u2190", " <- "),
        ("\u2018", "'"), ("\u2019", "'"), ("\u201c", '"'), ("\u201d", '"'),
        ("\u2026", "..."), ("\u00b7", "."), ("\u2022", "-"), ("\u2605", "*"),
    ]

    result = []
    for i, msg in enumerate(messages):
        content = msg.get("content")
        if not isinstance(content, str):
            result.append(msg)
            continue

        role = msg.get("role", "?")
        changed = False
        log_warn = role in ("system", "user")

        # Strip model-internal reasoning blocks from assistant history.
        # Thinking models re-derive their reasoning each turn; prior thinking
        # blocks are noise for the same model and confusing for other models.
        if role == "assistant":
            stripped = _strip_thinking_blocks(content)
            if stripped != content:
                logger.debug(
                    "msg[%d] (assistant): Stripped thinking block (%d → %d chars)",
                    i, len(content), len(stripped),
                )
                content = stripped
                changed = True

        if "\x00" in content:
            if log_warn:
                logger.warning("msg[%d] (%s): Stripping null bytes", i, role)
            content = content.replace("\x00", "")
            changed = True

        for raw, safe in _JINJA2_PAIRS:
            if raw in content:
                if log_warn:
                    count = content.count(raw)
                    idx = content.find(raw)
                    snip = content[max(0, idx - 40):idx + len(raw) + 40].replace("\n", "↵")
                    logger.warning(
                        "msg[%d] (%s): Escaping Jinja2 delimiter %r ×%d — near: %r",
                        i, role, raw, count, snip,
                    )
                content = content.replace(raw, safe)
                changed = True

        for raw, safe in _UNICODE_REPLACEMENTS:
            if raw in content:
                content = content.replace(raw, safe)
                changed = True

        import re as _re
        _SINGLE_BRACE_RE = _re.compile(r'\{([A-Za-z_][\w.]*)\}')
        _sbrace_match = _SINGLE_BRACE_RE.search(content)
        if _sbrace_match:
            if log_warn:
                snip = content[max(0, _sbrace_match.start() - 40):_sbrace_match.end() + 40].replace("\n", "↵")
                logger.warning(
                    "msg[%d] (%s): Escaping single-brace identifiers ×%d — near: %r",
                    i, role, len(_SINGLE_BRACE_RE.findall(content)), snip,
                )
            content = _SINGLE_BRACE_RE.sub(r'[\1]', content)
            changed = True

        _non_ascii = sum(1 for c in content if ord(c) > 127)
        if _non_ascii:
            import unicodedata as _ud
            if log_warn:
                logger.warning("msg[%d] (%s): Stripping %d residual non-ASCII chars", i, role, _non_ascii)
            content = _ud.normalize("NFKD", content).encode("ascii", "ignore").decode("ascii")
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

        model=Qwen3p5-Omnicoder  messages=5  tools=8  total_chars=12400 (~4133 tokens)
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

    total_chars = sum(
        len(m["content"]) if isinstance(m.get("content"), str) else 0
        for m in messages
    )
    if tools:
        import json as _json
        total_chars += sum(len(_json.dumps(t)) for t in tools)

    # estimation matching pre-flight (3 chars/token)
    est_tokens = total_chars // 3

    lines.append(
        f"model={model}  messages={len(messages)}  tools={len(tools)}  "
        f"total_chars={total_chars} (~{est_tokens} tokens)"
    )

    for i, msg in enumerate(messages):
        role = msg.get("role", "?")
        content = msg.get("content")
        tool_calls = msg.get("tool_calls") or []

        tc_str = f" (tool_calls={len(tool_calls)})" if tool_calls else ""
        
        if isinstance(content, str):
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
            import re as _re
            _sb_count = len(_re.findall(r'\{[A-Za-z_][\w.]*\}', content))
            if _sb_count:
                flags.append(f"SINGLE_BRACES:{_sb_count}")
            flag_str = f"  [{' '.join(flags)}]" if flags else ""
            lines.append(f"  [{i}] {role:<12} {len(content):5} chars{tc_str}{flag_str}")
        else:
            lines.append(f"  [{i}] {role:<12} {tc_str or '(no content)'}")

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
    max_retries: int | None = None,  # Max consecutive connect failures before raising; None = unlimited
    total_timeout_secs: float | None = None,  # Wall-clock deadline for the entire call (incl. backoff sleeps)
    # Budget tracking - when provided, the call is logged automatically
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    # Diagnostic labels - agent_name appears in log messages; session_id groups all
    # calls from one agent run so the diagnostics page can reconstruct sessions exactly.
    agent_name: str | None = None,
    session_id: str | None = None,
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

    # Drop trailing empty assistant messages before sending.
    # llama.cpp with enable_thinking treats any trailing assistant turn as a
    # "prefill" request (continue-from-here), which conflicts with thinking mode
    # and returns HTTP 400: "Assistant response prefill is incompatible with
    # enable_thinking."  An assistant message at the tail with empty/null content
    # and no tool_calls is never meaningful — it's safe to strip.
    if (
        messages
        and messages[-1].get("role") == "assistant"
        and not messages[-1].get("tool_calls")
        and not (messages[-1].get("content") or "").strip()
    ):
        _agent_label_pre = f"[{agent_name}]" if agent_name else "[Agent]"
        logger.debug(
            "%s Stripping trailing empty assistant message to avoid enable_thinking prefill rejection",
            _agent_label_pre,
        )
        messages = messages[:-1]

    # ── Pre-flight context size check ────────────────────────────────────────
    # Estimate the prompt token count from total message character count.
    # We use 3 chars/token (conservative — overestimates tokens, so we reject
    # earlier rather than later, which is the safe direction).
    # Raise *before* any HTTP call so callers can handle it as a clean abort
    # rather than burning a retry budget on a guaranteed-to-fail request.
    _resolved_max_context = _get_llm_max_context(llm_id) if llm_id else None
    if _resolved_max_context:
        _total_chars = sum(
            len(m["content"]) if isinstance(m.get("content"), str) else 0
            for m in messages
        )

        if tools:
            import json as _json
            _total_chars += sum(len(_json.dumps(t)) for t in tools)

        _max_output = (max_tokens or MAX_TOKENS_PER_TURN)
        _available = _resolved_max_context - _max_output
        # using 3 chars per token to more accurately reflect code
        _estimated = _total_chars // 3
        if _estimated > _available:
            _agent_label = f"[{agent_name}]" if agent_name else "[Agent]"
            logger.error(
                "%s Pre-flight context check failed: estimated %d tokens > available %d "
                "(max_context=%d minus max_tokens=%d). Refusing to send %d chars to LLM.",
                _agent_label, _estimated, _available,
                _resolved_max_context, _max_output, _total_chars,
            )
            raise ContextTooLargeError(_estimated, _resolved_max_context)
    # ────────────────────────────────────────────────────────────────────────

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

    # Concurrency semaphore for this endpoint.  Acquired once per HTTP attempt,
    # released in the finally block below — the slot is always free during backoff
    # sleeps so other callers can proceed without waiting.
    _sem = _get_or_create_semaphore(resolved_url, llm_id)
    _loop = asyncio.get_running_loop()

    _total_retry_count = 0
    _call_deadline = (time.monotonic() + total_timeout_secs) if total_timeout_secs is not None else None
    while True:
        # Exit immediately if shutdown has been signalled.
        if _shutdown_event.is_set():
            raise ShutdownError("Server is shutting down")

        # Wall-clock deadline check.
        if _call_deadline is not None and time.monotonic() >= _call_deadline:
            raise RuntimeError(
                f"LLM call to {resolved_url} deadline exceeded ({total_timeout_secs:.0f}s total_timeout_secs)."
            )

        # Backoff gate - if endpoint is cooling down, sleep until it clears (never raise).
        # All concurrent callers cooperate on the shared next_allowed timestamp.
        with _ep_lock:
            _state = _endpoint_states.get(resolved_url)
            remaining = (_state.next_allowed - time.monotonic()) if _state else 0.0
        if remaining > 0:
            if _call_deadline is not None and time.monotonic() + remaining >= _call_deadline:
                raise RuntimeError(
                    f"LLM endpoint {resolved_url} in backoff ({remaining:.0f}s) — "
                    f"would exceed call deadline ({total_timeout_secs:.0f}s), giving up."
                )
            logger.debug(
                "LLM endpoint %s in backoff (%.0fs remaining), sleeping.",
                resolved_url, remaining,
            )
            await asyncio.sleep(remaining)
            continue  # re-check after sleep

        # Acquire a concurrency slot.  Suspends this coroutine (without blocking
        # the event-loop thread) until the server has capacity for one more request.
        # The slot is held only for a single HTTP attempt and released in `finally`.
        await _loop.run_in_executor(None, _sem.acquire)

        _retry_wait: "float | None" = None
        try:
            if _shutdown_event.is_set():
                raise ShutdownError("Server is shutting down")

            if stream:
                # Streaming: per-chunk idle timeout; clock runs only during generation.
                result = await _stream_llm_response(url, payload, _idle_timeout)
            else:
                import json as _json
                async with httpx.AsyncClient(timeout=_http_timeout) as client:
                    response = await client.post(
                        url,
                        content=_json.dumps(payload, ensure_ascii=True).encode("ascii"),
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
            # _retry_wait stays None -> break after finally

        except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
            # Infrastructure problem: server not running.  Log at WARNING, not ERROR.
            # Update shared backoff state so all concurrent callers cooperate.
            with _ep_lock:
                st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                st.fail_count_connect += 1
                if st.fail_count_connect <= _BACKOFF_FREE_TRIES:
                    wait = _BACKOFF_BASE_DELAY
                    logger.warning(
                        "%s LLM endpoint %s unreachable (attempt %d/%d), retrying in %.0fs: %s",
                        _agent_label, resolved_url, st.fail_count_connect, _BACKOFF_FREE_TRIES, wait, exc,
                    )
                else:
                    wait = st.delay
                    logger.warning(
                        "%s LLM endpoint %s unreachable (attempt %d), backing off %.0fs.",
                        _agent_label, resolved_url, st.fail_count_connect, wait,
                    )
                    st.next_allowed = time.monotonic() + wait
                    st.delay = min(st.delay * 2.0, _BACKOFF_MAX_DELAY)

            _total_retry_count += 1
            if max_retries is not None and _total_retry_count >= max_retries:
                raise RuntimeError(
                    f"LLM endpoint {resolved_url} unreachable after {_total_retry_count} attempt(s)."
                ) from exc
            _retry_wait = wait + random.uniform(0, wait * 0.5)

        except httpx.HTTPStatusError as exc:
            if exc.response.status_code >= 500:
                body_text = exc.response.text or ""
                payload_desc = _describe_payload(payload)

                # llama.cpp returns 500 when the model generates a tool call whose
                # `arguments` field contains invalid JSON (e.g. unescaped quotes from
                # triple-quoted Python strings in write_file content).  Retrying the
                # same message history will produce the same broken output — fail fast
                # so the caller (component_loop, MaestroLoop) can count the failure and
                # inject a correction turn or trigger REVERT_TO_DESIGN.
                if "Failed to parse tool call arguments" in body_text:
                    logger.warning(
                        "%s LLM endpoint %s returned %d (non-retryable: model generated"
                        " invalid tool call arguments JSON — likely unescaped quotes in"
                        " write_file content).\nError: %s\nPayload:\n%s",
                        _agent_label, url, exc.response.status_code,
                        body_text[:300], payload_desc,
                    )
                    raise

                # llama.cpp emits "Failed to parse input" for two distinct reasons:
                #
                # 1. Bad content in the payload (null bytes, control chars, Jinja2
                #    template delimiters) — the request will fail identically on every
                #    retry.  Non-retryable.  _sanitize_messages() catches these and
                #    payload_desc will show a flag ("NULL_BYTES", "CTRL_CHARS",
                #    "JINJA2_DELIMITERS").  Fast-fail only in that case.
                #
                # 2. Batch-slot / KV-cache contention — a transient capacity issue
                #    seen with multi-batch models (e.g. 9BATCH).  The sanitizer finds
                #    nothing wrong; the failure position varies across retries.
                #    Retrying after backoff succeeds.  Treat as retryable.
                if "Failed to parse input" in body_text:
                    import re as _re

                    # ── Structured parse-error context ────────────────────────────
                    # Build a message-by-message breakdown showing cumulative char
                    # counts and estimated template-space positions, so we can
                    # pinpoint which message contains the bad character without
                    # manual math.
                    #
                    # Qwen3 chat-template overhead per message:
                    #   <|im_start|>{role}\n{content}<|im_end|>\n
                    #   ≈ 13 + len(role) + 2 + len(content) + 12  (chars)
                    # role overhead (not counting content): system=31, user=29,
                    #   assistant=34, tool=29.  We use 30 as a round estimate.
                    # Final generation prefix: <|im_start|>assistant\n ≈ 20 chars.
                    _PER_MSG_OVERHEAD = 30
                    _FINAL_OVERHEAD = 20

                    _msgs = payload.get("messages", [])
                    _total_content_chars = sum(
                        len(msg.get("content", ""))
                        for msg in _msgs
                        if isinstance(msg.get("content"), str)
                    )
                    _threshold = int(_total_content_chars * 1.5) + 512

                    m = _re.search(r"pos (\d+)", body_text)
                    _is_content_error = m is not None and int(m.group(1)) <= _threshold
                    pos = int(m.group(1)) if m else 0

                    # Walk messages, estimating each one's template-space range.
                    _detail_lines: list[str] = []
                    _running = 0           # estimated template-space cursor
                    _error_msg_idx = -1    # which message the error falls in
                    _error_msg_offset = 0  # char offset within that message
                    _error_msg_content = ""

                    for _mi, _mm in enumerate(_msgs):
                        _role = _mm.get("role", "?")
                        _content = _mm.get("content")
                        _tcs = _mm.get("tool_calls") or []
                        _tcid = _mm.get("tool_call_id", "")

                        _msg_start = _running
                        _running += _PER_MSG_OVERHEAD  # role markers
                        _content_start = _running      # where content begins

                        if isinstance(_content, str):
                            _clen = len(_content)
                            _running += _clen
                            _in_range = _content_start <= pos < _content_start + _clen
                            if _in_range and _error_msg_idx == -1:
                                _error_msg_idx = _mi
                                _error_msg_offset = pos - _content_start
                                _error_msg_content = _content
                            _marker = " ← ERROR HERE" if _in_range else ""

                            if _clen <= 120:
                                # Short enough to show in full
                                _preview = _content.encode("ascii", errors="replace").decode("ascii")
                                _detail_lines.append(
                                    f"  msg[{_mi}] {_role:<12} {_clen:5} chars"
                                    f"  tpl[{_msg_start}..{_running}]"
                                    f"  \"{_preview}\"{_marker}"
                                )
                            else:
                                _detail_lines.append(
                                    f"  msg[{_mi}] {_role:<12} {_clen:5} chars"
                                    f"  tpl[{_msg_start}..{_running}]{_marker}"
                                )
                        elif _tcs:
                            _running += 60  # rough estimate for tool_calls JSON
                            _detail_lines.append(
                                f"  msg[{_mi}] {_role:<12}  (tool_calls={len(_tcs)})"
                                f"  tpl[{_msg_start}..{_running}]"
                            )
                        else:
                            _detail_lines.append(
                                f"  msg[{_mi}] {_role:<12}  (empty)"
                                f"  tpl[{_msg_start}]"
                            )

                    _running += _FINAL_OVERHEAD
                    _detail_lines.append(
                        f"  Total raw content: {_total_content_chars} chars"
                        f"  |  estimated template span: 0..{_running}"
                        f"  |  threshold: {_threshold}"
                        f"  |  -> {'CONTENT ERROR' if _is_content_error else 'transient contention'}"
                    )

                    # Snippet around the estimated error position
                    if _error_msg_idx >= 0:
                        _snip_start = max(0, _error_msg_offset - 80)
                        _snip_end = min(len(_error_msg_content), _error_msg_offset + 300)
                        _snip = _error_msg_content[_snip_start:_snip_end]
                        _cursor = _error_msg_offset - _snip_start  # where the ^ goes
                        _snip_ascii = _snip.encode("ascii", errors="replace").decode("ascii")
                        _detail_lines.append(
                            f"\n  Error in msg[{_error_msg_idx}]"
                            f" ({_msgs[_error_msg_idx].get('role', '?')})"
                            f" at content offset {_error_msg_offset}"
                            f" (chars {_snip_start}-{_snip_end} shown):\n"
                            f"  {_snip_ascii}\n"
                            f"  {' ' * _cursor}^"
                        )
                    elif not _is_content_error:
                        _detail_lines.append(
                            f"\n  pos {pos} is beyond all message content"
                            f" - KV/batch-space position, this is transient contention."
                        )
                    else:
                        _detail_lines.append(
                            f"\n  pos {pos} couldn't be mapped to a specific message"
                            f" - check template overhead estimate."
                        )

                    logger.warning(
                        "%s Parse error at pos %d:\n%s",
                        _agent_label, pos, "\n".join(_detail_lines),
                    )

                    _content_flags = ("NULL_BYTES:", "CTRL_CHARS:", "JINJA2_DELIMITERS:", "SINGLE_BRACES:")
                    if _is_content_error or any(f in payload_desc for f in _content_flags):
                        logger.warning(
                            "%s LLM endpoint %s returned %d (non-retryable: content error).\n"
                            "Error: %s\nPayload:\n%s",
                            _agent_label, url, exc.response.status_code, body_text[:300], payload_desc,
                        )
                        raise
                    logger.warning(
                        "%s LLM endpoint %s returned %d"
                        " (parse error at pos >> content - transient batch contention, will retry).\n"
                        "Error: %s\nPayload:\n%s",
                        _agent_label, url, exc.response.status_code, body_text[:300], payload_desc,
                    )

                # Transient server error (overload, KV-cache full, proxy hiccup).
                # Back off and retry; log at WARNING so it's visible but not alarming.
                with _ep_lock:
                    st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                    st.fail_count_response += 1
                    if st.fail_count_response <= _BACKOFF_FREE_TRIES:
                        wait = _BACKOFF_BASE_DELAY
                        logger.warning(
                            "%s LLM endpoint %s returned %d (attempt %d/%d), retrying in "
                            "%.0fs.\nPayload:\n%s\nResponse: %s",
                            _agent_label, resolved_url, exc.response.status_code,
                            st.fail_count_response, _BACKOFF_FREE_TRIES, wait,
                            payload_desc, body_text[:300],
                        )
                    else:
                        wait = st.delay
                        logger.warning(
                            "%s LLM endpoint %s returned %d (attempt %d), backing off "
                            "%.0fs.\nPayload:\n%s\nResponse: %s",
                            _agent_label, resolved_url, exc.response.status_code, st.fail_count_response, wait,
                            payload_desc, body_text[:300],
                        )
                        st.next_allowed = time.monotonic() + wait
                        st.delay = min(st.delay * 2.0, _BACKOFF_RESPONSE_MAX_DELAY)
                _total_retry_count += 1
                if max_retries is not None and _total_retry_count >= max_retries:
                    raise RuntimeError(
                        f"LLM endpoint {resolved_url} returned {exc.response.status_code} "
                        f"after {_total_retry_count} attempt(s)."
                    ) from exc
                _retry_wait = wait + random.uniform(0, wait * 0.5)
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
            payload_desc = _describe_payload(payload)
            with _ep_lock:
                st = _endpoint_states.setdefault(resolved_url, _EndpointState())
                st.fail_count_response += 1
                if st.fail_count_response <= _BACKOFF_FREE_TRIES:
                    wait = _BACKOFF_BASE_DELAY
                    logger.warning(
                        "%s LLM endpoint %s timed out (attempt %d/%d), retrying in %.0fs.\n"
                        "Payload:\n%s",
                        _agent_label, resolved_url, st.fail_count_response, _BACKOFF_FREE_TRIES, wait,
                        payload_desc
                    )
                else:
                    wait = st.delay
                    logger.warning(
                        "%s LLM endpoint %s timed out (attempt %d), backing off %.0fs.\n"
                        "Payload:\n%s",
                        _agent_label, resolved_url, st.fail_count_response, wait,
                        payload_desc
                    )
                    st.next_allowed = time.monotonic() + wait
                    st.delay = min(st.delay * 2.0, _BACKOFF_RESPONSE_MAX_DELAY)

            _total_retry_count += 1
            if max_retries is not None and _total_retry_count >= max_retries:
                raise RuntimeError(
                    f"LLM endpoint {resolved_url} timed out after {_total_retry_count} attempt(s)."
                ) from exc
            _retry_wait = wait + random.uniform(0, wait * 0.5)

        except Exception as exc:
            # RuntimeError("cannot schedule new futures after shutdown/interpreter shutdown")
            # is raised by asyncio's ThreadPoolExecutor (used for DNS resolution) once the
            # interpreter has begun tearing down.  Treat it as a clean shutdown signal so
            # agents exit quietly rather than logging a cascade of ERROR lines.
            if isinstance(exc, RuntimeError) and "cannot schedule new futures" in str(exc):
                raise ShutdownError(f"Interpreter shutting down: {exc}") from exc
            logger.error("LLM call failed to %s: %r (str: '%s')", url, exc, exc)
            raise  # JSON decode errors, etc. propagate to caller

        finally:
            # Always release the slot — even on raise — so other waiters can proceed.
            _sem.release()

        # Slot released.  Break on success (_retry_wait is None) or sleep before retry.
        if _retry_wait is None:
            break
        if _call_deadline is not None:
            budget = _call_deadline - time.monotonic()
            if budget <= 0:
                raise RuntimeError(
                    f"LLM call to {resolved_url} deadline exceeded ({total_timeout_secs:.0f}s total_timeout_secs)."
                )
            _retry_wait = min(_retry_wait, budget)
        await asyncio.sleep(_retry_wait)
        # loop continues → retry

    # Log every call to budget_entries.
    # Fall back to the context vars so agents that set set_llm_session_context()
    # get correct session grouping without passing session_id to every call site.
    _log_budget_entry(
        result, messages,
        task_id=task_id, llm_id=llm_id, budget_id=budget_id,
        agent_name=agent_name or _ctx_agent_name.get(),
        session_id=session_id or _ctx_session_id.get(),
    )

    return result


def _log_budget_entry(
    response: dict,
    messages: list[dict],
    *,
    task_id: str | None,
    llm_id: int | None,
    budget_id: int | None,
    agent_name: str | None = None,
    session_id: str | None = None,
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
            session_id=session_id,
            agent_name=agent_name,
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
