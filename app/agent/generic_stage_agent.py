"""
app/agent/generic_stage_agent.py
---------------------------------
GenericStageAgent — a universal LLM agent driven entirely by PipelineStage.config.

Unlike CustomLLMAgent (which reads a custom_agent_definitions DB row),
GenericStageAgent reads system_prompt, tool_allowlist, gate_type, max_turns,
required_input_keys, and output_keys directly from stage_config.config.

Any custom pipeline stage whose agent_type has no registered executor and no
registered stage handler is dispatched here.
"""

from __future__ import annotations

import logging

from app.agent.agent_loop import AgentLoop
from app.agent.pipeline_router import StageConfig, advance_stage
from app.agent.tools import build_tool_schemas

logger = logging.getLogger(__name__)


class GenericStageAgent(AgentLoop):
    """LLM agent configured entirely from PipelineStage.config."""

    _agent_name: str = "generic_stage_agent"

    def __init__(
        self,
        *,
        task_id: str,
        stage_config: StageConfig,
        llm_id: int | None,
        budget_id: int | None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        max_context: int | None = None,
    ) -> None:
        cfg = stage_config.config or {}
        max_turns = int(cfg.get("max_turns", 20))
        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self._system_prompt: str = cfg.get("system_prompt", "")
        self._gate_type: str = cfg.get("gate_type", "llm_judge")
        self._stage_config: StageConfig = stage_config
        self._agent_name = f"generic:{stage_config.stage_key}"

        allowed = list(cfg.get("tool_allowlist") or cfg.get("allowed_tools") or [])
        if "submit_work" not in allowed:
            allowed.append("submit_work")
        self._tool_schemas_list = build_tool_schemas(allowed)

    # ------------------------------------------------------------------
    # AgentLoop abstract interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        from app.database import get_task
        task = get_task(self.task_id)
        cfg = self._stage_config.config or {}

        user_lines = [
            f"Task ID: {self.task_id}",
            f"Title: {task.title if task else '(unknown)'}",
            f"Description:\n{task.description or '' if task else ''}",
        ]

        required_keys = cfg.get("required_input_keys") or []
        if isinstance(required_keys, str):
            required_keys = [k.strip() for k in required_keys.split(",") if k.strip()]
        content_blob = (task.content or {}) if task else {}
        if required_keys:
            user_lines.append("\n== Prior Stage Outputs ==")
            for key in required_keys:
                if key in content_blob:
                    user_lines.append(f"{key}: {content_blob[key]}")

        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": "\n".join(user_lines)},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas_list

    async def _on_terminal(self) -> dict:
        signal = self._terminal_signal.get("signal", "")
        if self._gate_type in ("none", "single_pass"):
            condition = "pass"
        elif signal == "ACCEPTED":
            condition = "pass"
        elif signal == "REJECTED":
            condition = "fail"
        else:
            condition = "pass"

        # Write output_keys from submit_work payload → task.content
        output_keys = (self._stage_config.config or {}).get("output_keys") or []
        if isinstance(output_keys, str):
            output_keys = [k.strip() for k in output_keys.split(",") if k.strip()]
        if output_keys and self._terminal_signal:
            from app.database import get_task, update_task
            task = get_task(self.task_id)
            blob = dict(task.content or {}) if task else {}
            payload = self._terminal_signal.get("payload") or {}
            for key in output_keys:
                if key in payload:
                    blob[key] = payload[key]
            update_task(self.task_id, content=blob)

        advance_stage(self.task_id, condition)
        return {"signal": signal, "condition": condition}

    async def _on_max_turns(self) -> dict:
        logger.warning(
            "[generic_stage] task '%s' stage '%s': max turns — advancing fail.",
            self.task_id, self._stage_config.stage_key,
        )
        advance_stage(self.task_id, "fail")
        return {"signal": "MAX_TURNS", "condition": "fail"}

    async def _on_error(self, reason: str) -> dict:
        from app.agent.llm_client import is_shutting_down, ShutdownError
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")
        logger.error(
            "[generic_stage] task '%s' stage '%s': error — %s (stage stays put)",
            self.task_id, self._stage_config.stage_key, reason,
        )
        return {"signal": "ERROR", "reason": reason}
