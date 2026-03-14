"""
app/agent/loop.py
-----------------
The Wiggum Loop — core orchestration engine for a single Maestro task.

MaestroLoop drives the LLM→tool-call→result→LLM cycle until one of:
  • The agent emits an ACCEPTED signal.
  • The agent emits a REVERT_TO_DESIGN signal.
  • max_turns is exceeded.
  • MAX_CONSECUTIVE_ERRORS consecutive tool errors occur.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

import httpx

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TEMPERATURE,
    LLM_TIMEOUT_SECONDS,
    MAX_TOKENS_PER_TURN,
    MAX_TURNS,
    MAX_CONSECUTIVE_ERRORS,
    SIGNAL_ACCEPTED,
    SIGNAL_REVERT,
    GIT_SAFETY_BRANCH_PREFIX,
)
from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, dispatch_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Result dataclass
# ---------------------------------------------------------------------------

@dataclass
class LoopResult:
    """Outcome of a single MaestroLoop run."""

    task_id: str
    status: Literal["ACCEPTED", "REVERT_TO_DESIGN", "MAX_TURNS", "ERROR"]
    turns: int
    final_message: str
    git_branch: str | None = None
    files_changed: list[str] = field(default_factory=list)
    error_detail: str | None = None


# ---------------------------------------------------------------------------
# Active loop registry (task_id → asyncio.Task)
# ---------------------------------------------------------------------------

_ACTIVE_LOOPS: dict[str, asyncio.Task] = {}
_LOOP_STATUS: dict[str, dict] = {}  # task_id → {status, turns, ...}


def get_loop_status(task_id: str) -> dict | None:
    """Return current status snapshot for a running or completed loop."""
    return _LOOP_STATUS.get(task_id)


def request_stop(task_id: str) -> bool:
    """
    Request graceful stop for a running loop.
    Returns True if the loop was found and cancelled.
    """
    task = _ACTIVE_LOOPS.get(task_id)
    if task and not task.done():
        task.cancel()
        return True
    return False


# ---------------------------------------------------------------------------
# Main loop class
# ---------------------------------------------------------------------------

class MaestroLoop:
    """
    Drives the LLM agent loop for a single Kanban task.

    Usage::

        loop = MaestroLoop(task_id="task-123")
        result = await loop.run()
    """

    def __init__(
        self,
        task_id: str,
        max_turns: int = MAX_TURNS,
    ) -> None:
        self.task_id = task_id
        self.max_turns = max_turns
        self._messages: list[dict] = []
        self._turn: int = 0
        self._consecutive_errors: int = 0
        self._stop_requested: bool = False
        self._git_branch: str | None = None
        self._files_changed: list[str] = []

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> LoopResult:
        """
        Execute the Wiggum Loop until a terminal condition is reached.
        Registers itself in _ACTIVE_LOOPS and updates _LOOP_STATUS.
        """
        # Register in the global registry
        current_task = asyncio.current_task()
        _ACTIVE_LOOPS[self.task_id] = current_task
        _LOOP_STATUS[self.task_id] = {
            "task_id": self.task_id,
            "status": "RUNNING",
            "turns": 0,
            "git_branch": None,
        }

        try:
            return await self._loop()
        except asyncio.CancelledError:
            logger.info("Loop for task '%s' was cancelled.", self.task_id)
            result = LoopResult(
                task_id=self.task_id,
                status="ERROR",
                turns=self._turn,
                final_message="Loop was stopped by external request.",
                git_branch=self._git_branch,
            )
            _LOOP_STATUS[self.task_id] = self._status_dict(result)
            return result
        except Exception as exc:
            logger.exception("Unexpected error in loop for task '%s'.", self.task_id)
            result = LoopResult(
                task_id=self.task_id,
                status="ERROR",
                turns=self._turn,
                final_message=f"Unexpected error: {exc}",
                git_branch=self._git_branch,
                error_detail=str(exc),
            )
            _LOOP_STATUS[self.task_id] = self._status_dict(result)
            return result
        finally:
            _ACTIVE_LOOPS.pop(self.task_id, None)

    # ------------------------------------------------------------------
    # Internal loop
    # ------------------------------------------------------------------

    async def _loop(self) -> LoopResult:
        """Core Do-While iteration."""
        # Seed the conversation with the task context
        self._messages = self._build_messages()

        while self._turn < self.max_turns:
            self._turn += 1
            _LOOP_STATUS[self.task_id]["turns"] = self._turn
            logger.debug("Task '%s' — turn %d/%d", self.task_id, self._turn, self.max_turns)

            # ── LLM call ──────────────────────────────────────────────
            try:
                response = await self._call_llm(self._messages)
            except Exception as exc:
                logger.error("LLM call failed on turn %d: %s", self._turn, exc)
                self._consecutive_errors += 1
                if self._check_failure_count():
                    return self._revert_result(f"LLM call failed {MAX_CONSECUTIVE_ERRORS} times: {exc}")
                # Append a synthetic error message and retry
                self._messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Please continue.",
                })
                continue

            # ── Parse response ─────────────────────────────────────────
            assistant_message = response.get("choices", [{}])[0].get("message", {})
            self._messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # ── Check for terminal signal in content ───────────────────
            terminal = self._extract_terminal_signal(content)
            if terminal:
                return self._handle_terminal(terminal)

            # ── Dispatch tool calls ────────────────────────────────────
            if tool_calls:
                tool_result_messages = self._handle_tool_calls(tool_calls)
                self._messages.extend(tool_result_messages)
                # Reset consecutive error counter if any tool succeeded
                if not all(
                    msg.get("content", "").startswith("ERROR")
                    for msg in tool_result_messages
                ):
                    self._consecutive_errors = 0
                else:
                    self._consecutive_errors += 1
                    if self._check_failure_count():
                        return self._revert_result(
                            f"Tool calls failed {MAX_CONSECUTIVE_ERRORS} times consecutively."
                        )
                continue

            # ── No tool calls and no terminal signal — nudge the agent ─
            if not tool_calls and not terminal:
                self._messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] You did not call any tool and did not emit a terminal signal. "
                        "You must either call a tool to make progress or emit your final JSON report. "
                        "Do not output free-form prose as a terminal action."
                    ),
                })

        # ── Max turns exceeded ─────────────────────────────────────────
        logger.warning("Task '%s' exceeded max turns (%d).", self.task_id, self.max_turns)
        result = LoopResult(
            task_id=self.task_id,
            status="MAX_TURNS",
            turns=self._turn,
            final_message=f"Max turns ({self.max_turns}) exceeded without reaching a terminal state.",
            git_branch=self._git_branch,
            files_changed=self._files_changed,
        )
        _LOOP_STATUS[self.task_id] = self._status_dict(result)
        return result

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        """
        Assemble the initial message list:
          [system_prompt, user_task_brief]
        """
        return [
            {"role": "system", "content": MAESTRO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Your assigned task ID is: **{self.task_id}**\n\n"
                    f"Begin by calling get_task('{self.task_id}') to load the full "
                    f"task definition, then follow the workflow in your system prompt.\n\n"
                    f"Your first action should be to create a safety branch: "
                    f"git_create_branch('{GIT_SAFETY_BRANCH_PREFIX}{self.task_id}').\n\n"
                    f"Proceed."
                ),
            },
        ]

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        """
        POST to the llama.cpp OpenAI-compatible endpoint.
        Returns the raw response dict.
        Raises httpx.HTTPError on network failures.
        """
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "tool_choice": "auto",
            "temperature": LLM_TEMPERATURE,
            "max_tokens": MAX_TOKENS_PER_TURN,
        }

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------

    def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
        """
        Dispatch each tool call and return a list of tool-role messages
        ready to be appended to the conversation.
        """
        result_messages: list[dict] = []

        for tc in tool_calls:
            tool_id = tc.get("id", "unknown")
            function_block = tc.get("function", {})
            name = function_block.get("name", "")
            raw_args = function_block.get("arguments", "{}")

            # Parse arguments JSON
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError as exc:
                arguments = {}
                logger.warning("Failed to parse tool arguments for '%s': %s", name, exc)

            logger.debug("Dispatching tool '%s' with args: %s", name, arguments)

            # Track git branch creation
            if name == "git_create_branch":
                branch = arguments.get("branch_name", "")
                if branch:
                    self._git_branch = branch
                    _LOOP_STATUS[self.task_id]["git_branch"] = branch

            # Track file writes for the final report
            if name in ("write_file", "append_file"):
                path = arguments.get("path", "")
                if path and path not in self._files_changed:
                    self._files_changed.append(path)

            # Dispatch
            result_content = dispatch_tool(name, arguments)

            result_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": result_content,
            })

        return result_messages

    # ------------------------------------------------------------------
    # Failure counting
    # ------------------------------------------------------------------

    def _check_failure_count(self) -> bool:
        """
        Return True if consecutive errors have reached the threshold,
        triggering a REVERT_TO_DESIGN signal.
        """
        return self._consecutive_errors >= MAX_CONSECUTIVE_ERRORS

    # ------------------------------------------------------------------
    # Terminal signal extraction
    # ------------------------------------------------------------------

    def _extract_terminal_signal(self, content: str) -> dict | None:
        """
        Scan the assistant content for a terminal JSON signal.
        Returns the parsed dict if found, else None.
        """
        if not content:
            return None
        # Try to find a JSON block in the content
        for attempt in [content, self._extract_json_block(content)]:
            if not attempt:
                continue
            try:
                parsed = json.loads(attempt.strip())
                if isinstance(parsed, dict) and "signal" in parsed:
                    sig = parsed["signal"]
                    if sig in (SIGNAL_ACCEPTED, SIGNAL_REVERT):
                        return parsed
            except (json.JSONDecodeError, ValueError):
                continue
        return None

    @staticmethod
    def _extract_json_block(text: str) -> str | None:
        """Extract the first ```json ... ``` or bare { ... } block from text."""
        import re
        # Try fenced code block
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if fenced:
            return fenced.group(1)
        # Try bare outermost JSON object
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end != -1 and end > start:
            return text[start : end + 1]
        return None

    # ------------------------------------------------------------------
    # Terminal handlers
    # ------------------------------------------------------------------

    def _handle_terminal(self, signal_dict: dict) -> LoopResult:
        """Convert a terminal signal dict into a LoopResult."""
        sig = signal_dict.get("signal")

        if sig == SIGNAL_ACCEPTED:
            result = LoopResult(
                task_id=self.task_id,
                status="ACCEPTED",
                turns=self._turn,
                final_message=signal_dict.get("summary", "Task accepted."),
                git_branch=signal_dict.get("git_branch") or self._git_branch,
                files_changed=signal_dict.get("files_changed") or self._files_changed,
            )
        else:  # REVERT_TO_DESIGN
            result = LoopResult(
                task_id=self.task_id,
                status="REVERT_TO_DESIGN",
                turns=self._turn,
                final_message=signal_dict.get("reason", "Reverting to design."),
                git_branch=self._git_branch,
                error_detail=signal_dict.get("advice"),
            )

        _LOOP_STATUS[self.task_id] = self._status_dict(result)
        return result

    def _revert_result(self, reason: str) -> LoopResult:
        """Construct a REVERT_TO_DESIGN LoopResult for internal failure cases."""
        result = LoopResult(
            task_id=self.task_id,
            status="REVERT_TO_DESIGN",
            turns=self._turn,
            final_message=reason,
            git_branch=self._git_branch,
        )
        _LOOP_STATUS[self.task_id] = self._status_dict(result)
        return result

    # ------------------------------------------------------------------
    # Status snapshot helper
    # ------------------------------------------------------------------

    def _status_dict(self, result: LoopResult) -> dict:
        return {
            "task_id": result.task_id,
            "status": result.status,
            "turns": result.turns,
            "git_branch": result.git_branch,
            "final_message": result.final_message,
        }
