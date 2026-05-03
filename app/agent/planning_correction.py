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
from typing import Any

from app.agent.config import (
    CORRECTION_MAX_TURNS,
    check_context_saturation,
)
from app.agent.llm_client import call_llm, is_shutting_down, sanitize_user_content, ShutdownError
from app.agent.tools import async_dispatch_tool, build_tool_schemas, CORRECTION_AGENT_TOOLS

logger = logging.getLogger(__name__)
AGENT_NAME = "Planning Correction Agent"
_MAX_CONSECUTIVE_ERRORS = 3

# Include submit_work in correction tools
_CORRECTION_TOOL_SCHEMAS: list[dict] = build_tool_schemas(CORRECTION_AGENT_TOOLS + ["submit_work"])


class PlanningCorrectionAgent:
    """
    Lightweight agent that patches a failing plan to satisfy gate checks.

    Reads the codebase to verify what is real, then makes minimal JSON
    changes to the plan fields that failed the gate via write_plan_fields.
    Does not write files or use git tools.
    """

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
        self.task_id = task_id
        self.planning_result_id = planning_result_id
        self.current_plan = current_plan
        self.gate_failures = gate_failures
        self.project_root = project_root
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.max_context = max_context
        self.task_title = task_title
        self.task_description = task_description

        self._messages: list[dict] = []
        self._turn: int = 0
        self._consecutive_errors: int = 0
        self._no_tool_turns: int = 0
        self._warnings_fired: set[float] = set()
        self._turn_warnings_fired: set[int] = set()

    async def run(self) -> dict:
        """
        Execute the correction agent loop.

        Returns:
          {"outcome": "corrected"|"stalled"|"max_turns"|"error", "fields_patched": [...]}
        """
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            return {"outcome": "error", "fields_patched": []}

        if self.project_root:
            from app.agent.tools import set_task_git_cwd
            set_task_git_cwd(self.project_root)

        self._messages = self._build_messages()

        while self._turn < CORRECTION_MAX_TURNS:
            self._turn += 1
            logger.debug(
                "[planning_correction] task '%s' — turn %d/%d",
                self.task_id, self._turn, CORRECTION_MAX_TURNS,
            )

            # Turn saturation check
            from app.agent.config import check_turn_saturation
            if check_turn_saturation(
                self._turn, CORRECTION_MAX_TURNS, self._turn_warnings_fired, self._messages
            ):
                # Turn nudge was injected
                pass

            try:
                response = await self._call_llm(self._messages)
            except ShutdownError:
                logger.info("[planning_correction] task '%s' — shutdown.", self.task_id)
                return {"outcome": "error", "fields_patched": []}
            except Exception as exc:
                self._turn -= 1
                self._consecutive_errors += 1
                logger.error(
                    "[planning_correction] task '%s' LLM error: %s", self.task_id, exc
                )
                if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                    return {"outcome": "stalled", "fields_patched": []}
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

            if tool_calls:
                result_messages = await self._handle_tool_calls(tool_calls)
                self._messages.extend(result_messages)

                # Check for terminal signal from submit_work or successful write_plan_fields
                for rm in result_messages:
                    content_str = rm.get("content", "")
                    if isinstance(content_str, str) and "__maestro_terminal__" in content_str:
                        try:
                            data = json.loads(content_str)
                            if data.get("signal") == "REJECTED":
                                logger.warning(
                                    "[planning_correction] task '%s' signalled REJECTED via submit_work.",
                                    self.task_id,
                                )
                                return {"outcome": "stalled", "fields_patched": []}
                        except Exception:
                            pass

                patched_fields = self._extract_patched_fields(tool_calls, result_messages)
                if patched_fields:
                    logger.info(
                        "[planning_correction] task '%s' — patched fields: %s.",
                        self.task_id, patched_fields,
                    )
                    return {"outcome": "corrected", "fields_patched": patched_fields}

                all_errors = all(
                    m.get("content", "").startswith("ERROR")
                    for m in result_messages
                )
                if all_errors:
                    self._consecutive_errors += 1
                    if self._consecutive_errors >= _MAX_CONSECUTIVE_ERRORS:
                        return {"outcome": "stalled", "fields_patched": []}
                else:
                    self._consecutive_errors = 0
                    self._no_tool_turns = 0
                continue

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
                    "or call submit_work with signal='REJECTED' if you cannot determine a fix."
                ),
            })

        logger.warning(
            "[planning_correction] task '%s' — max_turns (%d) exceeded.",
            self.task_id, CORRECTION_MAX_TURNS,
        )
        return {"outcome": "max_turns", "fields_patched": []}

    # ------------------------------------------------------------------
    # Message building
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
            f"TASK: {sanitize_user_content(self.task_title)}\n"
            f"DESCRIPTION: {sanitize_user_content(self.task_description)}\n\n"
            f"PLANNING RESULT ID: {self.planning_result_id}\n\n"
            f"GATE FAILURES:\n{sanitize_user_content(failures_text)}\n\n"
            f"CURRENT interface_contracts:\n{sanitize_user_content(contracts_json)}\n\n"
            f"PROJECT SNAPSHOT:\n{sanitize_user_content(snapshot)}"
            f"{sanitize_user_content(arch_block)}\n\n"
            "WORKFLOW:\n"
            "1. Read relevant source files (read_file, search_files) to verify what actually "
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
            "   For spec_compliance failures: the gate found a forbidden algorithm or approach "
            "named in design_rationale. Rewrite design_rationale so it describes only the TARGET "
            "implementation — what the code WILL do after this task completes. You may include "
            "one brief sentence such as 'Replaces the existing X with Y.' Do NOT repeat the "
            "forbidden keyword anywhere else in design_rationale. Field to patch: design_rationale.\n"
            "3. Call write_plan_fields EXACTLY ONCE with ALL corrected fields in a single call. "
            "After the tool returns success, stop immediately — do not call any more tools and "
            "do not output additional explanation.\n"
            "4. If you cannot determine a valid fix, call submit_work with signal='REJECTED'.\n\n"
            f"After {_MAX_CONSECUTIVE_ERRORS} consecutive tool failures, "
            "call submit_work with signal='REJECTED'."
        )

        return [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "Analyze the gate failures above and apply the minimal corrections to the plan."},
        ]

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

    # ------------------------------------------------------------------
    # Result extraction
    # ------------------------------------------------------------------

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

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            tools=_CORRECTION_TOOL_SCHEMAS,
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
        )
