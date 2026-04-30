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
       b. It calls submit_work(signal="RESOLUTION_STALLED") after repeated failures.
       c. max_turns is exceeded.
  4. signal_completion(f"pip_resolution_{pip_id}") always fires on exit (in scheduler).
  5. The scheduler detects this signal, marks the job done, and re-dispatches
     the parent stage so the pre-flight can run again.
"""

from __future__ import annotations

import logging

from app.agent.agent_loop import AgentLoop
from app.agent.config import (
    PIP_RESOLUTION_MAX_TURNS,
    GIT_SAFETY_BRANCH_PREFIX,
    INDEV_AGENT_TOOLS,
)
from app.agent.llm_client import is_shutting_down
from app.agent.tools import build_tool_schemas

logger = logging.getLogger(__name__)
AGENT_NAME = "PIP Resolution Agent"
_MAX_CONSECUTIVE_ERRORS = 3

_RESOLUTION_TOOL_SCHEMAS: list[dict] = build_tool_schemas(INDEV_AGENT_TOOLS)


class PIPResolutionAgent(AgentLoop):
    """
    Targeted implementation agent that closes PIP quality gaps.

    Operates on the existing maestro/task-{id} branch, making minimal,
    focused changes to satisfy the specific requirements from a PIP that
    failed the pre-flight gate.
    """

    _agent_name = AGENT_NAME

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
        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=PIP_RESOLUTION_MAX_TURNS,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self.pip_id = pip_id
        self.requirements = requirements
        self.research_findings = research_findings
        self.last_verification_findings = last_verification_findings
        self.project_root = project_root
        self.task_title = task_title
        self.origin_stage = origin_stage

    # ------------------------------------------------------------------
    # AgentLoop interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
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
            f"After {_MAX_CONSECUTIVE_ERRORS} consecutive tool failures, call:\n"
            "  submit_work(signal='RESOLUTION_STALLED', summary='resolution exhausted',\n"
            "              payload={'reason': 'consecutive tool failures',\n"
            "                       'advice': 'try different approach'})\n"
            "Do NOT output raw JSON with a signal key — use the submit_work tool call."
        )

        return [{"role": "system", "content": system_prompt}]

    def _get_tool_schemas(self) -> list[dict]:
        return _RESOLUTION_TOOL_SCHEMAS

    async def _on_terminal(self) -> dict:
        # RESOLUTION_STALLED signal from submit_work → stalled
        return {"status": "stalled", "turns": self._turn}

    async def _on_max_turns(self) -> dict:
        logger.warning(
            "[pip_resolution] pip %d — max_turns (%d) exceeded.",
            self.pip_id, self.max_turns,
        )
        return {"status": "max_turns", "turns": self._turn}

    async def _on_error(self, reason: str) -> dict:
        logger.info("[pip_resolution] pip %d — stalled (error): %s", self.pip_id, reason)
        return {"status": "stalled", "turns": self._turn}

    async def _on_no_tool_call(self):
        """Nudge once, then exit 'done' (requirements satisfied — agent stopped naturally)."""
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
                "that satisfy the PIP requirements, or call "
                "submit_work(signal='RESOLUTION_STALLED', summary='...') if you cannot proceed."
            ),
        })
        return None

    # ------------------------------------------------------------------
    # run() override for setup logic
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)

        if is_shutting_down():
            return {"status": "error", "turns": 0}

        if self.project_root:
            from app.agent.tools import set_task_git_cwd
            set_task_git_cwd(self.project_root)

        logger.debug(
            "[pip_resolution] pip %d task '%s' — starting (%d max turns)",
            self.pip_id, self.task_id, self.max_turns,
        )

        self._messages = self._build_messages()
        return await self._run_loop()
