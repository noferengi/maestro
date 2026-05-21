"""
app/agent/reflection_agent.py
------------------------------
ReflectionAgent — skeptical review of a prior stage's output.

Produces a structured JSON confidence report stored at
  reflection:{task_id}:{stage_key}
in the project document store.  Maestro reads the report during its
assessment tick and decides the next action (advance, retry, demote, PIP).

The stage always advances with condition "pass" — the gate is Maestro's
judgment, not a hardcoded pass/fail threshold here.
"""

from __future__ import annotations

import json
import logging

from app.agent.agent_loop import AgentLoop
from app.agent.config import (
    ORCHESTRATION_LLM_ID,
    REFLECTION_MAX_HISTORY_TURNS,
    REFLECTION_MAX_TURNS,
)
from app.agent.pipeline_router import StageConfig, advance_stage
from app.agent.tools import build_tool_schemas

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = """\
You are a skeptical reviewer. Your role is to find problems with the work product \
described below — not to be encouraging, but to identify real defects, wrong assumptions, \
and missed edge cases that the producing agent may have overlooked.

Be specific. Vague concerns do not help. If you are uncertain, say so in \
`uncertain_about`. Do not invent issues. A high-confidence clean report is valuable.

You have access to get_task_history_recent to inspect the worker agent's LLM turn \
history if the base context is not enough. Use it when needed; do not call it \
unnecessarily.

When you have completed your analysis, call submit_work with a JSON payload matching \
this schema exactly:

{
  "confidence": <float 0.0-1.0>,
  "issues": [
    {"severity": "blocking"|"warning"|"note", "finding": "<specific description>"}
  ],
  "uncertain_about": ["<thing you could not verify>"]
}

Severity levels:
  blocking  — real defect that should not advance
  warning   — potential issue; orchestrator decides
  note      — cosmetic / speculative; human review only
"""


def _resolve_reflection_llm(
    stage_config: StageConfig,
    fallback_llm_id: "int | None",
) -> "int | None":
    """LLM resolution: stage reflection_llm_id → orchestrator LLM → project default."""
    cfg = stage_config.config or {}
    stage_llm = cfg.get("reflection_llm_id")
    if stage_llm is not None:
        return int(stage_llm)
    if ORCHESTRATION_LLM_ID is not None:
        return ORCHESTRATION_LLM_ID
    return fallback_llm_id


def _build_context_message(
    task_id: str,
    stage_config: StageConfig,
    max_history_turns: int,
) -> str:
    """Assemble the user-turn context for the reflection agent."""
    from app.database import get_task
    from app.agent.doc_store import list_documents

    task = get_task(task_id)
    lines = [
        f"Task ID: {task_id}",
        f"Title: {task.title if task else '(unknown)'}",
        f"Stage: {stage_config.stage_key}",
        f"Description:\n{(task.description or '') if task else ''}",
    ]

    # Surface the most relevant prior-stage output from task.content
    if task and task.content:
        content = task.content if isinstance(task.content, dict) else {}
        for key in ("final_output", "output", "result", "code"):
            if key in content:
                val = str(content[key])[:4000]
                lines.append(f"\n== Prior Stage Output ({key}) ==\n{val}")
                break

    # Inject earlier reflection reports for this task (from other stages)
    try:
        if task and task.project:
            docs = list_documents(task.project, tag="reflection")
            prior = [
                d for d in docs
                if d["key"].startswith(f"reflection:{task_id}:")
                and d["key"] != f"reflection:{task_id}:{stage_config.stage_key}"
            ]
            if prior:
                lines.append(f"\n== Prior Reflection Reports ({len(prior)}) ==")
                for d in prior:
                    lines.append(f"[{d['key']}]\n{d.get('content', '')[:2000]}")
    except Exception as exc:
        logger.debug("ReflectionAgent: could not load prior reflections: %s", exc)

    lines.append(
        f"\nYou may call get_task_history_recent(task_id={task_id!r}, "
        f"max_turns={max_history_turns}) to inspect the worker agent's "
        "LLM turns when the base context is insufficient."
    )
    return "\n".join(lines)


