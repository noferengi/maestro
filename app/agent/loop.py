"""
app/agent/loop.py
-----------------
The Wiggum Loop - core orchestration engine for a single Maestro task.

MaestroLoop drives the LLM -> tool-call -> result -> LLM cycle until one of:
  * The agent emits an ACCEPTED signal via submit_work.
  * The agent emits a REVERT_TO_DESIGN signal via submit_work.
  * max_turns is exceeded.
  * MAX_CONSECUTIVE_ERRORS consecutive tool errors occur.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Literal

from app.agent.agent_loop import AgentLoop
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
    SIGNAL_RESOLUTION_STALLED,
    SIGNAL_CORRECTION_STALLED,
    SIGNAL_VERDICT_REJECTED,
    SIGNAL_VERDICT_NEEDS_WORK,
    GIT_SAFETY_BRANCH_PREFIX,
    INDEV_AGENT_TOOLS,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
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

class MaestroLoop(AgentLoop):
    """
    Drives the LLM agent loop for a single Kanban task.

    Usage::

        loop = MaestroLoop(task_id="task-123")
        result = await loop.run()
    """

    _agent_name = AGENT_NAME

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
        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self.project_path = project_path
        self._stop_requested: bool = False
        self._git_branch: str | None = None
        self._files_changed: list[str] = []

    # ------------------------------------------------------------------
    # AgentLoop interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        """Assemble [system_prompt, user_task_brief]."""
        _project_path = self.project_path or None
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

        from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT
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

    def _get_tool_schemas(self) -> list[dict]:
        return _INDEV_TOOL_SCHEMAS

    async def _on_terminal(self) -> LoopResult:
        """Convert a terminal signal dict into a LoopResult."""
        signal_dict = self._terminal_signal
        sig = signal_dict.get("signal")
        payload = signal_dict.get("payload", {})

        if sig == SIGNAL_ACCEPTED:
            result = LoopResult(
                task_id=self.task_id,
                status="ACCEPTED",
                turns=self._turn,
                final_message=signal_dict.get("summary", "Task accepted."),
                git_branch=payload.get("git_branch") or self._git_branch,
                files_changed=payload.get("files_changed") or self._files_changed,
            )
        elif sig in (SIGNAL_RESOLUTION_STALLED, SIGNAL_CORRECTION_STALLED,
                     SIGNAL_VERDICT_REJECTED, SIGNAL_VERDICT_NEEDS_WORK):
            result = LoopResult(
                task_id=self.task_id,
                status="REVERT_TO_DESIGN",
                turns=self._turn,
                final_message=payload.get("reason", signal_dict.get("summary", "Reverting.")),
                git_branch=self._git_branch,
                error_detail=payload.get("advice"),
            )
        else:  # REVERT_TO_DESIGN
            result = LoopResult(
                task_id=self.task_id,
                status="REVERT_TO_DESIGN",
                turns=self._turn,
                final_message=payload.get("reason", signal_dict.get("summary", "Reverting to design.")),
                git_branch=self._git_branch,
                error_detail=payload.get("advice"),
            )

        _LOOP_STATUS[self.task_id] = self._status_dict(result)
        return result

    async def _on_max_turns(self) -> LoopResult:
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

    async def _on_error(self, reason: str) -> LoopResult:
        result = LoopResult(
            task_id=self.task_id,
            status="ERROR",
            turns=self._turn,
            final_message=reason,
            git_branch=self._git_branch,
            error_detail=reason,
        )
        _LOOP_STATUS[self.task_id] = self._status_dict(result)
        return result

    async def _on_no_tool_call(self):
        """MaestroLoop always nudges — never exits on idle turns."""
        self._messages.append({
            "role": "user",
            "content": (
                "[SYSTEM] You did not call any tool. "
                "You must either call a tool to make progress or call "
                "submit_work(signal='ACCEPTED', summary='...') to complete. "
                "Do not output free-form prose or raw JSON as a terminal action — "
                "use the submit_work tool call."
            ),
        })
        return None  # always continue

    # ------------------------------------------------------------------
    # Tool dispatch override — git tracking + timeout research
    # ------------------------------------------------------------------

    async def _dispatch_tools(self, tool_calls: list) -> list[dict]:
        result_messages = await super()._dispatch_tools(tool_calls)

        # Track git branch creation and file writes
        for tc in tool_calls:
            function_block = tc.get("function", {})
            name = function_block.get("name", "")
            raw_args = function_block.get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except Exception:
                args = {}
            if name == "write_git_branch":
                branch = args.get("branch_name", "")
                if branch:
                    self._git_branch = branch
                    _LOOP_STATUS[self.task_id]["git_branch"] = branch
            if name in ("write_file", "append_file"):
                path = args.get("path", "")
                if path and path not in self._files_changed:
                    self._files_changed.append(path)

        # Handle shell timeouts by triggering inline research
        has_timeout = any(
            "ERROR: Command timed out" in m.get("content", "")
            for m in result_messages if m.get("role") == "tool"
        )
        if has_timeout:
            logger.info(
                "MaestroLoop detected shell timeout for task '%s' - triggering research.",
                self.task_id,
            )
            research_signal = {
                "signal": SIGNAL_NEEDS_RESEARCH,
                "question": (
                    "The last shell command timed out. Investigate the source code and tests "
                    "to see if there is an infinite loop, a deadlock, or a high-complexity "
                    "algorithm (like naive Fibonacci) being called with large inputs in a test."
                ),
                "context": "A shell command timed out during implementation.",
            }
            research_result = await self._handle_needs_research(research_signal)
            # Replace the timeout error content so error counting doesn't trigger
            for i, m in enumerate(result_messages):
                if m.get("role") == "tool" and "ERROR: Command timed out" in m.get("content", ""):
                    result_messages[i] = dict(m, content="[SYSTEM] Shell command timed out.")
            # Append research findings as user message (will be extended to self._messages by base)
            result_messages.append({
                "role": "user",
                "content": (
                    "[SYSTEM] The shell command timed out, and a Research Agent was triggered to investigate.\n"
                    f"Verdict: {research_result.get('verdict', 'unknown')}\n"
                    f"Findings:\n{research_result.get('findings', 'No findings.')}\n\n"
                    "Based on these findings, fix the implementation or the tests to avoid the timeout."
                ),
            })
            self._consecutive_errors = 0

        return result_messages

    # ------------------------------------------------------------------
    # run() override — registry management + CancelledError handling
    # ------------------------------------------------------------------

    async def run(self) -> LoopResult:
        """
        Execute the Wiggum Loop until a terminal condition is reached.
        Registers itself in _ACTIVE_LOOPS and updates _LOOP_STATUS.
        """
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)

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

        # Pre-warm file summaries for the project (fire-and-forget)
        _project_root = self.project_path or PROJECT_ROOT
        if self.llm_id is not None:
            try:
                from app.agent.project_snapshot import prewarm_project_summaries
                import asyncio as _asyncio
                await _asyncio.get_event_loop().run_in_executor(
                    None,
                    lambda: prewarm_project_summaries(
                        _project_root,
                        llm_id=self.llm_id,
                        budget_id=self.budget_id,
                        task_id=self.task_id,
                    ),
                )
            except Exception as exc:
                logger.warning("prewarm failed (non-fatal): %s", exc)

        try:
            self._messages = self._build_messages()
            return await self._run_loop()
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
    # NEEDS_RESEARCH handler (inline research triggered by timeout)
    # ------------------------------------------------------------------

    async def _handle_needs_research(self, signal_dict: dict) -> dict:
        """Run a research agent inline and return findings."""
        from app.agent.research import run_research
        from app.database import create_research_job, update_research_job

        question = signal_dict.get("question", "What do I need to investigate?")
        context_str = signal_dict.get("context", "")

        job = create_research_job(
            task_id=self.task_id,
            question=question,
            context=json.dumps({"question": question, "context": context_str}),
            priority=0.0,
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
    # Terminal handler helpers
    # ------------------------------------------------------------------

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

    def _status_dict(self, result: LoopResult) -> dict:
        return {
            "task_id": result.task_id,
            "status": result.status,
            "turns": result.turns,
            "git_branch": result.git_branch,
            "final_message": result.final_message,
        }
