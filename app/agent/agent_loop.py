"""
app/agent/agent_loop.py
-----------------------
AgentLoop — abstract base class for all Maestro turn-loop agents.
ReviewerLoop — concrete subclass for single-purpose verdict loops.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    MAX_CONSECUTIVE_ERRORS,
    check_context_saturation,
    check_turn_saturation,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.tools import async_dispatch_tool
from app.agent.verdicts import Vote, Verdict

logger = logging.getLogger(__name__)


class AgentLoop(ABC):
    """
    Abstract base class for Maestro turn-loop agents.

    Owns: all loop state (_messages, _turn, _consecutive_errors, etc.) and
    the run() skeleton including LLM call, tool dispatch, and terminal detection.

    Subclasses provide _build_messages(), _get_tool_schemas(), _on_terminal(),
    _on_max_turns(), and _on_error(). Override _on_no_tool_call() and
    _post_dispatch_hook() for custom idle/post-dispatch semantics.
    """

    # Class-level agent name used in LLM budget entries. Subclasses override.
    _agent_name: str = ""

    def __init__(
        self,
        *,
        task_id: str,
        llm_id: int | None,
        budget_id: int | None,
        max_turns: int,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        max_context: int | None = None,
    ) -> None:
        self.task_id = task_id
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_turns = max_turns
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.max_context = max_context or 0

        self._messages: list[dict] = []
        self._turn: int = 0
        self._consecutive_errors: int = 0
        self._no_tool_turns: int = 0
        self._warnings_fired: set[float] = set()
        self._turn_warnings_fired: set[int] = set()
        self._terminal_signal: dict | None = None
        self._total_prompt_tokens: int = 0
        self._total_completion_tokens: int = 0

    # ------------------------------------------------------------------
    # Abstract interface — subclasses must implement
    # ------------------------------------------------------------------

    @abstractmethod
    def _build_messages(self) -> list[dict]:
        """Return the initial message list for the conversation."""

    @abstractmethod
    def _get_tool_schemas(self) -> list[dict]:
        """Return the tool schemas for this agent."""

    @abstractmethod
    async def _on_terminal(self) -> Any:
        """Called when submit_work fires _terminal_signal. Return the agent result."""

    @abstractmethod
    async def _on_max_turns(self) -> Any:
        """Called when max_turns is exceeded. Return the agent result."""

    @abstractmethod
    async def _on_error(self, reason: str) -> Any:
        """Called on fatal error (LLM failures, shutdown). Return the agent result."""

    # ------------------------------------------------------------------
    # Overrideable hooks
    # ------------------------------------------------------------------

    async def _on_no_tool_call(self) -> Any:
        """
        Called when the LLM produces no tool calls.
        Default: nudge once, then delegate to _on_max_turns() on second idle turn.
        Returns None to continue the loop; any other value exits with that result.
        """
        self._no_tool_turns += 1
        if self._no_tool_turns >= 2:
            return await self._on_max_turns()
        self._messages.append({
            "role": "user",
            "content": (
                "[SYSTEM] You did not call any tool. "
                "You must either call a tool to make progress or call "
                "submit_work(signal='ACCEPTED', summary='...') to complete. "
                "Do not output free-form prose or raw JSON — use the submit_work tool call."
            ),
        })
        return None  # continue

    async def _post_dispatch_hook(
        self, tool_calls: list, result_messages: list[dict]
    ) -> Any:
        """
        Called after tool dispatch completes (but before error counting).
        Return None to continue normal processing; any other value exits the loop.
        Subclasses override for custom post-dispatch exit conditions.
        """
        return None

    # ------------------------------------------------------------------
    # Main loop entry point
    # ------------------------------------------------------------------

    async def run(self) -> Any:
        """Execute the agent turn loop."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(self._agent_name or self.__class__.__name__)

        if is_shutting_down():
            return await self._on_error("Server is shutting down")

        self._messages = self._build_messages()
        return await self._run_loop()

    async def _run_loop(self) -> Any:
        """Core turn loop. Subclasses that override run() call this directly."""
        while self._turn < self.max_turns:
            self._turn += 1
            logger.debug(
                "[%s] task '%s' — turn %d/%d",
                self._agent_name or self.__class__.__name__,
                self.task_id, self._turn, self.max_turns,
            )

            # ── LLM call ──────────────────────────────────────────────
            try:
                response = await self._call_llm(self._messages)
            except ShutdownError:
                return await self._on_error("Server is shutting down")
            except Exception as exc:
                self._turn -= 1
                self._consecutive_errors += 1
                logger.error(
                    "[%s] task '%s': LLM call failed: %s",
                    self._agent_name or self.__class__.__name__, self.task_id, exc,
                )
                if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    return await self._on_error(
                        f"LLM call failed {MAX_CONSECUTIVE_ERRORS} times: {exc}"
                    )
                self._messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Please continue.",
                })
                continue

            # ── Parse response ─────────────────────────────────────────
            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            self._messages.append(assistant_msg)

            usage = response.get("usage", {})
            prompt_tokens = usage.get("prompt_tokens", 0)
            self._total_prompt_tokens += prompt_tokens
            self._total_completion_tokens += usage.get("completion_tokens", 0)

            check_context_saturation(
                prompt_tokens, self.max_context,
                self._warnings_fired, self._messages,
                terminate_threshold=0,
            )
            check_turn_saturation(
                self._turn, self.max_turns,
                self._turn_warnings_fired, self._messages,
            )

            tool_calls = assistant_msg.get("tool_calls") or []

            if tool_calls:
                result_messages = await self._dispatch_tools(tool_calls)
                self._messages.extend(result_messages)

                if self._terminal_signal is not None:
                    return await self._on_terminal()

                hook_result = await self._post_dispatch_hook(tool_calls, result_messages)
                if hook_result is not None:
                    return hook_result

                # Count errors only from tool-role messages
                tool_msgs = [m for m in result_messages if m.get("role") == "tool"]
                all_errors = bool(tool_msgs) and all(
                    m.get("content", "").startswith("ERROR") for m in tool_msgs
                )
                if all_errors:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                        return await self._on_error(
                            f"Tool calls failed {MAX_CONSECUTIVE_ERRORS} times consecutively."
                        )
                else:
                    self._consecutive_errors = 0
                    self._no_tool_turns = 0
                continue

            # ── No tool calls ──────────────────────────────────────────
            result = await self._on_no_tool_call()
            if result is not None:
                return result

        return await self._on_max_turns()

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            tools=self._get_tool_schemas(),
            tool_choice="auto",
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            agent_name=self._agent_name or self.__class__.__name__,
        )

    # ------------------------------------------------------------------
    # Tool dispatch — single point for terminal detection
    # ------------------------------------------------------------------

    async def _dispatch_tools(self, tool_calls: list) -> list[dict]:
        """
        Dispatch each tool call and return tool-role messages.
        _terminal_signal is set here — nowhere else.
        """
        result_messages: list[dict] = []
        for tc in tool_calls:
            tool_id = tc.get("id", "unknown")
            function_block = tc.get("function", {})
            name = function_block.get("name", "")
            raw_args = function_block.get("arguments", "{}")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {}
                logger.warning(
                    "[%s] Failed to parse args for '%s'",
                    self._agent_name or self.__class__.__name__, name,
                )

            result = await self._execute_tool(name, arguments)
            result_str = str(result)

            result_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": result_str,
            })

            if name == "submit_work":
                try:
                    data = json.loads(result_str)
                    if data.get("__maestro_terminal__"):
                        self._terminal_signal = data
                        logger.info(
                            "[%s] task '%s': submit_work signal=%s",
                            self._agent_name or self.__class__.__name__,
                            self.task_id, data.get("signal"),
                        )
                except Exception:
                    pass

        return result_messages

    async def _execute_tool(self, name: str, arguments: dict) -> Any:
        """Execute a single tool. Override for custom dispatch (e.g. write containment)."""
        return await async_dispatch_tool(
            name, arguments,
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            llm_base_url=self.llm_base_url,
            llm_model=self.llm_model,
        )


