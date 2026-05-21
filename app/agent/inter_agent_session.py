"""
app/agent/inter_agent_session.py
---------------------------------
InterAgentSession — a slim, single-purpose LLM session that answers one
question from a peer agent running on a different task.

Called by the ask_agent tool handler in async_dispatch_tool().  The
session runs synchronously inline with the calling agent's LLM slot.
Budget entries are charged to the *calling* task (not the target) so the
cost appears in the calling task's trace, tagged agent_name="InterAgentSession".
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

AGENT_NAME = "InterAgentSession"
_MAX_TURNS = 5

_SYSTEM_PROMPT = """\
A peer agent has asked you a question about your current work.
Answer concisely and directly.  If you need to look at your work product \
to answer accurately, use your read tools.  Do not take actions or modify \
state — only answer the question.
"""

_TOOL_LIST = [
    "get_task",
    "list_tasks",
    "get_document",
    "get_project_summary",
    "get_module_summary",
    "list_scope_summaries",
]


class InterAgentSession:
    """
    Runs a short read-only LLM session scoped to the target agent's task,
    answering one question posed by a peer agent.

    The session inherits the calling session's LLM slot — no extra slot is
    reserved.  ask_depth is threaded so nested ask_agent calls within the
    sub-session see the correct depth.
    """

    def __init__(
        self,
        *,
        question: str,
        target_task_id: str,
        calling_task_id: str,
        calling_session_id: "int | None",
        ask_depth: int,
        llm_id: "int | None",
        budget_id: "int | None",
    ) -> None:
        self.question = question
        self.target_task_id = target_task_id
        self.calling_task_id = calling_task_id
        self.calling_session_id = calling_session_id
        self.ask_depth = ask_depth
        self.llm_id = llm_id
        self.budget_id = budget_id

    async def run(self) -> str:
        from app.agent.llm_client import call_llm, is_shutting_down
        from app.agent.tools import build_tool_schemas, async_dispatch_tool, _ask_depth_ctx

        if is_shutting_down():
            return "ERROR: Server is shutting down — cannot process inter-agent ask."

        context_block = self._build_context()
        system_content = _SYSTEM_PROMPT
        if context_block:
            system_content = f"{_SYSTEM_PROMPT}\n\n{context_block}"

        tool_schemas = build_tool_schemas(_TOOL_LIST)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_content},
            {"role": "user",   "content": self.question},
        ]

        # Resolve LLM endpoint
        from app.agent.config import LLM_BASE_URL, LLM_MODEL
        base_url = LLM_BASE_URL
        model = LLM_MODEL
        if self.llm_id is not None:
            try:
                from app.database import get_llm
                llm_rec = get_llm(self.llm_id)
                if llm_rec:
                    base_url = llm_rec.base_url or base_url
                    model = llm_rec.model or model
            except Exception as exc:
                logger.debug("InterAgentSession: LLM lookup failed: %s", exc)

        for turn in range(1, _MAX_TURNS + 1):
            if is_shutting_down():
                return "ERROR: Server is shutting down during inter-agent session."

            # Set ask_depth so nested ask_agent calls in this sub-session see it
            tok = _ask_depth_ctx.set(self.ask_depth)
            try:
                response = await call_llm(
                    messages,
                    base_url=base_url,
                    model=model,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    task_id=self.calling_task_id,
                    agent_name=AGENT_NAME,
                    tools=tool_schemas,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.warning("InterAgentSession LLM call failed on turn %d: %s", turn, exc)
                return f"ERROR: InterAgentSession LLM call failed: {exc}"
            finally:
                _ask_depth_ctx.reset(tok)

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
                        task_id=self.calling_task_id,
                        llm_id=self.llm_id,
                        budget_id=self.budget_id,
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "unknown"),
                        "name": name,
                        "content": result,
                    })
                continue

            if content:
                logger.info(
                    "InterAgentSession answered from task=%s to task=%s in %d turns",
                    self.target_task_id, self.calling_task_id, turn,
                )
                return content

        return "InterAgentSession exhausted its turn budget without producing an answer."

    def _build_context(self) -> str:
        try:
            from app.database import get_task as _db_get_task
            task = _db_get_task(self.target_task_id)
        except Exception:
            task = None

        if task is None:
            return ""

        parts = [
            f"**Target agent task:** {task.title}",
            f"**Stage:** {task.type or 'unknown'}",
        ]
        if task.description:
            parts.append(f"**Description:** {task.description}")
        return "\n".join(parts)
