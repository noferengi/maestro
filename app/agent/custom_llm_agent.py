"""
app/agent/custom_llm_agent.py
------------------------------
CustomLLMAgent — a generic LLM agent whose behavior is entirely driven by a
custom_agent_definitions row rather than hardcoded Python logic.

Reads the definition from the DB, injects its system_prompt, enforces its
allowed_tools list, runs the standard AgentLoop, then uses the pluggable
verifier framework (if configured) to gate the result.

Phase 5 deliverable.  Built-in agents continue to use their own classes;
CustomLLMAgent is only dispatched for stages whose agent_type resolves to a
name registered in custom_agent_definitions.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any

from app.agent.agent_loop import AgentLoop
from app.agent.config import CUSTOM_AGENT_DEFAULT_MAX_TURNS
from app.agent.pipeline_router import StageConfig, advance_stage
from app.agent.tools import TOOL_SCHEMAS, build_tool_schemas
from app.agent.verdicts import Verdict

logger = logging.getLogger(__name__)

# Tools that are always present regardless of allowed_tools configuration.
_ALWAYS_ON_TOOLS = ("submit_work", "report_tool_bug")


def _sanitize_for_format(value: str) -> str:
    """Escape { and } in injected values so str.format_map cannot misinterpret them."""
    return value.replace("{", "{{").replace("}", "}}")


def _get_worktree_diff(task_id: str, project_path: str) -> str:
    """Return the git diff of the task's worktree against HEAD, or '' if unavailable."""
    worktree = os.path.join(project_path, ".maestro-worktrees", task_id)
    if not os.path.isdir(worktree):
        return ""
    try:
        result = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=worktree,
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        return result.stdout or ""
    except Exception:
        return ""


def _default_user_content(task: Any, task_id: str) -> str:
    """Default user message when no user_prompt_template is configured."""
    return (
        f"Task ID: {task_id}\n"
        f"Title: {task.title if task else '(unknown)'}\n"
        f"Description:\n{task.description or '' if task else ''}"
    )


