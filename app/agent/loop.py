"""
app/agent/loop.py
-----------------
The Wiggum Loop - core orchestration engine for a single Maestro task.

MaestroLoop drives the LLM -> tool-call -> result -> LLM cycle until one of:
  * The agent emits an ACCEPTED signal.
  * The agent emits a REVERT_TO_DESIGN signal.
  * max_turns is exceeded.
  * MAX_CONSECUTIVE_ERRORS consecutive tool errors occur.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Literal

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    MAX_TURNS,
    MAX_CONSECUTIVE_ERRORS,
    PROJECT_ROOT,
    SIGNAL_ACCEPTED,
    SIGNAL_REVERT,
    SIGNAL_NEEDS_RESEARCH,
    SIGNAL_CONTEXT_TOO_LARGE,
    GIT_SAFETY_BRANCH_PREFIX,
    INDEV_AGENT_TOOLS,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT
from app.agent.tools import TOOL_SCHEMAS, dispatch_tool, async_dispatch_tool, build_tool_schemas

_INDEV_TOOL_SCHEMAS: list[dict] = build_tool_schemas(INDEV_AGENT_TOOLS)

logger = logging.getLogger(__name__)
AGENT_NAME = "Maestro Loop"


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
_LOOP_STATUS: dict[str, dict] = {}  # task_id -> {status, turns, ...}


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
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        max_context: int | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        project_path: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.max_turns = max_turns
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.max_context = max_context
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.project_path = project_path
        self._messages: list[dict] = []
        self._turn: int = 0
        self._consecutive_errors: int = 0
        self._stop_requested: bool = False
        self._git_branch: str | None = None
        self._files_changed: list[str] = []
        self._last_prompt_tokens: int = 0
        self._warnings_fired: set[float] = set()
        self._turn_warnings_fired: set[int] = set()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> LoopResult:
        """
        Execute the Wiggum Loop until a terminal condition is reached.
        Registers itself in _ACTIVE_LOOPS and updates _LOOP_STATUS.
        """
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        # Register in the global registry
        current_task = asyncio.current_task()
        _ACTIVE_LOOPS[self.task_id] = current_task
        _LOOP_STATUS[self.task_id] = {
            "task_id": self.task_id,
            "status": "RUNNING",
            "turns": 0,
            "git_branch": None,
        }

        if self.project_path:
            from app.agent.tools import set_task_git_cwd
            set_task_git_cwd(self.project_path)
            logger.info("Task '%s': git cwd set to '%s'.", self.task_id, self.project_path)

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
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        # Pre-warm file summaries for the project (fire-and-forget)
        _project_root = getattr(self, 'project_path', None) or PROJECT_ROOT
        if getattr(self, 'llm_id', None) is not None:
            try:
                from app.agent.project_snapshot import prewarm_project_summaries
                import asyncio as _asyncio
                await _asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: prewarm_project_summaries(
                        _project_root,
                        llm_id=self.llm_id,
                        budget_id=getattr(self, 'budget_id', None),
                        task_id=self.task_id,
                    ),
                )
            except Exception as exc:
                logger.warning("prewarm failed (non-fatal): %s", exc)

        # Seed the conversation with the task context
        self._messages = self._build_messages()

        while self._turn < self.max_turns:
            self._turn += 1
            _LOOP_STATUS[self.task_id]["turns"] = self._turn
            logger.debug("Task '%s' - turn %d/%d", self.task_id, self._turn, self.max_turns)

            # ── LLM call ──────────────────────────────────────────────
            try:
                response = await self._call_llm(self._messages)
            except Exception as exc:
                # Failed call does not count as a turn - roll back the increment.
                self._turn -= 1
                _LOOP_STATUS[self.task_id]["turns"] = self._turn
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

            # ── Track token usage & inject context/turn warnings ───────
            usage = response.get("usage", {})
            self._last_prompt_tokens = usage.get("prompt_tokens", 0)
            self._maybe_inject_context_warning()
            self._maybe_inject_turn_warning()

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # ── Check for terminal / research signal in content ────────
            signal = self._extract_signal(content)
            if signal:
                sig_type = signal.get("signal")
                if sig_type in (SIGNAL_ACCEPTED, SIGNAL_REVERT):
                    return self._handle_terminal(signal)
                if sig_type == SIGNAL_NEEDS_RESEARCH:
                    research_result = await self._handle_needs_research(signal)
                    self._messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] Research agent completed.\n"
                            f"Verdict: {research_result.get('verdict', 'unknown')}\n"
                            f"Findings:\n{research_result.get('findings', 'No findings.')}\n\n"
                            "Continue your work incorporating these findings."
                        ),
                    })
                    self._consecutive_errors = 0
                    continue
                if sig_type == SIGNAL_CONTEXT_TOO_LARGE:
                    return self._revert_result(
                        "Agent signalled CONTEXT_TOO_LARGE — task scope exceeds context budget."
                    )

            if tool_calls:
                tool_result_messages = await self._handle_tool_calls(tool_calls)
                self._messages.extend(tool_result_messages)
                
                # Check for timeouts in tool results
                has_timeout = any(
                    "ERROR: Command timed out" in msg.get("content", "")
                    for msg in tool_result_messages
                )
                if has_timeout:
                    logger.info("MaestroLoop detected shell timeout for task '%s' - triggering research.", self.task_id)
                    research_signal = {
                        "signal": SIGNAL_NEEDS_RESEARCH,
                        "question": (
                            "The last shell command timed out. Investigate the source code and tests "
                            "to see if there is an infinite loop, a deadlock, or a high-complexity "
                            "algorithm (like naive Fibonacci) being called with large inputs in a test."
                        ),
                        "context": "A shell command timed out during implementation."
                    }
                    research_result = await self._handle_needs_research(research_signal)
                    self._messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] The shell command timed out, and a Research Agent was triggered to investigate.\n"
                            f"Verdict: {research_result.get('verdict', 'unknown')}\n"
                            f"Findings:\n{research_result.get('findings', 'No findings.')}\n\n"
                            "Based on these findings, fix the implementation or the tests to avoid the timeout."
                        ),
                    })
                    # Reset consecutive errors since we've handled the timeout with research
                    self._consecutive_errors = 0
                    continue

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

            # ── No tool calls and no signal - nudge the agent ─────────
            if not tool_calls and not signal:
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
        _project_path = getattr(self, 'project_path', None) or None
        snapshot_block = ""
        if _project_path:
            try:
                from app.agent.project_snapshot import build_snapshot_with_summaries
                from app.agent.config import SNAPSHOT_CONTEXT_RATIO
                _snap_max = (
                    int(self.max_context * SNAPSHOT_CONTEXT_RATIO)
                    if self.max_context else None
                )
                snapshot_block = f"\n\n{build_snapshot_with_summaries(_project_path, max_tokens=_snap_max)}"
            except Exception:
                pass

        # Inject architecture context - look up the task's project by task_id
        arch_block = ""
        pip_block = ""
        try:
            from app.database import get_task as _get_task, get_pips_for_task as _get_pips
            from app.agent.project_snapshot import build_architecture_context
            _task_rec = _get_task(self.task_id)
            if _task_rec and _task_rec.project:
                _arch = build_architecture_context(_task_rec.project, agent_type='loop')
                if _arch:
                    arch_block = f"\n\n{_arch}"
                
                # Fetch PIPs for this task
                pips = _get_pips(self.task_id)
                if pips:
                    pip_block = "\n\n### HISTORICAL PERFORMANCE IMPROVEMENT PLANS (PIPs)\n"
                    pip_block += "This task has previously failed review/optimization. You MUST satisfy ALL requirements below:\n"
                    for i, pip in enumerate(pips):
                        reqs = json.loads(pip.requirements)
                        pip_block += f"\nPIP {i+1} (from {pip.origin_stage}, status: {pip.status}):\n"
                        for req in reqs:
                            pip_block += f"- {req}\n"
        except Exception:
            pass

        return [
            {"role": "system", "content": MAESTRO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Your assigned task ID is: **{self.task_id}**"
                    f"{snapshot_block}{arch_block}{pip_block}\n\n"
                    f"Begin by calling get_task('{self.task_id}') to load the full "
                    f"task definition, including the approved PLANNING result "
                    f"(file_manifest, implementation_steps, interface_contracts). "
                    f"Then follow the workflow in your system prompt.\n\n"
                    f"Your first action should be to create a safety branch: "
                    f"git_create_branch('{GIT_SAFETY_BRANCH_PREFIX}{self.task_id}').\n\n"
                    f"Proceed."
                ),
            },
        ]

    # ------------------------------------------------------------------
    # Context window warnings
    # ------------------------------------------------------------------

    def _maybe_inject_context_warning(self) -> None:
        """Inject a warning message if token usage crosses a threshold."""
        if not self.max_context:
            return
        # terminate_threshold=0 disables hard termination - MaestroLoop uses its own signal system
        check_context_saturation(
            self._last_prompt_tokens,
            self.max_context,
            self._warnings_fired,
            self._messages,
            terminate_threshold=0,
        )

    def _maybe_inject_turn_warning(self) -> None:
        """Inject a warning message if tool-call turns are running low."""
        from app.agent.config import check_turn_saturation
        check_turn_saturation(
            self._turn,
            self.max_turns,
            self._turn_warnings_fired,
            self._messages,
        )

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        """
        POST to the OpenAI-compatible endpoint.
        Returns the raw response dict.
        Raises httpx.HTTPError on network failures.
        """
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            tools=_INDEV_TOOL_SCHEMAS,
            tool_choice="auto",
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            agent_name=AGENT_NAME,
        )

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------

    async def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
        """
        Dispatch each tool call and return a list of tool-role messages
        ready to be appended to the conversation.
        Uses async_dispatch_tool so spawn_research_agent works properly.
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

            # Dispatch (async - handles spawn_research_agent correctly)
            result_content = await async_dispatch_tool(
                name, arguments,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
            )

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

    def _extract_signal(self, content: str) -> dict | None:
        """
        Scan the assistant content for a JSON signal dict.
        Recognizes ACCEPTED, REVERT_TO_DESIGN, and NEEDS_RESEARCH.
        Returns the parsed dict if found, else None.
        """
        if not content:
            return None
        for attempt in [content, extract_json_block(content)]:
            if not attempt:
                continue
            try:
                parsed = json.loads(attempt.strip())
                if isinstance(parsed, dict) and "signal" in parsed:
                    sig = parsed["signal"]
                    if sig in (SIGNAL_ACCEPTED, SIGNAL_REVERT, SIGNAL_NEEDS_RESEARCH, SIGNAL_CONTEXT_TOO_LARGE):
                        return parsed
            except (json.JSONDecodeError, ValueError):
                continue
        return None


    # ------------------------------------------------------------------
    # NEEDS_RESEARCH handler
    # ------------------------------------------------------------------

    async def _handle_needs_research(self, signal_dict: dict) -> dict:
        """
        Run a research agent inline, record the job, and return findings.
        The loop continues after this - it is not a terminal action.
        """
        from app.agent.research import run_research
        from app.database import create_research_job, update_research_job

        question = signal_dict.get("question", "What do I need to investigate?")
        context_str = signal_dict.get("context", "")

        job = create_research_job(
            task_id=self.task_id,
            question=question,
            context=json.dumps({"question": question, "context": context_str}),
            priority=0.0,  # inline = highest priority
            depth=0,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
        )

        try:
            if job:
                update_research_job(job.id, status="running")
            result = await run_research(
                question=question,
                context={"question": question, "task_context": context_str},
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
            )
            if job:
                update_research_job(
                    job.id, status="completed",
                    verdict=json.dumps(result.vote),
                    findings=result.findings,
                    lives_used=result.lives_used,
                    prompt_tokens=result.prompt_tokens,
                    completion_tokens=result.completion_tokens,
                )
            return result.vote | {"findings": result.findings}
        except Exception as exc:
            logger.exception(
                "Research job failed for task '%s': %s", self.task_id, exc
            )
            if job:
                update_research_job(job.id, status="failed", findings=str(exc))
            return {"verdict": "ERROR", "findings": f"Research failed: {exc}"}

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
