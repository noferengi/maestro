"""
app/agent/pip_resolution.py
---------------------------
PIP Resolution Agent — targeted implementation agent that closes quality gaps
identified by the PIP Pre-flight Verifier.

Lifecycle (driven by the scheduler):
  1. _dispatch_pip_resolution_jobs() creates a daemon thread targeting
     _run_pip_resolution_agent(job, task, llm) in scheduler.py.
  2. PIPResolutionAgent.run() drives the LLM → tool → LLM cycle on the
     existing maestro/task-{id} branch.
  3. The agent exits when:
       a. It stops calling tools (requirements satisfied — natural completion).
       b. It emits {"signal": "RESOLUTION_STALLED"} after repeated failures.
       c. max_turns is exceeded.
  4. signal_completion(f"pip_resolution_{pip_id}") always fires on exit.
  5. The scheduler detects this signal, marks the job done, and re-dispatches
     the parent stage so the pre-flight can run again.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.config import (
    PIP_RESOLUTION_MAX_TURNS,
    GIT_SAFETY_BRANCH_PREFIX,
    INDEV_AGENT_TOOLS,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.tools import async_dispatch_tool, build_tool_schemas

logger = logging.getLogger(__name__)
AGENT_NAME = "PIP Resolution Agent"
SIGNAL_STALLED = "RESOLUTION_STALLED"
_MAX_CONSECUTIVE_ERRORS = 3

_RESOLUTION_TOOL_SCHEMAS: list[dict] = build_tool_schemas(INDEV_AGENT_TOOLS)


class PIPResolutionAgent:
    """
    Targeted implementation agent that closes PIP quality gaps.

    Operates on the existing maestro/task-{id} branch, making minimal,
    focused changes to satisfy the specific requirements from a PIP that
    failed the pre-flight gate.
    """

    def __init__(
        self,
        task_id: str,
        pip_id: int,
        requirements: list[str],
        research_findings: str,
        last_verification_findings: list[dict],
        project_root: str | None,
        llm_id: int,
        budget_id: int,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        max_context: int | None = None,
        task_title: str = "",
        origin_stage: str = "",
    ) -> None:
        self.task_id = task_id
        self.pip_id = pip_id
        self.requirements = requirements
        self.research_findings = research_findings
        self.last_verification_findings = last_verification_findings
        self.project_root = project_root
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.max_context = max_context
        self.task_title = task_title
        self.origin_stage = origin_stage

        self._messages: list[dict] = []
        self._turn: int = 0
        self._consecutive_errors: int = 0
        self._no_tool_turns: int = 0
        self._warnings_fired: set[float] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """
        Execute the resolution agent loop.

        Returns {"status": "done" | "stalled" | "max_turns" | "error", "turns": int}.
        "done"      — agent stopped calling tools (requirements satisfied).
        "stalled"   — RESOLUTION_STALLED signal or consecutive tool failures.
        "max_turns" — turn cap exceeded.
        "error"     — server shutting down or unexpected exception.
        """
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            return {"status": "error", "turns": 0}

        from app.agent.tools import set_task_git_cwd
        if self.project_root:
            set_task_git_cwd(self.project_root)

        self._messages = self._build_messages()
        max_turns = PIP_RESOLUTION_MAX_TURNS

        while self._turn < max_turns:
            self._turn += 1
            logger.debug(
                "[pip_resolution] pip %d task '%s' — turn %d/%d",
                self.pip_id, self.task_id, self._turn, max_turns,
            )

            # LLM call
            try:
                response = await self._call_llm(self._messages)
            except ShutdownError:
                logger.info("[pip_resolution] pip %d — shutdown requested.", self.pip_id)
                return {"status": "error", "turns": self._turn}
            except Exception as exc:
                self._turn -= 1
                self._consecutive_errors += 1
                logger.error("[pip_resolution] pip %d LLM call failed: %s", self.pip_id, exc)
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    logger.warning(
                        "[pip_resolution] pip %d — %d consecutive LLM failures, stalling.",
                        self.pip_id, _MAX_CONSECUTIVE_ERRORS,
                    )
                    return {"status": "stalled", "turns": self._turn}
                self._messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Please continue.",
                })
                continue

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            self._messages.append(assistant_message)

            usage = response.get("usage", {})
            self._maybe_inject_context_warning(usage.get("prompt_tokens", 0))

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # Dispatch tool calls
            if tool_calls:
                result_messages = await self._handle_tool_calls(tool_calls)
                self._messages.extend(result_messages)
                all_errors = all(
                    m.get("content", "").startswith("ERROR")
                    for m in result_messages
                )
                if all_errors:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        logger.warning(
                            "[pip_resolution] pip %d — %d consecutive tool errors, stalling.",
                            self.pip_id, _MAX_CONSECUTIVE_ERRORS,
                        )
                        return {"status": "stalled", "turns": self._turn}
                else:
                    self._consecutive_errors = 0
                    self._no_tool_turns = 0
                continue

            # No tool calls — check for RESOLUTION_STALLED signal
            if content:
                try:
                    block = extract_json_block(content)
                    if block:
                        parsed = json.loads(block)
                        if isinstance(parsed, dict) and parsed.get("signal") == SIGNAL_STALLED:
                            logger.warning(
                                "[pip_resolution] pip %d signalled RESOLUTION_STALLED.",
                                self.pip_id,
                            )
                            return {"status": "stalled", "turns": self._turn}
                except Exception:
                    pass

            # No tool calls, no stall signal — first time: nudge; second time: done
            self._no_tool_turns += 1
            if self._no_tool_turns >= 2:
                logger.info(
                    "[pip_resolution] pip %d — agent stopped calling tools after %d turns (done).",
                    self.pip_id, self._turn,
                )
                return {"status": "done", "turns": self._turn}

            self._messages.append({
                "role": "user",
                "content": (
                    "[SYSTEM] No tool was called. Use your tools to make targeted changes "
                    "that satisfy the PIP requirements, or emit "
                    '{"signal": "RESOLUTION_STALLED"} if you cannot proceed.'
                ),
            })

        logger.warning(
            "[pip_resolution] pip %d — max_turns (%d) exceeded.",
            self.pip_id, max_turns,
        )
        return {"status": "max_turns", "turns": self._turn}

    # ------------------------------------------------------------------
    # Message building
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        """Build the initial system prompt for the resolution agent."""
        from app.agent.project_snapshot import build_project_snapshot, build_architecture_context
        from app.database import get_task as _get_task

        snapshot = ""
        arch_block = ""
        task_title = self.task_title
        try:
            if self.project_root:
                snapshot = build_project_snapshot(self.project_root)
            task_rec = _get_task(self.task_id)
            if task_rec:
                task_title = task_rec.title or task_title
                if task_rec.project:
                    arch = build_architecture_context(task_rec.project, agent_type="loop")
                    if arch:
                        arch_block = f"\n\nARCHITECTURE CONTEXT:\n{arch}"
        except Exception as exc:
            logger.debug("[pip_resolution] Context build warning: %s", exc)

        req_bullets = "\n".join(f"- {r}" for r in self.requirements)
        branch = f"{GIT_SAFETY_BRANCH_PREFIX}{self.task_id}"

        findings_text = "(no structured findings from last verification)"
        if self.last_verification_findings:
            lines = []
            for f in self.last_verification_findings:
                status = f.get("status", "?").upper()
                req = f.get("requirement", "")
                detail = f.get("detail", "")
                lines.append(f"  [{status}] {req}: {detail}")
            findings_text = "\n".join(lines)

        system_prompt = (
            "You are the Maestro PIP Resolution Agent.\n\n"
            "Your sole objective is to satisfy the specific requirements listed below. "
            "These requirements represent quality debts from a prior demotion. "
            "The implementation agent has already completed the core work — "
            "you are here to close the remaining gaps.\n\n"
            f"TASK: {task_title}\n"
            f"BRANCH: {branch}\n\n"
            f"PIP REQUIREMENTS (task was demoted from: {self.origin_stage}):\n"
            f"{req_bullets}\n\n"
            f"WHAT FAILED IN THE LAST VERIFICATION:\n"
            f"{findings_text}\n\n"
            f"RESEARCH FINDINGS — WHAT WORK IS NEEDED:\n"
            f"{self.research_findings or 'No research findings available.'}\n\n"
            f"PROJECT SNAPSHOT:\n{snapshot}"
            f"{arch_block}\n\n"
            "Work iteratively. Read the relevant files first. Make targeted, minimal changes. "
            "Commit your changes with clear messages referencing the PIP requirement. "
            "Stop when you are confident every requirement above is satisfied.\n"
            "Do NOT expand scope beyond these requirements.\n"
            f"After {_MAX_CONSECUTIVE_ERRORS} consecutive tool failures, stop and emit "
            '{"signal": "RESOLUTION_STALLED"}.'
        )

        return [{"role": "system", "content": system_prompt}]

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            tools=_RESOLUTION_TOOL_SCHEMAS,
            tool_choice="auto",
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            agent_name=AGENT_NAME,
        )

    # ------------------------------------------------------------------
    # Tool dispatch
    # ------------------------------------------------------------------

    async def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
        result_messages = []
        for tc in tool_calls:
            tool_id = tc.get("id", "unknown")
            function_block = tc.get("function", {})
            name = function_block.get("name", "")
            raw_args = function_block.get("arguments", "{}")
            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {}
            try:
                result = await async_dispatch_tool(
                    name,
                    arguments,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    llm_base_url=self.llm_base_url,
                    llm_model=self.llm_model,
                )
            except Exception as exc:
                result = f"ERROR: tool '{name}' raised: {exc}"
            result_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "content": str(result),
            })
        return result_messages

    # ------------------------------------------------------------------
    # Context warning injection
    # ------------------------------------------------------------------

    def _maybe_inject_context_warning(self, prompt_tokens: int) -> None:
        if not self.max_context:
            return
        check_context_saturation(
            prompt_tokens,
            self.max_context,
            self._warnings_fired,
            self._messages,
            terminate_threshold=0,
        )