def _store_reflection_report(task_id: str, stage_key: str, report_json: str) -> None:
    """Upsert the reflection report into the project document store."""
    try:
        from app.database import get_task
        from app.agent.doc_store import store_document

        task = get_task(task_id)
        if task and task.project:
            key = f"reflection:{task_id}:{stage_key}"
            store_document(
                task.project,
                key,
                report_json,
                tags=["reflection"],
                written_by_task_id=task_id,
            )
            logger.debug("[reflection_agent] stored report at '%s'", key)
    except Exception as exc:
        logger.warning("[reflection_agent] could not store report for task '%s': %s", task_id, exc)


class ReflectionAgent(AgentLoop):
    """Skeptical reviewer — produces a structured JSON confidence report."""

    _agent_name: str = "reflection_agent"

    def __init__(
        self,
        *,
        task_id: str,
        stage_config: StageConfig,
        llm_id: "int | None",
        budget_id: "int | None",
        llm_base_url: "str | None" = None,
        llm_model: "str | None" = None,
        max_context: "int | None" = None,
    ) -> None:
        cfg = stage_config.config or {}
        max_turns = int(cfg.get("max_turns", REFLECTION_MAX_TURNS))
        self._max_history_turns = int(
            cfg.get("reflection_max_history_turns", REFLECTION_MAX_HISTORY_TURNS)
        )
        resolved_llm_id = _resolve_reflection_llm(stage_config, llm_id)
        super().__init__(
            task_id=task_id,
            llm_id=resolved_llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        self._stage_config = stage_config
        self._system_prompt: str = cfg.get("system_prompt") or _DEFAULT_SYSTEM_PROMPT
        # Read-only: only history inspection + submit_work; no file-write tools
        self._tool_schemas_list = build_tool_schemas(
            ["get_task_history_recent", "submit_work", "report_tool_bug"]
        )

    # ------------------------------------------------------------------
    # AgentLoop abstract interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        user_content = _build_context_message(
            self.task_id, self._stage_config, self._max_history_turns
        )
        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": user_content},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas_list

    async def _on_terminal(self) -> dict:
        signal = self._terminal_signal.get("signal", "")
        payload = self._terminal_signal.get("payload") or {}

        report_json = json.dumps(payload) if payload else json.dumps({
            "confidence": 0.5,
            "issues": [],
            "uncertain_about": ["Agent submitted without a structured payload."],
        })
        _store_reflection_report(self.task_id, self._stage_config.stage_key, report_json)

        # Record blocking issues in episodic memory (best-effort)
        try:
            import app.agent.config as _cfg
            if _cfg.EPISODIC_MEMORY_ENABLED and payload:
                blocking = [i for i in payload.get("issues", []) if i.get("severity") == "blocking"]
                if blocking:
                    from app.database import get_task as _get_task
                    from app.agent.episodic_memory import insert_episode
                    task = _get_task(self.task_id)
                    if task and task.project_id is not None:
                        findings = " ".join(i.get("finding", "") for i in blocking[:3])
                        insert_episode(
                            project_id=task.project_id,
                            task_id=self.task_id,
                            episode_type="failure",
                            content=(
                                f"Task '{task.title}' blocked at stage "
                                f"'{self._stage_config.stage_key}'. {findings}"
                            ),
                            metadata={
                                "stage_key": self._stage_config.stage_key,
                                "task_title": task.title,
                                "outcome": "reflection_block",
                            },
                            settings=_cfg,
                        )
        except Exception:
            pass  # episodic write must never break the reflection flow

        # Always advance — Maestro owns the gate decision, not this agent
        advance_stage(self.task_id, "pass")
        return {"signal": signal, "condition": "pass", "report": payload}

    async def _on_max_turns(self) -> dict:
        logger.warning(
            "[reflection_agent] task '%s' stage '%s': max turns reached — storing incomplete report.",
            self.task_id, self._stage_config.stage_key,
        )
        _store_reflection_report(
            self.task_id,
            self._stage_config.stage_key,
            json.dumps({
                "confidence": 0.0,
                "issues": [],
                "uncertain_about": ["Max turns reached; reflection review is incomplete."],
            }),
        )
        advance_stage(self.task_id, "pass")
        return {"signal": "MAX_TURNS", "condition": "pass"}

    async def _on_error(self, reason: str) -> dict:
        from app.agent.llm_client import is_shutting_down, ShutdownError
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")
        logger.error(
            "[reflection_agent] task '%s' stage '%s': error — %s (stage stays put)",
            self.task_id, self._stage_config.stage_key, reason,
        )
        return {"signal": "ERROR", "reason": reason}
