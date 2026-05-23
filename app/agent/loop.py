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
    SIGNAL_REJECTED,
    SIGNAL_REVERT,
    SIGNAL_NEEDS_HUMAN,
    SIGNAL_CONSULT,
    GIT_SAFETY_BRANCH_PREFIX,
    INDEV_AGENT_TOOLS,
    check_context_saturation,
)
from app.agent.llm_client import call_llm, is_shutting_down, sanitize_user_content, ShutdownError
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
    status: Literal["ACCEPTED", "REJECTED", "REVERT_TO_DESIGN", "NEEDS_HUMAN", "CONSULTING", "MAX_TURNS", "ERROR"]
    turns: int
    final_message: str
    git_branch: str | None = None
    files_changed: list[str] = field(default_factory=list)
    error_detail: str | None = None
    consultation_question: str | None = None  # Added for CONSULTING state


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

        from app.agent.tools import reset_consult_count, _ask_depth_ctx
        reset_consult_count(self.task_id)
        _ask_depth_ctx.set(0)  # root session always starts at depth 0

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
            
            # Reset terminal signal for this turn
            self._terminal_signal = None

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

            if tool_calls:
                tool_result_messages = await self._handle_tool_calls(tool_calls)
                self._messages.extend(tool_result_messages)
                
                # Check for terminal signal from submit_work tool call
                if hasattr(self, "_terminal_signal") and self._terminal_signal:
                    return self._handle_terminal(self._terminal_signal)
                
                # Check for timeouts in tool results
                has_timeout = any(
                    "ERROR: Command timed out" in msg.get("content", "")
                    for msg in tool_result_messages
                )
                if has_timeout:
                    logger.info("MaestroLoop detected shell timeout for task '%s'.", self.task_id)
                    self._messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] The last shell command timed out. "
                            "Call spawn_research_agent to investigate the source code and tests "
                            "for infinite loops, deadlocks, or high-complexity algorithms called "
                            "with large inputs. Then fix the implementation based on the findings."
                        ),
                    })
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

            # ── No tool calls — nudge the agent ───────────────────────
            if not tool_calls:
                self._messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] You did not call any tool and did not emit a terminal signal. "
                        "The ONLY way to complete your task is by calling the 'submit_work' tool. "
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
        Assemble the message list.
        If a saved session state exists (from a CONSULTING pause), load it.
        Otherwise, build the initial system prompt and task brief.
        """
        from app.database import get_task_session_state, delete_task_session_state, get_task as _get_task
        
        # Check for saved state (resume logic)
        saved = get_task_session_state(self.task_id)
        if saved:
            _session_id, turn_count, messages = saved
            self._turn = turn_count
            
            # Look up the task to see if a hint was provided
            task_rec = _get_task(self.task_id)
            if task_rec and task_rec.consultation_payload:
                try:
                    payload = json.loads(task_rec.consultation_payload)
                    hint = payload.get("hint")
                    if hint:
                        messages.append({
                            "role": "user",
                            "content": f"[MAESTRO STEERING HINT] {hint}\n\nPlease proceed with implementation based on this guidance."
                        })
                except:
                    pass

            # Clean up the state record now that we've loaded it
            delete_task_session_state(self.task_id)
            
            logger.info("Task '%s': Resuming from saved state (turn %d).", self.task_id, self._turn)
            return messages

        # --- Standard initial build ---
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
                snapshot_block = f"\n\n{sanitize_user_content(build_snapshot_with_summaries(_project_path, max_tokens=_snap_max))}"
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
                    arch_block = f"\n\n{sanitize_user_content(_arch)}"

                # Fetch PIPs for this task
                pips = _get_pips(self.task_id)
                if pips:
                    pip_block = "\n\n### HISTORICAL PERFORMANCE IMPROVEMENT PLANS (PIPs)\n"
                    pip_block += "This task has previously failed review/optimization. You MUST satisfy ALL requirements below:\n"
                    for i, pip in enumerate(pips):
                        reqs = json.loads(pip.requirements)
                        pip_block += f"\nPIP {i+1} (from {pip.origin_stage}, status: {pip.status}):\n"
                        for req in reqs:
                            pip_block += f"- {sanitize_user_content(req)}\n"

                # Fetch global architectural decisions
                from app.database import get_project_decisions
                decisions = get_project_decisions(_task_rec.project)
                if decisions:
                    pip_block += "\n\n### BINDING ARCHITECTURAL DECISIONS\n"
                    pip_block += "These are global project-level decisions you MUST follow:\n"
                    for d in decisions:
                        pip_block += f"\n- [{d.topic}]: {sanitize_user_content(d.decision)}\n"
                        if d.rationale:
                            pip_block += f"  Rationale: {sanitize_user_content(d.rationale)}\n"

                # Inject active goals so the agent knows the direction to move in
                try:
                    from app.database import get_active_goals_for_project
                    goals = get_active_goals_for_project(_task_rec.project)
                    if goals:
                        pip_block += "\n\n### ACTIVE PROJECT GOALS\n"
                        pip_block += "These goals define the direction this project is moving. Let them guide your implementation choices:\n"
                        for g in goals:
                            pip_block += f"\n**{sanitize_user_content(g.title)}** [{g.status}] — {int(g.progress * 100)}% complete\n"
                            pip_block += f"{sanitize_user_content(g.statement)}\n"
                            if g.criteria:
                                crit_texts = [c.get("text", "") for c in g.criteria if c.get("text")]
                                if crit_texts:
                                    pip_block += "Criteria: " + "; ".join(sanitize_user_content(t) for t in crit_texts) + "\n"
                except Exception:
                    pass  # goal injection is best-effort, never block the agent

                # Gap 4 — inject autopilot objective context for tasks spawned by an objective
                try:
                    if _task_rec and _task_rec.autopilot_objective_id:
                        from app.database import get_objective, list_objectives
                        obj = get_objective(_task_rec.autopilot_objective_id)
                        if obj and obj.status != "complete":
                            created_str = obj.created_at.strftime("%Y-%m-%d") if obj.created_at else "unknown"
                            pip_block += "\n\n### AUTOPILOT OBJECTIVE\n"
                            pip_block += (
                                f"[P{obj.priority}] {sanitize_user_content(obj.description)}\n"
                                f"Status: {obj.status} | Created by: {obj.created_by} | "
                                f"Started: {created_str}\n"
                            )
                            if obj.last_assessment:
                                pip_block += f"Latest assessment: {sanitize_user_content(obj.last_assessment)}\n"
                            pip_block += (
                                f"Evidence log: call get_objective_evidence(objective_id={obj.id})"
                                " to read the full history.\n"
                            )
                            others = [
                                o for o in list_objectives(obj.project_id, status="active")
                                if o.id != obj.id
                            ]
                            if others:
                                pip_block += "\n**Other active objectives (summaries):**\n"
                                for o in others:
                                    pip_block += f"- [P{o.priority}] id={o.id}: {sanitize_user_content(o.description)}\n"
                                pip_block += "Use get_objective_detail(id) to read any of the above in full.\n"
                except Exception:
                    pass  # objective injection is best-effort, never block the agent

                # Inject demotion history so re-entering dev sessions know why they were sent back
                if _task_rec and _task_rec.demotion_history:
                    recent = _task_rec.demotion_history[-3:]
                    lines = ["\n\n### DEMOTION HISTORY (most recent first — read before implementing)"]
                    for entry in reversed(recent):
                        ts = entry.get("timestamp", "")[:10]
                        lines.append(
                            f"- [{ts}] {entry['from']} → {entry['to']}: {sanitize_user_content(entry.get('reason', ''))}"
                        )
                    pip_block += "\n".join(lines) + "\n"
        except Exception:
            pass

        # Auto-inject relevant past episodes (Gap 7 — episodic memory)
        episode_block = ""
        try:
            import app.agent.config as _cfg
            if (
                _cfg.EPISODIC_MEMORY_ENABLED
                and _cfg.EPISODIC_MEMORY_AUTO_INJECT_K > 0
                and _task_rec
                and _task_rec.project_id is not None
            ):
                from app.agent.episodic_memory import query_episodes
                episodes = query_episodes(
                    project_id=_task_rec.project_id,
                    question=(_task_rec.description or _task_rec.title or self.task_id),
                    k=_cfg.EPISODIC_MEMORY_AUTO_INJECT_K,
                    settings=_cfg,
                )
                if episodes:
                    lines = ["\n\n### Relevant past experience"]
                    for ep in episodes:
                        ts = ep["created_at"].strftime("%Y-%m-%d") if ep.get("created_at") else "?"
                        lines.append(
                            f"- [{ep['episode_type']} | {ts}] {ep['content']}"
                        )
                    episode_block = "\n".join(lines) + "\n"
        except Exception:
            pass  # episodic injection is always best-effort

        return [
            {"role": "system", "content": MAESTRO_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": (
                    f"Your assigned task ID is: **{self.task_id}**"
                    f"{snapshot_block}{arch_block}{pip_block}{episode_block}\n\n"
                    f"Begin by calling get_task('{self.task_id}') to load the full "
                    f"task definition, including the approved PLANNING result "
                    f"(file_manifest, implementation_steps, interface_contracts). "
                    f"Then follow the workflow in your system prompt.\n\n"
                    f"Your maestro/task-{self.task_id} branch is already created and checked out. "
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

            # Resolve submit_work(previous=True) before dispatching.
            if name == "submit_work" and arguments.get("previous"):
                resolved = self._resolve_previous_submit(arguments)
                if resolved is None:
                    result_content = (
                        "[ERROR] submit_work(previous=True) failed: no prior submit_work call "
                        "exists in this session to reference. You must call submit_work with "
                        "explicit signal and summary arguments at least once before previous=True "
                        "can be used."
                    )
                    result_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_id,
                        "name": name,
                        "content": result_content,
                    })
                    continue
                arguments = resolved

            # Dispatch (async - handles spawn_research_agent correctly)
            result_content = await async_dispatch_tool(
                name, arguments,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
            )

            # Check for terminal signal from submit_work
            if isinstance(result_content, str) and "__maestro_terminal__" in result_content:
                try:
                    terminal_data = json.loads(result_content)
                    if terminal_data.get("__maestro_terminal__"):
                        gate_error = self._check_gate_for_submit(terminal_data)
                        if gate_error:
                            # Gate blocked: inject rejection into context and continue loop.
                            result_content = gate_error
                        else:
                            self._terminal_signal = terminal_data
                except Exception:
                    pass

            result_messages.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": result_content,
            })

        return result_messages

    # ------------------------------------------------------------------
    # Gate pre-check hook (overridden by CustomLLMAgent)
    # ------------------------------------------------------------------

    def _check_gate_for_submit(self, terminal_data: dict) -> str | None:
        """
        Called before a terminal submit_work signal is accepted.
        Returns None if the gate passes, or a rejection message string that
        will be injected into the agent context so the loop can continue.
        Base implementation always passes; CustomLLMAgent overrides this.
        """
        return None

    def _resolve_previous_submit(self, current_args: dict) -> dict | None:
        """
        Walk message history backwards to resolve submit_work(previous=True).
        Chains through any intermediate previous=True calls until it finds
        a submit_work call with concrete (non-previous) arguments.
        Returns the resolved arguments dict, or None if not found.
        """
        visited: set[int] = set()
        for msg in reversed(self._messages):
            if msg.get("role") != "assistant":
                continue
            for tc in (msg.get("tool_calls") or []):
                tc_id = id(tc)
                if tc_id in visited:
                    continue
                visited.add(tc_id)
                fn = tc.get("function", {})
                if fn.get("name") != "submit_work":
                    continue
                raw = fn.get("arguments", "{}")
                try:
                    args = json.loads(raw) if isinstance(raw, str) else raw
                except Exception:
                    continue
                if args.get("previous"):
                    continue  # skip, keep walking back
                # Found concrete args — strip previous flag if present and return
                args.pop("previous", None)
                logger.info(
                    "[loop] submit_work(previous=True) resolved to args from earlier call "
                    "(signal=%s, summary=%s...)",
                    args.get("signal"), str(args.get("summary", ""))[:60],
                )
                return args
        logger.warning("[loop] submit_work(previous=True): no prior concrete submit_work found in history")
        return None

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
        elif sig == SIGNAL_NEEDS_HUMAN:
            result = LoopResult(
                task_id=self.task_id,
                status="NEEDS_HUMAN",
                turns=self._turn,
                final_message=signal_dict.get("summary", "Agent escalated for human review."),
                git_branch=self._git_branch,
            )
        elif sig == SIGNAL_REJECTED:
            # Dev agent self-reporting rejection — treat like a design revert
            result = LoopResult(
                task_id=self.task_id,
                status="REJECTED",
                turns=self._turn,
                final_message=signal_dict.get("summary", "Implementation rejected."),
                git_branch=self._git_branch,
                error_detail=signal_dict.get("advice"),
            )
        elif sig == SIGNAL_CONSULT:
            # Save message history so we can resume later
            from app.database import save_task_session_state, get_agent_session_id_for_task
            
            # We need the current session ID to link the state
            # This assumes the loop has already been registered (it has, in run())
            session_id = None
            try:
                from app.agent.llm_client import get_active_session_id
                session_id = get_active_session_id()
            except:
                pass

            question = signal_dict.get("payload", {}).get("question", "No question provided.")
            
            # Save the state!
            save_task_session_state(
                task_id=self.task_id,
                session_id=session_id or 0,
                turn_count=self._turn,
                messages=self._messages
            )

            result = LoopResult(
                task_id=self.task_id,
                status="CONSULTING",
                turns=self._turn,
                final_message=f"Agent paused for consultation: {question}",
                git_branch=self._git_branch,
                consultation_question=question
            )
        else:  # REVERT_TO_DESIGN and any unknown signal
            result = LoopResult(
                task_id=self.task_id,
                status="REVERT_TO_DESIGN",
                turns=self._turn,
                final_message=signal_dict.get("reason", signal_dict.get("summary", "Reverting to design.")),
                git_branch=self._git_branch,
                error_detail=signal_dict.get("advice"),
            )

        _LOOP_STATUS[self.task_id] = self._status_dict(result)

        # Enqueue async session-end summary job (Gap 7 — episodic memory)
        if sig != SIGNAL_CONSULT:  # don't summarise paused sessions
            try:
                import app.agent.config as _cfg
                if _cfg.EPISODIC_MEMORY_ENABLED:
                    from app.database import create_episodic_summary_job
                    create_episodic_summary_job(
                        task_id=self.task_id,
                        final_status=sig or "UNKNOWN",
                        llm_id=self.llm_id,
                        budget_id=self.budget_id,
                    )
            except Exception:
                pass  # job enqueue must never break session teardown

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
