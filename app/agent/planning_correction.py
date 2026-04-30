"""
app/agent/planning_correction.py
---------------------------------
PlanningCorrectionAgent — lightweight inter-stage repair agent that makes
surgical JSON patches to a failing plan before the scheduler retries the
full planning gate.

Lifecycle (driven inline by _run_planning_task in scheduler.py):
  1. Gate rejects the plan; scheduler calls _run_planning_correction().
  2. PlanningCorrectionAgent.run() drives the LLM -> tool cycle.
  3. The agent reads relevant codebase files, then calls write_plan_fields
     with the corrected plan JSON fields.
  4. Scheduler re-runs the gate on the patched plan.
  5. Passes -> advance to INDEV; still fails -> fall through to cooldown.
"""

from __future__ import annotations

import json
import logging

from app.agent.agent_loop import AgentLoop
from app.agent.config import (
    CORRECTION_MAX_TURNS,
    SIGNAL_CORRECTION_STALLED,
)
from app.agent.llm_client import is_shutting_down
from app.agent.tools import build_tool_schemas, CORRECTION_AGENT_TOOLS

logger = logging.getLogger(__name__)
AGENT_NAME = "Planning Correction Agent"
_MAX_CONSECUTIVE_ERRORS = 3

_CORRECTION_TOOL_SCHEMAS: list[dict] = build_tool_schemas(CORRECTION_AGENT_TOOLS)