class CustomLLMAgent(AgentLoop):
    """
    Generic LLM agent driven by a custom_agent_definitions row.

    Subclasses AgentLoop so it inherits the full turn-loop, tool dispatch,
    context saturation tracking, and shutdown guard.

    gate_type is read from the definition:
      llm_judge   — the LLM's submit_work signal determines pass/fail
      single_pass — always advance with "pass" after run completes
      none        — no gate; advance with "pass" unconditionally
    """

    _agent_name: str = "custom_llm_agent"

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
        defn = _load_definition(stage_config.agent_type)
        if defn is None:
            raise ValueError(
                f"CustomLLMAgent: no custom_agent_definitions row for name={stage_config.agent_type!r}"
            )

        allowed_tools = list(defn.allowed_tools or [])
        for always_on in _ALWAYS_ON_TOOLS:
            if always_on not in allowed_tools:
                allowed_tools.append(always_on)

        # max_turns: stage_config > definition > maestro.ini default
        cfg = stage_config.config or {}
        stage_max_turns = cfg.get("max_turns")
        max_turns = int(
            stage_max_turns if stage_max_turns is not None
            else defn.max_turns if defn.max_turns is not None
            else CUSTOM_AGENT_DEFAULT_MAX_TURNS
        )

        # max_tokens: stage_config > definition > None (no cap)
        stage_max_tokens = cfg.get("max_tokens")
        resolved_max_tokens = (
            int(stage_max_tokens) if stage_max_tokens is not None
            else defn.max_tokens
        )

        super().__init__(
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            max_tokens=resolved_max_tokens,
        )

        self._system_prompt: str = defn.system_prompt or ""
        self._tool_schemas_list: list[dict] = build_tool_schemas(allowed_tools)
        self._gate_type: str = defn.gate_type or "llm_judge"
        self._stage_config: StageConfig = stage_config
        self._agent_name = f"custom:{defn.name}"

        # Store definition-level verifier fields for use in _on_terminal
        self._defn_verifier: str = defn.verifier or "none"
        self._defn_verifier_cmd: str | None = defn.verifier_cmd
        self._user_prompt_template: str = defn.user_prompt_template or ""

        # Reset per-session tool-success state so re-dispatched tasks start clean
        from app.agent.tool_success_store import reset as _tss_reset
        _tss_reset(task_id)

    # ------------------------------------------------------------------
    # AgentLoop abstract interface
    # ------------------------------------------------------------------

    def _build_messages(self) -> list[dict]:
        from app.database import get_task
        from app.database.crud_projects import get_project_path
        task = get_task(self.task_id)

        if self._user_prompt_template:
            # Build variable map with sanitized values
            project_path = get_project_path(task.project) if task and task.project else ""
            vars: dict[str, str] = {
                "task_id":          _sanitize_for_format(str(self.task_id)),
                "task_title":       _sanitize_for_format(task.title or "" if task else ""),
                "task_description": _sanitize_for_format(task.description or "" if task else ""),
                "task_stage":       _sanitize_for_format(task.stage_key or "" if task else ""),
                "task_project":     _sanitize_for_format(task.project or "" if task else ""),
            }

            # card.* variables: card.diff is a live git diff; others come from task.content
            if project_path:
                vars["card.diff"] = _sanitize_for_format(
                    _get_worktree_diff(self.task_id, project_path)
                )
            else:
                vars["card.diff"] = ""

            if task and isinstance(task.content, dict):
                for k, v in task.content.items():
                    raw = json.dumps(v) if isinstance(v, (dict, list)) else str(v)
                    vars[f"card.{k}"] = _sanitize_for_format(raw)
                    # Also expose as content_<key> for backward compat with plan spec
                    vars[f"content_{k}"] = _sanitize_for_format(raw)

            try:
                user_content = self._user_prompt_template.format_map(vars)
            except (KeyError, ValueError):
                # Unknown placeholder — fall back to default so bad templates don't crash runs
                logger.warning(
                    "[custom_llm_agent] task '%s': user_prompt_template format failed, using default",
                    self.task_id,
                )
                user_content = _default_user_content(task, self.task_id)
        else:
            user_content = _default_user_content(task, self.task_id)

        return [
            {"role": "system", "content": self._system_prompt},
            {"role": "user",   "content": user_content},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas_list

    async def _on_terminal(self) -> dict:
        signal = self._terminal_signal.get("signal", "")
        if self._gate_type == "none" or self._gate_type == "single_pass":
            condition = "pass"
        elif signal in ("ACCEPTED",):
            condition = "pass"
        elif signal in ("REJECTED",):
            condition = "fail"
        else:
            condition = "pass"

        # Run formal verification gate if LLM passed and a verifier is configured.
        # Stage-level verifier overrides the definition-level verifier.
        if condition == "pass":
            cfg = (self._stage_config.config or {}) if self._stage_config else {}
            verifier = cfg.get("verifier") or self._defn_verifier
            verifier_cmd = cfg.get("verifier_cmd") or self._defn_verifier_cmd
            if verifier and verifier != "none":
                from app.agent.verifiers import run_verifier
                # Build a patched StageConfig with the resolved verifier fields
                patched = StageConfig(
                    stage_key=self._stage_config.stage_key if self._stage_config else "",
                    agent_type=self._stage_config.agent_type if self._stage_config else "",
                    config={**(cfg), "verifier": verifier, "verifier_cmd": verifier_cmd},
                    template_id=self._stage_config.template_id if self._stage_config else None,
                )
                logger.info(
                    "[custom_llm_agent] task '%s': running verifier '%s'",
                    self.task_id, verifier,
                )
                if not run_verifier(self.task_id, patched):
                    logger.warning(
                        "[custom_llm_agent] task '%s': verifier '%s' failed — condition -> fail",
                        self.task_id, verifier,
                    )
                    condition = "fail"

        # Check required_tool_successes declared in stage config.
        # e.g. {"required_tool_successes": ["run_lean4", "run_test_pytest"]}
        if condition == "pass":
            cfg = (self._stage_config.config or {}) if self._stage_config else {}
            required = cfg.get("required_tool_successes") or []
            if required:
                from app.agent.tool_success_store import query as _tss_query
                blocked_by: list[str] = []
                for tool_name in required:
                    state = _tss_query(self.task_id, tool_name)
                    if state is not True:
                        label = "never called" if state is None else "called but failed"
                        blocked_by.append(f"{tool_name} ({label})")
                if blocked_by:
                    logger.warning(
                        "[custom_llm_agent] task '%s': required_tool_successes not met: %s — condition -> fail",
                        self.task_id, blocked_by,
                    )
                    condition = "fail"

        # Check required_tool_groups: each inner list is an OR-group; all groups must be satisfied.
        if condition == "pass":
            cfg = (self._stage_config.config or {}) if self._stage_config else {}
            required_groups = cfg.get("required_tool_groups") or []
            if required_groups:
                from app.agent.tool_success_store import query_group as _tss_query_group
                blocked_groups: list[str] = []
                for group in required_groups:
                    if not _tss_query_group(self.task_id, group):
                        blocked_groups.append(f"none of [{', '.join(group)}] succeeded")
                if blocked_groups:
                    logger.warning(
                        "[custom_llm_agent] task '%s': required_tool_groups not met: %s — condition -> fail",
                        self.task_id, blocked_groups,
                    )
                    condition = "fail"

        advance_stage(self.task_id, condition)
        return {"signal": signal, "condition": condition}

    async def _on_max_turns(self) -> dict:
        logger.warning(
            "[custom_llm_agent] task '%s': max turns reached — advancing with 'fail'",
            self.task_id,
        )
        advance_stage(self.task_id, "fail")
        return {"signal": "MAX_TURNS", "condition": "fail"}

    async def _on_error(self, reason: str) -> dict:
        from app.agent.llm_client import is_shutting_down, ShutdownError
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")
        logger.error(
            "[custom_llm_agent] task '%s': error — %s (stage stays put)",
            self.task_id, reason,
        )
        return {"signal": "ERROR", "reason": reason}


# ---------------------------------------------------------------------------
# Helper: load definition with registry fallback
# ---------------------------------------------------------------------------

def _load_definition(agent_type: str):
    """
    Load a custom_agent_definitions row by name.

    Returns None if not found (caller should raise ValueError).
    """
    try:
        from app.database.session import SessionLocal
        from app.database.models import CustomAgentDefinition
        db = SessionLocal()
        try:
            return (
                db.query(CustomAgentDefinition)
                .filter(CustomAgentDefinition.name == agent_type)
                .first()
            )
        finally:
            db.close()
    except Exception as exc:
        logger.error("[custom_llm_agent] _load_definition(%r) failed: %s", agent_type, exc)
        return None
