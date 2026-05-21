"""
app/agent/consult_agent.py
--------------------------
ConsultAgent — a slim Maestro-mode session that answers an escalated question
from an inner agent and returns the answer synchronously.

Called by handle_consult_maestro() in async_dispatch_tool().  The calling
agent's session continues with the returned answer as a normal tool result.
"""

from __future__ import annotations

import logging
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    CONSULT_AGENT_MAX_TURNS,
    ORCHESTRATION_LLM_ID,
)

logger = logging.getLogger(__name__)
AGENT_NAME = "ConsultAgent"

_CONSULT_SYSTEM_PROMPT = """\
You are The Maestro, the orchestrating intelligence for this software project.
An inner agent has escalated a question that requires architectural judgment or \
domain knowledge beyond its current context.

Answer concisely and decisively.  Use your tools to pull in relevant documents, \
summaries, or task details if needed before answering.  The calling agent will \
receive your answer as a tool result and continue its session.

Do NOT call consult_maestro — that tool is not available to you.
When you have a complete answer, respond with plain text.  Do not call any more \
tools after you have enough information to answer.
"""


def _resolve_maestro_llm(project_maestro_llm_id: "int | None") -> "int | None":
    """
    Resolve the LLM ID for ConsultAgent.

    Priority: project.maestro_llm_id → [orchestration] maestro_llm_id (ini/env)
              → system_setting maestro_llm_id → None
    """
    if project_maestro_llm_id is not None:
        return project_maestro_llm_id
    if ORCHESTRATION_LLM_ID is not None:
        return ORCHESTRATION_LLM_ID
    try:
        from app.database import get_system_setting as _gss
        val = _gss("maestro_llm_id")
        if val is not None:
            return int(val)
    except Exception:
        pass
    return None


def _build_consult_context(task_id: str, project_name: "str | None") -> str:
    """Build arch-card + document-titles context block for the system prompt."""
    parts: list[str] = []

    # Architecture cards for the project
    try:
        from app.agent.project_snapshot import build_architecture_context
        arch = build_architecture_context(project_name or "", agent_type=None)
        if arch:
            parts.append(f"## Architecture Cards\n{arch}")
    except Exception as exc:
        logger.debug("ConsultAgent: arch context unavailable: %s", exc)

    # Document store titles
    try:
        from app.database import list_documents_by_project
        docs = list_documents_by_project(project_name) if project_name else []
        if docs:
            titles = "\n".join(f"- {d['key']}" for d in docs)
            parts.append(f"## Project Documents (use get_document to read)\n{titles}")
    except Exception as exc:
        logger.debug("ConsultAgent: document list unavailable: %s", exc)

    return "\n\n".join(parts)


_CONSULT_TOOLS = [
    "get_task",
    "list_tasks",
    "get_document",
    "get_project_summary",
    "get_module_summary",
    "list_scope_summaries",
]


async def run_consult_agent(
    question: str,
    task_id: str,
    caller_llm_id: "int | None",
    budget_id: "int | None",
    project_name: "str | None" = None,
    project_maestro_llm_id: "int | None" = None,
    max_turns: int = CONSULT_AGENT_MAX_TURNS,
) -> str:
    """
    Spin up a short Maestro-mode LLM session to answer ``question``.

    Returns the answer string, or an error message prefixed with "ERROR:".
    Budget entries are charged to the same task_id, tagged role=consult.
    """
    from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
    from app.agent.tools import build_tool_schemas, async_dispatch_tool
    import json

    if is_shutting_down():
        return "ERROR: Server is shutting down — cannot consult Maestro."

    # Resolve which LLM ConsultAgent uses
    llm_id = _resolve_maestro_llm(project_maestro_llm_id)
    if llm_id is None:
        llm_id = caller_llm_id  # graceful fallback

    if llm_id is None:
        return (
            "ERROR: No Maestro LLM configured.  Set maestro_llm_id in the project "
            "settings, [orchestration] maestro_llm_id in maestro.ini, or the "
            "maestro_llm_id system setting."
        )

    # Look up LLM endpoint
    try:
        from app.database import get_llm
        llm_rec = get_llm(llm_id)
    except Exception:
        llm_rec = None

    base_url = (llm_rec.base_url if llm_rec else None) or LLM_BASE_URL
    model    = (llm_rec.model    if llm_rec else None) or LLM_MODEL
    max_context = (llm_rec.max_context if llm_rec else 0) or 0

    # Build context block
    context_block = _build_consult_context(task_id, project_name)

    system_content = _CONSULT_SYSTEM_PROMPT
    if context_block:
        system_content = f"{_CONSULT_SYSTEM_PROMPT}\n\n{context_block}"

    tool_schemas = build_tool_schemas(_CONSULT_TOOLS)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_content},
        {"role": "user",   "content": question},
    ]

    answer: str = ""

    for turn in range(1, max_turns + 1):
        if is_shutting_down():
            return "ERROR: Server is shutting down during ConsultAgent run."

        try:
            response = await call_llm(
                messages,
                base_url=base_url,
                model=model,
                llm_id=llm_id,
                budget_id=budget_id,
                task_id=task_id,
                agent_name=AGENT_NAME,
                tools=tool_schemas,
                tool_choice="auto",
            )
        except Exception as exc:
            logger.warning("ConsultAgent LLM call failed on turn %d: %s", turn, exc)
            return f"ERROR: ConsultAgent LLM call failed: {exc}"

        msg = response.get("choices", [{}])[0].get("message", {})
        messages.append(msg)

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()

        if tool_calls:
            for tc in tool_calls:
                name = tc.get("function", {}).get("name", "")
                raw_args = tc.get("function", {}).get("arguments", "{}")
                try:
                    args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except json.JSONDecodeError:
                    args = {}
                result = await async_dispatch_tool(
                    name, args,
                    task_id=task_id,
                    llm_id=llm_id,
                    budget_id=budget_id,
                )
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", "unknown"),
                    "name": name,
                    "content": result,
                })
            continue

        # No tool calls — the model produced its answer
        if content:
            answer = content
            break

    if not answer:
        answer = "ConsultAgent exhausted its turn budget without producing an answer."

    logger.info(
        "ConsultAgent answered task=%s in %d turns (llm_id=%s)",
        task_id, turn, llm_id,
    )
    return answer
