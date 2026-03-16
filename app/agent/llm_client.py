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

import json
import logging
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
        HTTP timeout in seconds.  Defaults to ``LLM_TIMEOUT_SECONDS``.
    tools
        Optional list of OpenAI tool schemas.  Omitted from the payload
        when *None*.
    tool_choice
        Tool-choice strategy (e.g. ``"auto"``).  Omitted when *None*.
    response_format
        Response format hint (e.g. ``{"type": "json_object"}``).
        Omitted when *None*.
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
    httpx.TimeoutException
        On request timeout.
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

    url = f"{resolved_url}/chat/completions"
    logger.debug("LLM call -> %s  model=%s", url, resolved_model)

    async with httpx.AsyncClient(timeout=timeout or LLM_TIMEOUT_SECONDS) as client:
        response = await client.post(
            url,
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        result = response.json()

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

        create_budget_entry(
            llm_id=llm_id,
            budget_id=budget_id,
            task_id=task_id,
            prompt_cost=prompt_tokens,
            generation_cost=completion_tokens,
            tool_calls=total_turns,
            prompt_data=prompt_json,
            response_data=response_json,
        )
    except Exception:
        logger.debug("Failed to log budget entry", exc_info=True)