# ---------------------------------------------------------------------------
# ReviewerLoop — concrete subclass for single-purpose verdict loops
# ---------------------------------------------------------------------------

class ReviewerLoop(AgentLoop):
    """
    Concrete subclass for 'read code → return verdict' loops.
    Used by ConceptualReviewPipeline, SecurityPipeline, FullReviewPipeline.
    run() returns a Vote object.
    """

    def __init__(
        self,
        stage_name: str,
        system_prompt: str,
        user_prompt: str,
        tool_schemas: list[dict],
        **base_kwargs: Any,
    ) -> None:
        super().__init__(**base_kwargs)
        self._stage_name = stage_name
        self._system_prompt = system_prompt
        self._user_prompt = user_prompt
        self._tool_schemas = tool_schemas

    def _build_messages(self) -> list[dict]:
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user", "content": self._user_prompt},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas

    async def _on_terminal(self) -> Vote:
        payload = self._terminal_signal.get("payload", {})
        return self._vote_from_payload(payload, self._stage_name)

    async def _on_max_turns(self) -> Vote:
        return Vote(
            stage=self._stage_name,
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=65,
            justification="Reviewer exhausted turns",
            model=self.llm_model or "",
        )

    async def _on_error(self, reason: str) -> Vote:
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")
        return Vote(
            stage=self._stage_name,
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=65,
            justification=f"Reviewer error: {reason}",
            model=self.llm_model or "",
        )

    async def _on_no_tool_call(self) -> Any:
        self._no_tool_turns += 1
        if self._no_tool_turns >= 2:
            return await self._on_max_turns()
        self._messages.append({
            "role": "user",
            "content": (
                "[SYSTEM] No tool was called. "
                "Call submit_work(signal='REVIEW_COMPLETE', summary='<one sentence>', "
                "payload={'verdict': '...', 'confidence': N, 'justification': '...'}) "
                "to submit your verdict."
            ),
        })
        return None

    def _vote_from_payload(self, payload: dict, stage: str) -> Vote:
        verdict_str = str(payload.get("verdict", "POSSIBLE")).upper()
        try:
            verdict = Verdict(verdict_str)
        except ValueError:
            verdict = Verdict.POSSIBLE
        confidence = int(payload.get("confidence", 70))
        lo, hi = verdict.confidence_range
        confidence = max(lo, min(hi, confidence))
        justification = payload.get("justification", "")
        severity = payload.get("severity", "")
        if severity in ("high", "critical"):
            tag = f"[{severity.upper()}]"
            if tag not in justification:
                justification = f"{tag} {justification}"
        return Vote(
            stage=stage,
            verdict=verdict,
            confidence=confidence,
            justification=justification,
            model=self.llm_model or "",
        )