class PlanningCorrectionAgent(AgentLoop):
    """
    Lightweight agent that patches a failing plan to satisfy gate checks.

    Reads the codebase to verify what is real, then makes minimal JSON
    changes to the plan fields that failed the gate via write_plan_fields.
    Does not write files or use git tools.
    """

    _agent_name = AGENT_NAME

    def __init__(
        self,
        task_id: str,
        planning_result_id: int,
        current_plan: dict,
        gate_failures: list[dict],
        project_root: str | None,
        llm_id: int,
        budget_id: int,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        max_context: int | None = None,
        task_title: str = "",
        task_description: str = "",
    ) -> None:
        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=CORRECTION_MAX_TURNS,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self.planning_result_id = planning_result_id
        self.current_plan = current_plan
        self.gate_failures = gate_failures
        self.project_root = project_root
        self.task_title = task_title
        self.task_description = task_description

    # ------------------------------------------------------------------
    # AgentLoop interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        from app.agent.project_snapshot import build_project_snapshot, build_architecture_context
        from app.database import get_task as _get_task

        snapshot = ""
        arch_block = ""
        try:
            if self.project_root:
                snapshot = build_project_snapshot(self.project_root)
            task_rec = _get_task(self.task_id)
            if task_rec and task_rec.project:
                arch = build_architecture_context(task_rec.project, agent_type="loop")
                if arch:
                    arch_block = f"\n\nARCHITECTURE CONTEXT:\n{arch}"
        except Exception as exc:
            logger.debug("[planning_correction] Context build warning: %s", exc)

        failures_text = self._format_failures()
        contracts_json = self._format_interface_contracts()

        system_prompt = (
            "You are the Maestro Planning Correction Agent.\n\n"
            "The planning gate rejected the plan for this task. "
            "Your job: make the MINIMAL targeted changes to the plan JSON fields "
            "that satisfy the failing checks. Do NOT redesign the plan. "
            "Do NOT touch fields that are not failing.\n\n"
            f"TASK: {self.task_title}\n"
            f"DESCRIPTION: {self.task_description}\n\n"
            f"PLANNING RESULT ID: {self.planning_result_id}\n\n"
            f"GATE FAILURES:\n{failures_text}\n\n"
            f"CURRENT interface_contracts:\n{contracts_json}\n\n"
            f"PROJECT SNAPSHOT:\n{snapshot}"
            f"{arch_block}\n\n"
            "WORKFLOW:\n"
            "1. Read relevant source files (read_file, find_in_files) to verify what actually "
            "exists in the codebase.\n"
            "2. Determine the minimal JSON change that fixes each failing check.\n"
            "   For interface_completeness failures: entries in `consumes` that have no matching "
            "`provides` are typically imports that don't belong in interface_contracts. Remove "
            "them from consumes. This includes:\n"
            "     - Python built-ins: int, str, float, bool, bytes, dict, list, datetime, etc.\n"
            "     - Kotlin/JVM built-ins: Long, String, Boolean, Int, ByteArray, Any, Unit, "
            "Flow, StateFlow, MutableList, etc.\n"
            "     - Android framework: Context, Intent, ViewModel, Fragment, CoroutineScope, etc.\n"
            "     - Files/classes that already exist in the codebase (imports from other modules) "
            "— read the file first to confirm it exists, then remove it from consumes.\n"
            "   They are not cross-component contracts within this plan.\n"
            "3. Call write_plan_fields ONCE with ALL corrected fields.\n"
            "4. If you cannot determine a valid fix, call:\n"
            "     submit_work(signal='CORRECTION_STALLED', summary='cannot fix plan',\n"
            "                 payload={'reason': '<root cause>', 'advice': '<what to try instead>'})\n"
            "Do NOT output raw JSON with a signal key — use the submit_work tool call.\n\n"
            f"After {_MAX_CONSECUTIVE_ERRORS} consecutive tool failures, "
            "call submit_work(signal='CORRECTION_STALLED', ...) to signal stall."
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyze the gate failures above and apply the minimal corrections to the plan."},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return _CORRECTION_TOOL_SCHEMAS

    async def _on_terminal(self) -> dict:
        # submit_work CORRECTION_STALLED → stalled
        sig = self._terminal_signal.get("signal", SIGNAL_CORRECTION_STALLED)
        logger.info(
            "[planning_correction] task '%s': submit_work signal=%s",
            self.task_id, sig,
        )
        return {"outcome": "stalled", "fields_patched": [], "terminal_signal": sig}

    async def _on_max_turns(self) -> dict:
        logger.warning(
            "[planning_correction] task '%s' — max_turns (%d) exceeded.",
            self.task_id, self.max_turns,
        )
        return {"outcome": "max_turns", "fields_patched": []}

    async def _on_error(self, reason: str) -> dict:
        logger.info("[planning_correction] task '%s' — error: %s", self.task_id, reason)
        if "shutting down" in reason.lower():
            return {"outcome": "error", "fields_patched": []}
        return {"outcome": "stalled", "fields_patched": []}

    async def _on_no_tool_call(self):
        self._no_tool_turns += 1
        if self._no_tool_turns >= 2:
            logger.info(
                "[planning_correction] task '%s' — stopped calling tools after %d turns.",
                self.task_id, self._turn,
            )
            return {"outcome": "stalled", "fields_patched": []}
        self._messages.append({
            "role": "user",
            "content": (
                "[SYSTEM] No tool was called. Use write_plan_fields to apply your corrections, "
                "or call submit_work(signal='CORRECTION_STALLED', summary='...') "
                "if you cannot determine a fix."
            ),
        })
        return None

    async def _post_dispatch_hook(
        self, tool_calls: list, result_messages: list[dict]
    ) -> dict | None:
        """Exit 'corrected' as soon as write_plan_fields succeeds."""
        patched_fields = self._extract_patched_fields(tool_calls, result_messages)
        if patched_fields:
            logger.info(
                "[planning_correction] task '%s' — patched fields: %s.",
                self.task_id, patched_fields,
            )
            return {"outcome": "corrected", "fields_patched": patched_fields}
        return None

    # ------------------------------------------------------------------
    # run() override for setup logic
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)

        if is_shutting_down():
            return {"outcome": "error", "fields_patched": []}

        if self.project_root:
            from app.agent.tools import set_task_git_cwd
            set_task_git_cwd(self.project_root)

        self._messages = self._build_messages()
        return await self._run_loop()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _format_failures(self) -> str:
        lines = []
        for f in self.gate_failures:
            name = f.get("name", "?")
            detail = f.get("detail", "")
            hard = "(hard fail)" if f.get("hard_fail") else "(soft fail)"
            lines.append(f"  [{name}] {hard}: {detail}")
        return "\n".join(lines) if lines else "  (no failures recorded)"

    def _format_interface_contracts(self) -> str:
        contracts = self.current_plan.get("interface_contracts", [])
        if not contracts:
            return "[]"
        try:
            return json.dumps(contracts, indent=2)
        except Exception:
            return str(contracts)

    def _extract_patched_fields(
        self, tool_calls: list, result_messages: list[dict]
    ) -> list[str]:
        """Return list of field names patched if write_plan_fields succeeded."""
        for tc, rm in zip(tool_calls, result_messages):
            name = tc.get("function", {}).get("name", "")
            if name != "write_plan_fields":
                continue
            result_text = rm.get("content", "")
            if not result_text.startswith("Updated fields:"):
                continue
            raw_args = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                fields_json = args.get("fields_json", "{}")
                fields = json.loads(fields_json) if isinstance(fields_json, str) else fields_json
                return list(fields.keys())
            except Exception:
                return ["(unknown)"]
        return []
