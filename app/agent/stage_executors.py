"""
app/agent/stage_executors.py
------------------------------
Generic pipeline node executors registered via register_agent_type_executor().

Executor types:
  circuit_breaker        — configurable attempt counter; parks or fails when exhausted
  reflection_agent       — skeptical post-stage reviewer; stores confidence report
  static_analysis_widget — deterministic tree-sitter analysis; no LLM; injects JSON into task.content
  parallel_agents        — fan-out N child tasks (read-only or dangerous_edit); supports
                           dynamic agent lists derived from planning_result fields
  multiplier_node        — crash-survivable fan-out: N child tasks + collapser (vote_tally or judge_select)

Each executor has a public runner function (_run_*) that is registered in
scheduler.py at import time.  The function signature matches the agent-type
executor contract:

    fn(task_id, stage_config, llm_base_url, llm_model, max_context,
       llm_id, budget_id, project_path) -> None

A helper _CollectorAgent class (local to this module) runs an AgentLoop turn
loop but suppresses advance_stage — it just returns the submit_work payload.
This is used by the multiplier_node collapser (judge agent) and parallel subagents.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.agent.agent_loop import AgentLoop
from app.agent.pipeline_router import StageConfig, advance_stage
from app.agent.tools import build_tool_schemas

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# _CollectorAgent — minimal AgentLoop that returns submit_work payload
# ---------------------------------------------------------------------------

class _CollectorAgent(AgentLoop):
    """
    Runs a turn loop and returns the submit_work payload without advancing stage.
    Used as individual voters/proposers in multiplier_node children and the collapser judge.
    """

    def __init__(
        self,
        *,
        task_id: str,
        system_prompt: str,
        tool_allowlist: list[str],
        max_turns: int,
        llm_id: int | None,
        budget_id: int | None,
        llm_base_url: str | None,
        llm_model: str | None,
        max_context: int | None,
        user_message: str,
        agent_name: str = "collector",
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
        self._sys_prompt = system_prompt
        self._user_msg = user_message
        self._agent_name = agent_name
        allowed = list(tool_allowlist or [])
        if "submit_work" not in allowed:
            allowed.append("submit_work")
        self._tool_schemas_list = build_tool_schemas(allowed)

    def _build_messages(self) -> list[dict]:
        return [
            {"role": "system", "content": self._sys_prompt},
            {"role": "user",   "content": self._user_msg},
        ]

    def _get_tool_schemas(self) -> list[dict]:
        return self._tool_schemas_list

    async def _on_terminal(self) -> dict | None:
        return self._terminal_signal.get("payload") or {}

    async def _on_max_turns(self) -> None:
        logger.warning("[collector] task '%s' agent '%s': max turns reached.", self.task_id, self._agent_name)
        return None

    async def _on_error(self, reason: str) -> None:
        logger.error("[collector] task '%s' agent '%s': error — %s", self.task_id, self._agent_name, reason)
        return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _send_inbox_message(task_id: str, task: Any, message: str, source_type: str = "circuit_breaker") -> None:
    try:
        from app.database import create_inbox_message
        create_inbox_message(
            subject=message[:120],
            source_type=source_type,
            task_id=task_id,
            project_id=task.project if task else None,
            task_title=task.title if task else None,
            outcome="parked",
        )
    except Exception:
        logger.exception("[stage_executors] Failed to send inbox message for task '%s'", task_id)


# ---------------------------------------------------------------------------
# Circuit Breaker
# ---------------------------------------------------------------------------

def _run_circuit_breaker(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Configurable attempt counter.  Parks or fails the task when exhausted.

    Stage config shape:
        counter_key      — key in task.content._counters (fallback counter)
        max_attempts     — trigger threshold (default 3)
        count_source     — "transition_results" | "content_counter"
        count_transition — transition key to count in transition_results
        count_outcome    — outcome value to count (default "rejected")
        on_exhaust       — "park" | "fail" | "notify_only" (default "park")
        notify_inbox     — bool (default False)
        exhaust_message  — human-readable message written to history + inbox
    """
    from app.database import get_task, update_task, append_task_history
    from app.database.session import SessionLocal
    from app.database.models import TransitionResult

    cfg = stage_config.config or {}
    counter_key      = cfg.get("counter_key", "circuit_break_count")
    max_attempts     = int(cfg.get("max_attempts", 3))
    count_source     = cfg.get("count_source", "transition_results")
    count_transition = cfg.get("count_transition", "")
    count_outcome    = cfg.get("count_outcome", "rejected")
    on_exhaust       = cfg.get("on_exhaust", "park")
    notify_inbox     = bool(cfg.get("notify_inbox", False))
    exhaust_message  = cfg.get(
        "exhaust_message",
        f"Circuit breaker exhausted at stage '{stage_config.stage_key}' — manual intervention required.",
    )

    task = get_task(task_id)
    if not task:
        return

    blob = dict(task.content or {})

    # Already parked at this stage — skip silently.
    if blob.get("_parked_at_stage") == stage_config.stage_key:
        logger.debug("[circuit_breaker] task '%s' already parked at '%s'.", task_id, stage_config.stage_key)
        return

    # Count attempts.
    count = 0
    if count_source == "transition_results" and count_transition:
        db = SessionLocal()
        try:
            count = (
                db.query(TransitionResult)
                .filter(
                    TransitionResult.task_id == task_id,
                    TransitionResult.transition == count_transition,
                    TransitionResult.outcome == count_outcome,
                )
                .count()
            )
        finally:
            db.close()
    else:
        counters = blob.get("_counters") or {}
        count = int(counters.get(counter_key, 0))

    logger.info(
        "[circuit_breaker] task '%s' stage '%s': count=%d / max=%d.",
        task_id, stage_config.stage_key, count, max_attempts,
    )

    if count < max_attempts:
        advance_stage(task_id, "pass")
        return

    # Exhausted — apply on_exhaust policy.
    if on_exhaust == "park":
        blob["_parked_at_stage"] = stage_config.stage_key
        blob["_parked_reason"] = exhaust_message
        update_task(task_id, content=blob)
        append_task_history(task_id, "circuit_breaker_parked", message=exhaust_message)
        if notify_inbox:
            _send_inbox_message(task_id, task, exhaust_message)
        logger.info("[circuit_breaker] task '%s' parked at stage '%s'.", task_id, stage_config.stage_key)

    elif on_exhaust == "notify_only":
        if notify_inbox:
            _send_inbox_message(task_id, task, exhaust_message)
        advance_stage(task_id, "pass")

    else:  # "fail" or unknown
        logger.info("[circuit_breaker] task '%s' exhausted → fail.", task_id)
        advance_stage(task_id, "fail")


# ---------------------------------------------------------------------------
# Veto tally helper
# ---------------------------------------------------------------------------

def _tally_veto(votes: list) -> str:
    """Returns 'fail' if any vote is REJECTED or NOT_SUITABLE, else 'pass'."""
    from app.agent.verdicts import Verdict
    for v in votes:
        if v.verdict in (Verdict.REJECTED, Verdict.NOT_SUITABLE):
            return "fail"
    return "pass"


def _build_required_keys_preamble(task_id: str, required_input_keys: list[str]) -> str:
    """Fetch task.content and build a preamble block for required_input_keys."""
    if not required_input_keys:
        return ""
    try:
        from app.database import get_task as _get_task
        t = _get_task(task_id)
        blob = (t.content or {}) if t else {}
        lines = ["\n== Prior Stage Outputs =="]
        for key in required_input_keys:
            if key in blob:
                lines.append(f"{key}: {blob[key]}")
        return "\n".join(lines) + "\n\n"
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Reflection Agent
# ---------------------------------------------------------------------------

def _run_reflection_agent(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Run ReflectionAgent for a pipeline stage of agent_type 'reflection_agent'.

    Stores a structured JSON confidence report at
    reflection:{task_id}:{stage_key} in the project document store, then
    advances the stage unconditionally (condition='pass').  Maestro reads
    the report on its next tick and decides consequence.

    Stage config keys (all optional):
        system_prompt                — override default skeptical-reviewer prompt
        reflection_llm_id            — specific LLM for this reflection stage
        reflection_max_history_turns — cap on get_task_history_recent (default 20)
        max_turns                    — agent turn limit (default 150)
    """
    from app.database import create_agent_session, close_agent_session
    from app.agent.reflection_agent import ReflectionAgent

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"reflection_agent:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        agent = ReflectionAgent(
            task_id=task_id,
            stage_config=stage_config,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        result = loop.run_until_complete(agent.run())
        exit_reason = (result or {}).get("condition", "pass") if isinstance(result, dict) else "pass"

    except Exception:
        logger.exception("[reflection_agent] task '%s' stage '%s' raised.", task_id, stage_config.stage_key)
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Static Analysis Widget
# ---------------------------------------------------------------------------

def _run_static_analysis_widget(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Deterministic non-LLM node. Runs tree-sitter on the project folder and writes
    structured JSON to task.content[output_key]. Advances with 'pass' immediately.

    Stage config shape:
        output_key    — task.content key to write result (default "static_analysis")
        file_pattern  — glob filter applied to relative file paths (default "**/*.py")
        max_files     — cap to avoid large projects (default 50)
    """
    import fnmatch
    import os

    from app.database import get_task, update_task
    from app.agent.path_filter import walk_safe

    cfg         = stage_config.config or {}
    output_key  = cfg.get("output_key", "static_analysis")
    file_pattern = cfg.get("file_pattern", "**/*.py")
    max_files   = int(cfg.get("max_files", 50))

    if not project_path:
        logger.warning("[static_analysis_widget] task '%s': no project_path — skipping analysis.", task_id)
        advance_stage(task_id, "pass")
        return

    # Collect matching files
    file_paths: list[str] = []
    # Normalise pattern for per-filename matching (last component of a glob)
    _basename_pattern = file_pattern.split("/")[-1] if "/" in file_pattern else file_pattern
    for root, dirs, files in walk_safe(project_path):
        for fname in files:
            full = os.path.join(root, fname)
            rel  = os.path.relpath(full, project_path).replace("\\", "/")
            if fnmatch.fnmatch(rel, file_pattern) or fnmatch.fnmatch(fname, _basename_pattern):
                file_paths.append(full)
            if len(file_paths) >= max_files:
                break
        if len(file_paths) >= max_files:
            break

    if not file_paths:
        logger.info("[static_analysis_widget] task '%s': no files matched pattern '%s'.", task_id, file_pattern)
        task = get_task(task_id)
        blob = dict((task.content or {}) if task else {})
        blob[output_key] = {"file_count": 0, "files": {}, "import_graph": {}, "reverse_import_graph": {}}
        update_task(task_id, content=blob)
        advance_stage(task_id, "pass")
        return

    try:
        from app.agent.static_analysis import analyze_project, _file_analysis_to_dict
        analysis = analyze_project(file_paths)
        result = {
            "file_count": len(analysis.files),
            "files": {
                path: _file_analysis_to_dict(fa)
                for path, fa in analysis.files.items()
            },
            "import_graph": analysis.import_graph,
            "reverse_import_graph": analysis.reverse_import_graph,
        }
    except Exception:
        logger.exception("[static_analysis_widget] task '%s': analyze_project failed.", task_id)
        result = {"error": "analysis failed", "file_count": len(file_paths)}

    task = get_task(task_id)
    blob = dict((task.content or {}) if task else {})
    blob[output_key] = result
    update_task(task_id, content=blob)

    logger.info(
        "[static_analysis_widget] task '%s': analysed %d file(s) → '%s'.",
        task_id, result.get("file_count", 0), output_key,
    )
    advance_stage(task_id, "pass")


# ---------------------------------------------------------------------------
# dangerous_edit_llm_agent — wraps MaestroLoop with stage-config overrides
# ---------------------------------------------------------------------------

def _record_demotion(task_id: str, from_stage: str, to_stage: str, reason: str) -> None:
    """Local copy of scheduler._record_demotion_inline — avoids circular import."""
    import asyncio
    from datetime import datetime, timezone
    from app.database import get_task, update_task
    from app.agent.pip_agent import generate_pip

    task = get_task(task_id)
    if not task:
        return
    history = task.demotion_history or []
    history.append({
        "from": from_stage,
        "to": to_stage,
        "reason": reason[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    update_task(task_id, demotion_count=(task.demotion_count or 0) + 1, demotion_history=history)

    review_stages = {"conceptual_review", "optimization", "security", "human_review"}
    if from_stage in review_stages:
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(generate_pip(task_id, from_stage, reason))
        except RuntimeError:
            asyncio.run(generate_pip(task_id, from_stage, reason))


def _run_dangerous_edit_llm_agent(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Executor for dangerous_edit_llm_agent — wraps MaestroLoop with worktree-isolated
    writes and per-stage overrides for system_prompt, agent_tools, max_turns, and
    required_input_keys.

    Stage config shape:
        system_prompt        — override MAESTRO_SYSTEM_PROMPT (empty/absent = default)
        max_turns            — integer cap (default from maestro.ini)
        agent_tools          — comma-separated string or list of tool names (absent = INDEV_AGENT_TOOLS)
        required_input_keys  — comma-separated string or list; values injected from task.content
    """
    import asyncio
    import json as _json

    from app.agent.loop import MaestroLoop
    from app.agent.config import MAX_TURNS as _DEFAULT_MAX_TURNS
    from app.database import (
        get_task,
        update_task,
        create_agent_session,
        close_agent_session,
        create_inbox_message,
    )
    from app.agent.pipeline_router import advance_stage

    cfg = stage_config.config or {}
    system_prompt = cfg.get("system_prompt") or None  # empty string → None (use default)
    max_turns = int(cfg.get("max_turns", _DEFAULT_MAX_TURNS))
    stage_key = stage_config.stage_key

    # agent_tools: stored as JSON list or comma-sep string from the pipeline editor
    _raw_tools = cfg.get("agent_tools")
    if isinstance(_raw_tools, list):
        agent_tools: list[str] | None = [t.strip() for t in _raw_tools if t.strip()] or None
    elif isinstance(_raw_tools, str) and _raw_tools.strip():
        agent_tools = [t.strip() for t in _raw_tools.split(",") if t.strip()] or None
    else:
        agent_tools = None  # falls back to INDEV_AGENT_TOOLS

    # required_input_keys: same dual-format handling
    _raw_keys = cfg.get("required_input_keys", [])
    if isinstance(_raw_keys, list):
        required_keys: list[str] = [k.strip() for k in _raw_keys if k.strip()]
    elif isinstance(_raw_keys, str) and _raw_keys.strip():
        required_keys = [k.strip() for k in _raw_keys.split(",") if k.strip()]
    else:
        required_keys = []

    _session_id = None
    _exit_reason = "error"
    _exit_summary = ""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _session_id = create_agent_session(
            task_id=task_id,
            agent_type="dangerous_edit_llm_agent",
            llm_id=llm_id,
            budget_id=budget_id,
            scheduler_reason="scheduler",
            max_turns=max_turns,
        )

        maestro = MaestroLoop(
            task_id=task_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=project_path,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
            required_input_keys=required_keys,
        )
        result = loop.run_until_complete(maestro.run())
        _exit_summary = result.final_message or ""

        if result.status == "ACCEPTED":
            _exit_reason = "completed"
            advance_stage(task_id, "pass", from_stage=stage_key)

        elif result.status == "NEEDS_HUMAN":
            _exit_reason = "needs_human"
            advance_stage(task_id, "pass", from_stage=stage_key)
            task_obj = get_task(task_id)
            create_inbox_message(
                subject=f"Human review needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="needs_human",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="needs_human",
                data_json=_json.dumps({"summary": _exit_summary}),
            )

        elif result.status == "CONSULTING":
            _exit_reason = "consulting"
            update_task(task_id, consultation_payload=_json.dumps({
                "question": result.consultation_question,
                "hint": None,
                "source": None,
            }))
            task_obj = get_task(task_id)
            create_inbox_message(
                subject=f"Consultation needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="consultation",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="consultation",
                data_json=_json.dumps({
                    "question": result.consultation_question,
                    "summary": _exit_summary,
                }),
            )

        elif result.status in ("REVERT_TO_DESIGN", "REJECTED"):
            _exit_reason = "rejected"
            advance_stage(task_id, "fail", from_stage=stage_key)
            _record_demotion(task_id, stage_key, "planning",
                             result.final_message or "Agent requested revert")

        elif result.status in ("MAX_TURNS", "ERROR"):
            _exit_reason = result.status.lower()
            advance_stage(task_id, "fail", from_stage=stage_key)
            _record_demotion(task_id, stage_key, "planning",
                             f"{result.status} in dangerous_edit_llm_agent stage.")

    finally:
        loop.close()
        if _session_id is not None:
            close_agent_session(_session_id, _exit_reason, _exit_summary)


# ---------------------------------------------------------------------------
# parallel_agents — helpers for dynamic agent list construction
# ---------------------------------------------------------------------------

_DEFAULT_COMPONENT_PROMPT_TPL: str = (
    "You are implementing component '{component}'.\n"
    "Your assigned files: {files}\n\n"
    "Planning context:\n{planning_context}\n\n"
    "Write or update only your assigned files. "
    "Call submit_work with signal=ACCEPTED when done, "
    "or signal=REVERT_TO_DESIGN if the design is fundamentally wrong."
)


def _load_planning_context_for_task(task_id: str) -> str:
    """Return a JSON planning context string (capped at 8 KiB) for use in component prompts."""
    try:
        import json as _j
        from app.database import get_planning_result as _gpr
        pr = _gpr(task_id)
        if not pr:
            return ""
        ctx = {
            "implementation_steps": _j.loads(pr.implementation_steps or "[]"),
            "file_manifest": _j.loads(pr.file_manifest or "[]"),
            "interface_contracts": _j.loads(pr.interface_contracts or "[]"),
        }
        raw = _j.dumps(ctx, indent=1)
        return raw[:8000] + ("\n...[truncated]" if len(raw) > 8000 else "")
    except Exception:
        logger.warning("[parallel_agents] failed to load planning context for task '%s'", task_id, exc_info=True)
        return ""


def _build_dynamic_agents(task_id: str, dynamic_key: str, cfg: dict) -> list[dict]:
    """
    Build an agent-spec list from a planning_result column or task.content key.

    When cfg["items_from_content_key"] is set, reads items from task.content[key]
    instead of from a planning_results column.  Each item must have at least a
    "component" key (falling back to "path") and an optional "files" list.

    Otherwise, dynamic_key is treated as a planning_results column name
    (e.g. "implementation_steps").
    """
    import json as _j
    from app.database import get_task as _gt

    content_key: str | None = cfg.get("items_from_content_key")
    if content_key:
        task = _gt(task_id)
        raw_items = (task.content or {}).get(content_key, []) if task else []
        if not isinstance(raw_items, list):
            logger.warning(
                "[parallel_agents] task '%s': items_from_content_key='%s' is not a list.",
                task_id, content_key,
            )
            return []
    else:
        from app.database import get_planning_result as _gpr
        pr = _gpr(task_id)
        if not pr:
            logger.warning(
                "[parallel_agents] task '%s': dynamic_agents_from_key='%s' but no planning_result found.",
                task_id, dynamic_key,
            )
            return []
        try:
            raw_items = _j.loads(getattr(pr, dynamic_key, None) or "[]")
        except (_j.JSONDecodeError, TypeError):
            logger.warning(
                "[parallel_agents] task '%s': could not parse planning_result.%s as JSON list.",
                task_id, dynamic_key,
            )
            return []

    if not raw_items:
        return []

    planning_context = _load_planning_context_for_task(task_id)
    tpl: str = cfg.get("agent_system_prompt_template", _DEFAULT_COMPONENT_PROMPT_TPL)
    subagent_type: str = cfg.get("subagent_type", "dangerous_edit")
    agent_max_turns: int = int(cfg.get("max_turns", 200))
    # agent_tools from cfg-level is inherited by all dynamic children
    agent_tools: list[str] | None = cfg.get("agent_tools")

    agents: list[dict] = []
    for item in raw_items:
        component: str = item.get("component") or item.get("path", "unknown")
        files: list[str] = item.get("files") or ([item["path"]] if item.get("path") else [])
        spec: dict = {
            "name": component,
            "system_prompt": tpl.format(
                component=component,
                files=", ".join(files),
                planning_context=planning_context,
            ),
            "subagent_type": subagent_type,
            "max_turns": agent_max_turns,
        }
        if agent_tools is not None:
            spec["agent_tools"] = agent_tools
        agents.append(spec)
    return agents


# ---------------------------------------------------------------------------
# parallel_agents — fan-out creator
# ---------------------------------------------------------------------------

def _run_parallel_agents(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Fan-out creator for the parallel_agents node.

    Creates N child _psubagent tasks plus one _psubagent_join aggregator, then
    appends the aggregator ID to the parent's prerequisites to block re-dispatch
    until all children complete.  Does NOT call advance_stage — the aggregator
    drives the parent forward.
    """
    from app.database import get_task, update_task, create_task

    cfg: dict = stage_config.config or {}
    agents_cfg: list[dict] = list(cfg.get("agents", []))
    output_key: str = cfg.get("output_key", "parallel_agents_output")
    max_turns: int = int(cfg.get("max_turns", 30))

    parent = get_task(task_id)
    if not parent:
        return

    # Dynamic agent list: build from a planning_result field when no static agents are configured.
    dynamic_key: str | None = cfg.get("dynamic_agents_from_key")
    if dynamic_key and not agents_cfg:
        agents_cfg = _build_dynamic_agents(task_id, dynamic_key, cfg)
        if not agents_cfg:
            logger.warning(
                "[parallel_agents] task '%s': dynamic_agents_from_key='%s' produced no agents — staying put.",
                task_id, dynamic_key,
            )
            return

    # Idempotency guard: skip if children already created
    if (parent.content or {}).get("_psubagent_child_ids"):
        return

    content = dict(parent.content or {})
    content["_psubagent_waiting"] = True
    update_task(task_id, content=content)

    child_ids: list[str] = []
    for i, agent in enumerate(agents_cfg):
        name = agent.get("name", f"agent_{i}")
        tg_id = agent.get("tool_grouping_id")
        subagent_type: str = agent.get("subagent_type", "collector")
        child_task_type = "_psubagent_dangerous" if subagent_type == "dangerous_edit" else "_psubagent"
        child = create_task(
            title=f"[PA] {parent.title[:50]} — {name}",
            task_type=child_task_type,
            stage_key=child_task_type,
            project_id=parent.project_id,
            pipeline_template_id=None,
            llm_id=llm_id,
            budget_id=budget_id,
            content={"_subagent_cfg": {
                "name": name,
                "system_prompt": agent.get("system_prompt", "Complete the task and call submit_work."),
                "max_turns": agent.get("max_turns", max_turns),
                "output_key": output_key,
                "parent_task_id": task_id,
                "parent_stage_key": stage_config.stage_key,
                "tool_grouping_id": tg_id,
                "agent_tools": agent.get("agent_tools"),
            }},
        )
        if child:
            child_ids.append(child.id)

    agg = create_task(
        title=f"[PA-join] {parent.title[:50]}",
        task_type="_psubagent_join",
        stage_key="_psubagent_join",
        project_id=parent.project_id,
        pipeline_template_id=None,
        llm_id=llm_id,
        budget_id=budget_id,
        prerequisites=child_ids,
        content={"_subagent_cfg": {
            "parent_task_id": task_id,
            "output_key": output_key,
            "parent_stage_key": stage_config.stage_key,
            "child_ids": child_ids,
        }},
    )

    content["_psubagent_child_ids"] = child_ids
    content["_psubagent_agg_id"] = agg.id if agg else None
    existing_prereqs = list(parent.prerequisites or [])
    update_task(task_id, content=content,
                prerequisites=existing_prereqs + ([agg.id] if agg else []))

    logger.info(
        "[parallel_agents] task '%s': created %d children + aggregator '%s'.",
        task_id, len(child_ids), (agg.id if agg else "None"),
    )


# ---------------------------------------------------------------------------
# _psubagent — runs one parallel sub-agent child
# ---------------------------------------------------------------------------

def _run_parallel_subagent(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Runs a single parallel sub-agent child.  Config comes from task.content._subagent_cfg
    (injected by _run_parallel_agents at creation time).
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    name: str = cfg.get("name", "subagent")
    system_prompt: str = cfg.get("system_prompt", "Complete the task and call submit_work.")
    max_turns: int = int(cfg.get("max_turns", 30))
    parent_task_id: str | None = cfg.get("parent_task_id")
    tg_id: int | None = cfg.get("tool_grouping_id")

    # Resolve tool allowlist from tool grouping
    tool_allowlist: list[str] = ["submit_work"]
    if tg_id is not None:
        try:
            from app.database.crud_malleable import get_tool_grouping
            tg = get_tool_grouping(tg_id)
            if tg:
                tool_allowlist = list(tg.get("tools", ["submit_work"]))
        except Exception:
            logger.exception("[parallel_subagent] task '%s': failed to load tool grouping %s.", task_id, tg_id)

    parent = get_task(parent_task_id) if parent_task_id else None
    user_msg = (
        f"Task: {(parent.title if parent else task.title)}\n"
        f"Description:\n{(parent.description or '') if parent else (task.description or '')}\n\n"
        "Complete your assigned work. Use submit_work with:\n"
        "  signal='ACCEPTED', payload={'output': '<your full output>'}"
    )

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"parallel_subagent:{name}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        agent = _CollectorAgent(
            task_id=task_id,
            system_prompt=system_prompt,
            tool_allowlist=tool_allowlist,
            max_turns=max_turns,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            user_message=user_msg,
            agent_name=f"subagent:{name}",
        )
        payload = loop.run_until_complete(agent.run())
        output = (payload or {}).get("output", str(payload or ""))
        blob = dict(task.content or {})
        blob["output"] = output
        update_task(task_id, content=blob, type="completed", stage_key="completed")
        exit_reason = "completed"
    except Exception:
        logger.exception("[parallel_subagent] task '%s' agent '%s' raised.", task_id, name)
        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["output"] = f"ERROR: subagent '{name}' failed."
        blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
        # Still complete so the aggregator can fire
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _psubagent_dangerous — write-capable parallel subagent (MaestroLoop)
# ---------------------------------------------------------------------------

def _run_parallel_subagent_dangerous(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Write-capable parallel subagent — runs a scoped MaestroLoop for one component.
    Config comes from task.content._subagent_cfg (injected by _run_parallel_agents).

    Unlike _run_parallel_subagent (_CollectorAgent, read-only), this variant has a
    worktree and full write access.  It does NOT call advance_stage; the aggregator
    drives the parent forward once all children complete.
    """
    import json as _json
    from app.agent.loop import MaestroLoop
    from app.agent.config import MAX_TURNS as _DEFAULT_MAX_TURNS
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    name: str = cfg.get("name", "subagent")
    system_prompt: str | None = cfg.get("system_prompt") or None
    max_turns: int = int(cfg.get("max_turns", _DEFAULT_MAX_TURNS))

    # agent_tools: list or comma-sep string; None falls back to INDEV_AGENT_TOOLS inside MaestroLoop
    _raw_tools = cfg.get("agent_tools")
    if isinstance(_raw_tools, list):
        agent_tools: list[str] | None = [t.strip() for t in _raw_tools if t.strip()] or None
    elif isinstance(_raw_tools, str) and _raw_tools.strip():
        agent_tools = [t.strip() for t in _raw_tools.split(",") if t.strip()] or None
    else:
        agent_tools = None

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"parallel_subagent_dangerous:{name}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
        max_turns=max_turns,
    )
    exit_reason = "error"
    exit_summary = ""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        maestro = MaestroLoop(
            task_id=task_id,
            max_turns=max_turns,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=project_path,
            system_prompt=system_prompt,
            agent_tools=agent_tools,
            require_passing_tests=cfg.get("require_passing_tests", False),
            file_manifest=cfg.get("assigned_files"),
        )
        result = loop.run_until_complete(maestro.run())
        exit_summary = result.final_message or ""
        blob = dict(task.content or {})
        if result.status == "ACCEPTED":
            exit_reason = "completed"
            blob["output"] = exit_summary
        else:
            exit_reason = result.status.lower()
            blob["output"] = f"subagent '{name}' ended with status {result.status}: {exit_summary}"
            blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
    except Exception:
        logger.exception("[parallel_subagent_dangerous] task '%s' agent '%s' raised.", task_id, name)
        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["output"] = f"ERROR: subagent '{name}' failed."
        blob["_subagent_failed"] = True
        update_task(task_id, content=blob, type="completed", stage_key="completed")
    finally:
        close_agent_session(session_id, exit_reason, exit_summary)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _psubagent_join — aggregator: merges outputs and advances parent
# ---------------------------------------------------------------------------

def _run_parallel_subagent_aggregator(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Aggregator for parallel_agents.  Merges child outputs into the parent task's
    content and calls advance_stage on the parent so it proceeds to the next stage.
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_subagent_cfg", {})
    parent_task_id: str | None = cfg.get("parent_task_id")
    output_key: str = cfg.get("output_key", "parallel_agents_output")
    parent_stage_key: str = cfg.get("parent_stage_key", "")
    child_ids: list[str] = cfg.get("child_ids", [])

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="parallel_subagent_aggregator",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    try:
        merged: dict[str, str] = {}
        for cid in child_ids:
            child = get_task(cid)
            if not child:
                continue
            child_cfg = (child.content or {}).get("_subagent_cfg", {})
            name = child_cfg.get("name", cid)
            merged[name] = (child.content or {}).get("output", "")

        parent = get_task(parent_task_id) if parent_task_id else None
        if parent:
            parent_blob = dict(parent.content or {})
            parent_blob[output_key] = merged
            parent_blob.pop("_psubagent_waiting", None)
            update_task(parent_task_id, content=parent_blob)

        update_task(task_id, type="completed", stage_key="completed")
        advance_stage(parent_task_id, "pass", from_stage=parent_stage_key)
        exit_reason = "completed"
        logger.info(
            "[parallel_subagent_aggregator] task '%s': merged %d outputs → parent '%s' stage '%s'.",
            task_id, len(merged), parent_task_id, parent_stage_key,
        )
    except Exception:
        logger.exception("[parallel_subagent_aggregator] task '%s' raised.", task_id)
    finally:
        close_agent_session(session_id, exit_reason, "")


# ---------------------------------------------------------------------------
# json_schema_gate — deterministic plan validation + retry routing
# ---------------------------------------------------------------------------

def _validate_non_empty_list(value: Any) -> tuple[bool, str]:
    if isinstance(value, list):
        return (len(value) > 0), ("empty list" if not value else "ok")
    if isinstance(value, str):
        import json as _j
        try:
            parsed = _j.loads(value)
            if isinstance(parsed, list):
                return (len(parsed) > 0), ("empty list" if not parsed else "ok")
        except _j.JSONDecodeError:
            pass
    return False, f"not a list (got {type(value).__name__})"


def _validate_valid_dag(value: Any) -> tuple[bool, str]:
    if not value:
        return True, "empty graph — no cycles possible"
    if isinstance(value, str):
        import json as _j
        try:
            value = _j.loads(value)
        except _j.JSONDecodeError:
            return False, "not valid JSON"
    if not isinstance(value, dict):
        return False, f"not a dict (got {type(value).__name__})"
    from app.agent.static_analysis import _detect_cycles
    cycles = _detect_cycles(value)
    if cycles:
        cycle_strs = [" -> ".join(c) for c in cycles[:3]]
        return False, f"{len(cycles)} cycle(s): {'; '.join(cycle_strs)}"
    return True, f"no cycles in {len(value)} nodes"


_VALIDATORS = {
    "non_empty_list": _validate_non_empty_list,
    "valid_dag":       _validate_valid_dag,
}


def _run_json_schema_gate(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Validate fields from a planning_result row or task.content.  Routes to
    retry → correction stage or fail → park when retries exhausted.

    Stage config shape:
        source           — "planning_result" (default) or "task_content"
        required_fields  — list of {key, validator, hard_fail?}
        on_pass          — condition on success (default "pass")
        on_fail          — condition when retries exhausted (default "fail")
        max_retries      — number of correction attempts before failing (default 3)
        retry_condition  — condition to fire when retrying (default "retry")
        output_key       — task.content key for gate result (default "gate_result")

    When source="task_content", required_fields[*].key is resolved directly from
    task.content rather than from a planning_results table column.
    """
    import json as _j
    from app.database import get_task, update_task, get_planning_result

    cfg = stage_config.config or {}
    source: str = cfg.get("source", "planning_result")
    required_fields: list[dict] = cfg.get("required_fields") or []
    on_pass: str = cfg.get("on_pass", "pass")
    on_fail: str = cfg.get("on_fail", "fail")
    max_retries: int = int(cfg.get("max_retries", 3))
    retry_condition: str = cfg.get("retry_condition", "retry")
    output_key: str = cfg.get("output_key", "gate_result")

    task = get_task(task_id)
    if not task:
        return

    blob = dict(task.content or {})
    retry_count: int = int(blob.get("_gate_retry_count", 0))

    # Build field_map from the configured source
    field_map: dict[str, Any] = {}
    if source == "task_content":
        content_data = task.content or {}
        for field_cfg in required_fields:
            key = field_cfg.get("key", "")
            if key:
                field_map[key] = content_data.get(key)
    else:
        # Default: read from planning_results table columns
        pr = get_planning_result(task_id)
        if not pr:
            logger.warning("[json_schema_gate] task '%s': no planning_result found — fail.", task_id)
            blob[output_key] = {"passed": False, "failures": ["no planning_result"]}
            update_task(task_id, content=blob)
            advance_stage(task_id, on_fail)
            return

        for field_cfg in required_fields:
            key = field_cfg.get("key", "")
            if not key:
                continue
            raw = getattr(pr, key, None)
            if raw is None:
                field_map[key] = None
                continue
            if isinstance(raw, str):
                try:
                    field_map[key] = _j.loads(raw)
                except _j.JSONDecodeError:
                    field_map[key] = raw
            else:
                field_map[key] = raw

    failures: list[dict] = []
    for field_cfg in required_fields:
        key = field_cfg.get("key", "")
        validator_name = field_cfg.get("validator", "non_empty_list")
        hard_fail: bool = field_cfg.get("hard_fail", True)
        if not key:
            continue
        value = field_map.get(key)
        validator = _VALIDATORS.get(validator_name, _validate_non_empty_list)
        passed, detail = validator(value)
        if not passed:
            failures.append({"key": key, "detail": detail, "hard_fail": hard_fail})

    hard_failures = [f for f in failures if f.get("hard_fail", True)]

    blob[output_key] = {
        "passed": len(hard_failures) == 0,
        "failures": failures,
        "retry_count": retry_count,
    }

    if not hard_failures:
        blob["_gate_retry_count"] = 0
        blob.pop("_gate_failures", None)
        update_task(task_id, content=blob)
        logger.info("[json_schema_gate] task '%s': passed (%d soft failures).", task_id, len(failures))
        advance_stage(task_id, on_pass)
        return

    logger.info(
        "[json_schema_gate] task '%s': %d hard failure(s) — retry %d/%d.",
        task_id, len(hard_failures), retry_count, max_retries,
    )

    if retry_count < max_retries:
        blob["_gate_retry_count"] = retry_count + 1
        blob["_gate_failures"] = hard_failures
        update_task(task_id, content=blob)
        advance_stage(task_id, retry_condition)
    else:
        blob["_gate_retry_count"] = retry_count
        blob["_gate_failures"] = hard_failures
        update_task(task_id, content=blob)
        logger.info("[json_schema_gate] task '%s': retries exhausted — fail.", task_id)
        advance_stage(task_id, on_fail)


# ---------------------------------------------------------------------------
# planning_correction_stage — thin wrapper around PlanningCorrectionAgent
# ---------------------------------------------------------------------------

def _run_planning_correction_stage(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Thin executor wrapper for PlanningCorrectionAgent.

    Reads _gate_failures from task.content, runs the correction agent, then
    advances with 'pass' (back to json_schema_gate) or 'fail' (park).
    """
    from app.database import (
        get_task, get_planning_result, get_tasks,
        create_agent_session, close_agent_session,
    )
    from app.agent.planning_correction import PlanningCorrectionAgent
    import json as _j

    task = get_task(task_id)
    if not task:
        return

    blob = dict(task.content or {})
    gate_failures: list[dict] = blob.get("_gate_failures", [])

    pr = get_planning_result(task_id)
    if not pr:
        logger.warning("[planning_correction_stage] task '%s': no planning_result — fail.", task_id)
        advance_stage(task_id, "fail")
        return

    try:
        current_plan = {
            "file_manifest":        _j.loads(pr.file_manifest or "[]"),
            "implementation_steps": _j.loads(pr.implementation_steps or "[]"),
            "interface_contracts":  _j.loads(pr.interface_contracts or "[]"),
            "dependency_graph":     _j.loads(pr.dependency_graph or "{}"),
            "test_strategy":        _j.loads(pr.test_strategy or "[]"),
        }
    except (_j.JSONDecodeError, TypeError):
        current_plan = {}

    all_tasks = [
        {"id": t.id, "type": t.type, "prerequisites": t.prerequisites or []}
        for t in get_tasks()
    ]

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"planning_correction_stage:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        agent = PlanningCorrectionAgent(
            task_id=task_id,
            planning_result_id=pr.id,
            current_plan=current_plan,
            gate_failures=gate_failures,
            project_root=project_path,
            llm_id=llm_id,
            budget_id=budget_id,
            all_tasks=all_tasks,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            task_title=task.title or "",
            task_description=task.description or "",
        )
        result = loop.run_until_complete(agent.run())
        outcome = (result or {}).get("outcome", "error")
        if outcome == "corrected":
            advance_stage(task_id, "pass")
            exit_reason = "pass"
        else:
            advance_stage(task_id, "fail")
            exit_reason = "fail"
    except Exception:
        logger.exception(
            "[planning_correction_stage] task '%s' stage '%s' raised.", task_id, stage_config.stage_key
        )
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Phase 6 — Decomposed planning nodes
# ---------------------------------------------------------------------------

def _run_planning_survey_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """planning_survey_node executor — codebase survey + task classification.

    Writes to task.content: survey_summary, is_proof, is_simple, best_of_n.
    """
    from app.database import create_agent_session, close_agent_session, get_task, update_task
    from app.agent.planning_utils import run_planning_survey

    task = get_task(task_id)
    if not task:
        return

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning_survey",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_planning_survey(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description or "",
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                max_context=max_context,
                project_path=project_path,
                project_name=task.project,
            )
        )
        blob = dict(task.content or {})
        blob["survey_summary"] = result["survey_summary"]
        blob["is_proof"] = result["is_proof"]
        blob["is_simple"] = result["is_simple"]
        blob["best_of_n"] = result["best_of_n"]
        update_task(task_id, content=blob)
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[planning_survey_node] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_pitfall_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """planning_pitfalls node — deterministic checks + LLM pitfall detection.

    Reads winning_design + survey_summary from task.content.
    Writes pitfalls list to task.content. Always advances to pass (informational).
    """
    from app.database import create_agent_session, close_agent_session, get_task, update_task
    from app.agent.planning_utils import run_pitfall_detection

    task = get_task(task_id)
    if not task:
        return

    content = task.content or {}
    winning_design = content.get("winning_design") or {}
    survey_summary = content.get("survey_summary") or ""
    is_proof = bool(content.get("is_proof", False))

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning_pitfalls",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "pass"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pitfalls = loop.run_until_complete(
            run_pitfall_detection(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description or "",
                winning_design=winning_design,
                survey_summary=survey_summary,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                max_context=max_context,
                project_path=project_path,
                project_name=task.project,
                is_proof=is_proof,
            )
        )
        blob = dict(task.content or {})
        blob["pitfalls"] = pitfalls
        update_task(task_id, content=blob)
    except Exception:
        logger.exception("[pitfall_node] task '%s' raised.", task_id)
    finally:
        advance_stage(task_id, "pass")  # always informational — never blocks
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_consolidation_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """planning_consolidate node — merge winning_design + pitfalls, store PlanningResult.

    Reads winning_design, pitfalls, survey_summary from task.content.
    Stores a PlanningResult row. Advances to pass on success.
    """
    from app.database import (
        create_agent_session, close_agent_session, get_task, get_all_tasks,
        task_to_dict, supersede_planning_results,
    )
    from app.agent.planning_utils import run_consolidation_and_store

    task = get_task(task_id)
    if not task:
        return

    content = task.content or {}
    winning_design = content.get("winning_design") or {}
    pitfalls = content.get("pitfalls") or []
    survey_summary = content.get("survey_summary") or ""
    is_proof = bool(content.get("is_proof", False))

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning_consolidate",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        all_tasks = [task_to_dict(t) for t in get_all_tasks()]
        supersede_planning_results(task_id)
        loop.run_until_complete(
            run_consolidation_and_store(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description or "",
                winning_design=winning_design,
                pitfalls=pitfalls,
                survey_summary=survey_summary,
                all_tasks=all_tasks,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                max_context=max_context,
                project_path=project_path,
                project_name=task.project,
                is_proof=is_proof,
            )
        )
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[consolidation_node] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_planning_gate_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """planning_gate node — runs PlanningGate checks on the stored PlanningResult.

    Pass → json_schema_gate. Fail → planning_correction.
    """
    import json as _json
    from app.database import (
        create_agent_session, close_agent_session, get_task, get_all_tasks,
        task_to_dict, get_planning_result,
    )
    from app.agent.planning_gate import run_planning_gate
    from app.agent.planning_utils import _get_domain

    task = get_task(task_id)
    if not task:
        return

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning_gate",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pr = get_planning_result(task_id)
        if not pr:
            logger.warning("[planning_gate_node] task '%s': no planning result found.", task_id)
            advance_stage(task_id, "fail")
            exit_reason = "fail"
        else:
            planning_result_dict = {
                "file_manifest": _json.loads(pr.file_manifest or "[]"),
                "interface_contracts": _json.loads(pr.interface_contracts or "[]"),
                "implementation_steps": _json.loads(pr.implementation_steps or "[]"),
                "design_rationale": pr.design_rationale or "",
                "dependency_graph": _json.loads(pr.dependency_graph or "{}"),
                "test_strategy": _json.loads(pr.test_strategy or "[]"),
                "outcome": "passed",
            }
            all_tasks = [task_to_dict(t) for t in get_all_tasks()]
            try:
                domain = _get_domain(
                    getattr(task, "pipeline_template_id", None),
                    task.title,
                    task.description or "",
                )
            except Exception:
                domain = "software"
            gate_result = loop.run_until_complete(
                run_planning_gate(
                    task_id=task_id,
                    planning_result=planning_result_dict,
                    all_tasks=all_tasks,
                    max_context=max_context,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=llm_id,
                    budget_id=budget_id,
                    project_path=project_path,
                    task_description=task.description or "",
                    domain=domain,
                    stage_cfg=stage_config.config or {},
                )
            )
            if gate_result.get("passed"):
                advance_stage(task_id, "pass")
                exit_reason = "pass"
            else:
                advance_stage(task_id, "fail")
                exit_reason = "fail"
    except Exception:
        logger.exception("[planning_gate_node] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Intake decomposition executors (Operation Fury Phase 7)
# ---------------------------------------------------------------------------

def _intake_write_vote(task_id: str, stage_name: str, vote: dict) -> None:
    from app.database import get_task, update_task
    task = get_task(task_id)
    blob = dict(task.content or {}) if task else {}
    votes_map = dict(blob.get("intake_votes", {}))
    votes_map[stage_name] = vote
    blob["intake_votes"] = votes_map
    update_task(task_id, content=blob)


def _intake_read_vote(task_id: str, stage_name: str) -> dict:
    from app.database import get_task
    task = get_task(task_id)
    blob = task.content or {} if task else {}
    return (blob.get("intake_votes") or {}).get(stage_name, {})


def _loop_cleanup(loop: asyncio.AbstractEventLoop) -> None:
    try:
        loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
    except Exception:
        pass
    try:
        loop.close()
    except Exception:
        pass


def _run_intake_scope_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """intake_scope executor — LLM scope analysis (Stage 1 of 5)."""
    from app.database import create_agent_session, close_agent_session, get_task
    from app.agent._intake_pipeline import run_intake_scope_stage

    task = get_task(task_id)
    if not task or not task.description:
        return

    session_id = create_agent_session(
        task_id=task_id, agent_type="intake_scope",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        vote = loop.run_until_complete(
            run_intake_scope_stage(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project=task.project or None,
                stage_cfg=stage_config.config or {},
            )
        )
        _intake_write_vote(task_id, "scope", vote)
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[intake_scope] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        _loop_cleanup(loop)


def _run_intake_static_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """intake_static executor — deterministic static analysis (Stage 2 of 5)."""
    from app.database import create_agent_session, close_agent_session, get_task
    from app.agent._intake_pipeline import run_intake_static_stage

    task = get_task(task_id)
    if not task or not task.description:
        return

    scope_vote = _intake_read_vote(task_id, "scope")
    session_id = create_agent_session(
        task_id=task_id, agent_type="intake_static",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        vote = loop.run_until_complete(
            run_intake_static_stage(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description,
                scope_vote=scope_vote,
                project=task.project or None,
                project_path=project_path,
            )
        )
        _intake_write_vote(task_id, "static", vote)
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[intake_static] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        _loop_cleanup(loop)


def _run_intake_conflict_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """intake_conflict executor — LLM conflict detection (Stage 3 of 5)."""
    from app.database import create_agent_session, close_agent_session, get_task, get_all_tasks
    from app.agent._intake_pipeline import run_intake_conflict_stage

    task = get_task(task_id)
    if not task or not task.description:
        return

    scope_vote = _intake_read_vote(task_id, "scope")
    all_tasks = [
        {"id": t.id, "title": t.title, "type": t.type,
         "description": (t.description or "")[:300]}
        for t in get_all_tasks()
    ]
    session_id = create_agent_session(
        task_id=task_id, agent_type="intake_conflict",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        vote = loop.run_until_complete(
            run_intake_conflict_stage(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description,
                scope_vote=scope_vote,
                all_tasks=all_tasks,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project=task.project or None,
                stage_cfg=stage_config.config or {},
            )
        )
        _intake_write_vote(task_id, "conflict", vote)
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[intake_conflict] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        _loop_cleanup(loop)


def _run_intake_feasibility_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """intake_feasibility executor — LLM feasibility analysis (Stage 4 of 5)."""
    from app.database import create_agent_session, close_agent_session, get_task
    from app.agent._intake_pipeline import run_intake_feasibility_stage

    task = get_task(task_id)
    if not task or not task.description:
        return

    scope_vote = _intake_read_vote(task_id, "scope")
    static_vote = _intake_read_vote(task_id, "static")
    session_id = create_agent_session(
        task_id=task_id, agent_type="intake_feasibility",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        vote = loop.run_until_complete(
            run_intake_feasibility_stage(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description,
                scope_vote=scope_vote,
                static_vote=static_vote,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project=task.project or None,
                stage_cfg=stage_config.config or {},
            )
        )
        _intake_write_vote(task_id, "feasibility", vote)
        advance_stage(task_id, "pass")
        exit_reason = "pass"
    except Exception:
        logger.exception("[intake_feasibility] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        _loop_cleanup(loop)


def _run_intake_gate_node(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """intake_gate executor — tally all votes + advance/fail (Stage 5 of 5)."""
    from app.database import (
        create_agent_session, close_agent_session, get_task, get_all_tasks,
        create_transition_vote, create_transition_result,
        get_transition_results as _gtr,
    )
    from app.agent._intake_pipeline import run_intake_gate
    from app.agent.pipeline_router import advance_stage

    task = get_task(task_id)
    if not task or not task.description:
        return

    blob = task.content or {}
    votes_map = blob.get("intake_votes") or {}
    votes = [v for v in [
        votes_map.get("scope"),
        votes_map.get("static"),
        votes_map.get("conflict"),
        votes_map.get("feasibility"),
    ] if v]

    all_tasks = [
        {"id": t.id, "title": t.title, "type": t.type,
         "description": (t.description or "")[:300]}
        for t in get_all_tasks()
    ]
    session_id = create_agent_session(
        task_id=task_id, agent_type="intake_gate",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_intake_gate(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description,
                votes=votes,
                all_tasks=all_tasks,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project=task.project or None,
            )
        )
        create_transition_result(
            task_id=task_id,
            transition="idea_to_planning",
            outcome=result["outcome"],
            vote_summary=result,
            total_prompt_tokens=result.get("total_prompt_tokens", 0),
            total_completion_tokens=result.get("total_completion_tokens", 0),
        )
        for vote in result.get("votes", []):
            create_transition_vote(
                task_id=task_id,
                transition="idea_to_planning",
                stage=vote["stage"],
                verdict=vote["verdict"],
                confidence=vote.get("confidence", 0),
                justification=vote.get("justification", ""),
                raw_response=vote.get("raw_response"),
                prompt_tokens=vote.get("prompt_tokens", 0),
                completion_tokens=vote.get("completion_tokens", 0),
                model=vote.get("model", ""),
                budget_id=task.budget_id,
            )

        if result["outcome"] == "passed":
            advance_stage(task_id, "pass")
            exit_reason = "pass"
            logger.info("[intake_gate] task '%s' passed → advancing.", task_id)
        elif result["outcome"] == "subdivide":
            from app.main import _handle_subdivision_outcome
            _handle_subdivision_outcome(task, result, llm_base_url, llm_model, max_context, loop)
            exit_reason = "subdivide"
            logger.info("[intake_gate] task '%s' → subdivide.", task_id)
        else:
            all_results = _gtr(task_id, transition="idea_to_planning") or []
            rejection_count = sum(1 for r in all_results if r.outcome in ("rejected", "needs_research"))
            MAX_INTAKE_REJECTIONS = 3
            if rejection_count >= MAX_INTAKE_REJECTIONS:
                from datetime import datetime as _dt
                from app.database import update_task, append_task_history
                update_task(task_id, intake_exhausted_at=_dt.utcnow().isoformat())
                append_task_history(task_id, "intake_exhausted",
                                    message=f"Intake exhausted after {rejection_count} rejections.")
                logger.warning("[intake_gate] task '%s' intake exhausted.", task_id)
            advance_stage(task_id, "fail")
            exit_reason = "fail"
    except Exception:
        logger.exception("[intake_gate] task '%s' raised.", task_id)
        advance_stage(task_id, "fail")
    finally:
        close_agent_session(session_id, exit_reason, "")
        _loop_cleanup(loop)


# ---------------------------------------------------------------------------
# Multiplier Node — fan-out creator
# ---------------------------------------------------------------------------

def _run_multiplier_node(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Fan-out creator for the multiplier_node pipeline stage.

    Creates N _fan_out_child tasks plus one _fan_out_collapser task that
    aggregates their outputs and advances the parent stage.  The collapser ID
    is appended to the parent's prerequisites so the parent blocks until
    aggregation completes.  Does NOT call advance_stage — the collapser drives
    the parent forward.

    Stage config shape:
        agents              — list of {name, system_prompt, tools?, max_turns?}
                              (takes precedence over scalar config)
        n                   — number of child agents in scalar mode (default 3)
        agent_system_prompt — shared system prompt (scalar mode)
        agent_tools         — tool allowlist for children (scalar mode)
        agent_max_turns     — max turns per child (default 15)
        collapser_mode      — "vote_tally" (default) | "judge_select"
        tally_strategy      — "majority" (default) | "veto"
        on_tie              — "reject" (default) | "pass"
        judge_system_prompt — system prompt for the LLM judge (judge_select mode)
        judge_max_turns     — max turns for judge (default 10)
        required_input_keys — task.content keys to inject into child user messages
        output_key          — task.content key for final result (default "fan_out_result")
    """
    from app.database import get_task, update_task, create_task as _create_task

    cfg: dict = stage_config.config or {}
    agents_cfg: list[dict] = list(cfg.get("agents") or [])
    n: int = int(cfg.get("n", 3))
    agent_system_prompt: str = cfg.get("agent_system_prompt", "Complete the task and call submit_work.")
    agent_tools: list[str] = list(cfg.get("agent_tools") or [])
    agent_max_turns: int = int(cfg.get("agent_max_turns", 15))
    required_input_keys: list[str] = cfg.get("required_input_keys") or []
    if isinstance(required_input_keys, str):
        required_input_keys = [k.strip() for k in required_input_keys.split(",") if k.strip()]
    output_key: str = cfg.get("output_key", "fan_out_result")
    collapser_mode: str = cfg.get("collapser_mode", "vote_tally")
    tally_strategy: str = cfg.get("tally_strategy", "majority")
    on_tie: str = cfg.get("on_tie", "reject")
    judge_system_prompt: str = cfg.get("judge_system_prompt", "Compare the proposals and select the best one.")
    judge_max_turns: int = int(cfg.get("judge_max_turns", 10))
    min_improvement_pct: float = float(cfg.get("min_improvement_pct", 0.0))

    parent = get_task(task_id)
    if not parent:
        return

    # Idempotency guard: skip if children already created.
    if (parent.content or {}).get("_multiplier_child_ids"):
        return

    # Build agent config list (per-agent mode takes precedence).
    if not agents_cfg:
        agents_cfg = [
            {"name": f"agent_{i}", "system_prompt": agent_system_prompt,
             "tools": agent_tools, "max_turns": agent_max_turns}
            for i in range(n)
        ]

    # Allow task.content["best_of_n"] to trim the agent list at runtime (set by planning_survey).
    best_of_n = (parent.content or {}).get("best_of_n")
    if best_of_n is not None:
        try:
            agents_cfg = agents_cfg[:int(best_of_n)]
        except (ValueError, TypeError):
            pass

    context_preamble = _build_required_keys_preamble(task_id, required_input_keys)

    child_ids: list[str] = []
    for i, agent in enumerate(agents_cfg):
        name = agent.get("name", f"agent_{i}")
        child = _create_task(
            title=f"[MUL] {name} ← {parent.title[:45]}",
            task_type="_fan_out_child",
            stage_key="_fan_out_child",
            project_id=parent.project_id,
            pipeline_template_id=None,
            llm_id=llm_id,
            budget_id=budget_id,
            content={"_fan_out_cfg": {
                "name": name,
                "system_prompt": agent.get("system_prompt", agent_system_prompt),
                "tools": list(agent.get("tools") or agent_tools),
                "max_turns": int(agent.get("max_turns") or agent_max_turns),
                "collapser_mode": collapser_mode,
                "parent_task_id": task_id,
                "context_preamble": context_preamble,
            }},
        )
        if child:
            child_ids.append(child.id)

    collapser = _create_task(
        title=f"[MUL-join] {parent.title[:50]}",
        task_type="_fan_out_collapser",
        stage_key="_fan_out_collapser",
        project_id=parent.project_id,
        pipeline_template_id=None,
        llm_id=llm_id,
        budget_id=budget_id,
        prerequisites=child_ids,
        content={"_collapser_cfg": {
            "parent_task_id": task_id,
            "parent_stage_key": stage_config.stage_key,
            "child_ids": child_ids,
            "collapser_mode": collapser_mode,
            "tally_strategy": tally_strategy,
            "on_tie": on_tie,
            "judge_system_prompt": judge_system_prompt,
            "judge_max_turns": judge_max_turns,
            "min_improvement_pct": min_improvement_pct,
            "output_key": output_key,
        }},
    )

    blob = dict(parent.content or {})
    blob["_multiplier_child_ids"] = child_ids
    blob["_multiplier_collapser_id"] = collapser.id if collapser else None
    existing_prereqs = list(parent.prerequisites or [])
    update_task(task_id, content=blob,
                prerequisites=existing_prereqs + ([collapser.id] if collapser else []))

    logger.info(
        "[multiplier_node] task '%s': created %d children + collapser '%s'.",
        task_id, len(child_ids), (collapser.id if collapser else "None"),
    )


# ---------------------------------------------------------------------------
# _fan_out_child — runs one fan-out child agent
# ---------------------------------------------------------------------------

def _run_fan_out_child(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Runs one fan-out child created by multiplier_node.

    Config comes from task.content._fan_out_cfg (injected at creation time).
    Writes the submit_work payload to task.content["submission"] and sets the
    task to completed.
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_fan_out_cfg", {})
    name: str = cfg.get("name", "agent")
    system_prompt: str = cfg.get("system_prompt", "Complete the task and call submit_work.")
    tools: list[str] = list(cfg.get("tools") or [])
    max_turns: int = int(cfg.get("max_turns", 15))
    collapser_mode: str = cfg.get("collapser_mode", "vote_tally")
    context_preamble: str = cfg.get("context_preamble", "")
    parent_task_id: str | None = cfg.get("parent_task_id")

    parent = get_task(parent_task_id) if parent_task_id else None

    if collapser_mode == "vote_tally":
        user_msg = (
            context_preamble
            + f"Task ID: {parent_task_id or task_id}\n"
            f"Title: {parent.title if parent else task.title}\n"
            f"Description:\n{(parent.description or '') if parent else (task.description or '')}\n\n"
            "Review and vote. Call submit_work with:\n"
            "  signal='ACCEPTED' or 'REJECTED'\n"
            "  summary='your reasoning'\n"
            "  payload={'verdict': 'ACCEPTED'|'REJECTED', 'confidence': 0-100, 'justification': '...'}"
        )
    else:
        user_msg = (
            context_preamble
            + f"Task ID: {parent_task_id or task_id}\n"
            f"Title: {parent.title if parent else task.title}\n"
            f"Description:\n{(parent.description or '') if parent else (task.description or '')}\n\n"
            "Produce your best proposal and call submit_work with:\n"
            "  signal='ACCEPTED'\n"
            "  summary='brief description'\n"
            "  payload={your full proposal as a dict or string}"
        )

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"_fan_out_child:{name}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        collector = _CollectorAgent(
            task_id=task_id,
            system_prompt=system_prompt,
            tool_allowlist=tools,
            max_turns=max_turns,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            user_message=user_msg,
            agent_name=f"fan_out_child:{name}",
        )
        payload: dict | None = loop.run_until_complete(collector.run())

        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["submission"] = payload or {}
        update_task(task_id, content=blob, type="completed", stage_key="completed")
        exit_reason = "completed"

    except Exception:
        logger.exception("[fan_out_child] task '%s' agent '%s' raised.", task_id, name)
        fresh = get_task(task_id)
        blob = dict((fresh.content or {}) if fresh else {})
        blob["submission"] = {"verdict": "REJECTED", "confidence": 0, "justification": "Agent errored."}
        update_task(task_id, content=blob, type="completed", stage_key="completed")
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# _fan_out_collapser — aggregates child outputs and advances parent
# ---------------------------------------------------------------------------

def _run_fan_out_collapser(
    task_id: str,
    stage_config: "StageConfig",
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
) -> None:
    """
    Aggregator for multiplier_node.  Reads all child submissions and either:
      vote_tally  — tallies ACCEPTED/REJECTED votes via tally_votes()
      judge_select — runs an LLM judge to pick the best proposal

    Calls advance_stage on the parent task when done.
    Config comes from task.content._collapser_cfg (injected by multiplier_node).
    """
    from app.database import get_task, update_task, create_agent_session, close_agent_session
    from app.database.session import SessionLocal
    from app.database.models import TransitionVote
    from app.agent.verdicts import Vote, Verdict, tally_votes

    task = get_task(task_id)
    if not task:
        return

    cfg: dict = (task.content or {}).get("_collapser_cfg", {})
    parent_task_id: str | None = cfg.get("parent_task_id")
    parent_stage_key: str = cfg.get("parent_stage_key", "")
    child_ids: list[str] = cfg.get("child_ids", [])
    collapser_mode: str = cfg.get("collapser_mode", "vote_tally")
    tally_strategy: str = cfg.get("tally_strategy", "majority")
    on_tie: str = cfg.get("on_tie", "reject")
    judge_system_prompt: str = cfg.get("judge_system_prompt", "Compare the proposals and select the best one.")
    judge_max_turns: int = int(cfg.get("judge_max_turns", 10))
    output_key: str = cfg.get("output_key", "fan_out_result")
    min_improvement_pct: float = float(cfg.get("min_improvement_pct", 0.0))

    if not parent_task_id:
        logger.error("[fan_out_collapser] task '%s': no parent_task_id in _collapser_cfg.", task_id)
        return

    session_id = create_agent_session(
        task_id=task_id,
        agent_type="fan_out_collapser",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    try:
        children = [get_task(cid) for cid in child_ids]
        submissions: list[dict] = [
            (c.content or {}).get("submission", {}) for c in children if c
        ]

        parent_task = get_task(parent_task_id)
        parent_title = parent_task.title if parent_task else "(unknown)"

        if collapser_mode == "judge_select":
            proposals = [s for s in submissions if s]
            if not proposals:
                logger.warning("[fan_out_collapser] task '%s': no proposals — advancing fail.", task_id)
                advance_stage(parent_task_id, "fail", from_stage=parent_stage_key)
                exit_reason = "fail"
                update_task(task_id, type="completed", stage_key="completed")
                return

            _MAX_CHARS = 2000
            proposals_text = "\n\n".join(
                f"=== Proposal {i} ===\n{str(p)[:_MAX_CHARS]}"
                for i, p in enumerate(proposals)
            )
            judge_user_msg = (
                f"Task: {parent_title}\n\n"
                f"You have {len(proposals)} proposal(s):\n\n{proposals_text}\n\n"
                "Pick the best one. Call submit_work with:\n"
                "  signal='ACCEPTED'\n"
                "  summary='your rationale'\n"
                "  payload={'selected_index': N, 'rationale': '...'}"
            )
            judge = _CollectorAgent(
                task_id=task_id,
                system_prompt=judge_system_prompt,
                tool_allowlist=[],
                max_turns=judge_max_turns,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                max_context=max_context,
                user_message=judge_user_msg,
                agent_name="multiplier_judge",
            )
            judgment: dict | None = loop.run_until_complete(judge.run())

            selected_idx = 0
            if judgment:
                try:
                    selected_idx = int(judgment.get("selected_index", 0))
                    selected_idx = max(0, min(selected_idx, len(proposals) - 1))
                except (TypeError, ValueError):
                    selected_idx = 0

            winning = proposals[selected_idx]
            parent_fresh = get_task(parent_task_id)
            blob = dict((parent_fresh.content or {}) if parent_fresh else {})
            blob[output_key] = winning
            if judgment:
                blob[f"{output_key}_rationale"] = judgment.get("rationale", "")
            update_task(parent_task_id, content=blob)

            if min_improvement_pct > 0.0:
                best_sub = (winning.get("proposals") or [{}])[0]
                estimated_pct = float(best_sub.get("estimated_improvement_pct", 0))
                if estimated_pct < min_improvement_pct:
                    logger.info(
                        "[fan_out_collapser] task '%s': winning proposal %.1f%% < %.1f%% min — skip.",
                        task_id, estimated_pct, min_improvement_pct,
                    )
                    advance_stage(parent_task_id, "skip", from_stage=parent_stage_key)
                    exit_reason = "skip"
                    update_task(task_id, type="completed", stage_key="completed")
                    return

            advance_stage(parent_task_id, "pass", from_stage=parent_stage_key)
            exit_reason = "pass"

        else:
            votes: list[Vote] = []
            for i, sub in enumerate(submissions):
                verdict_str = str(sub.get("verdict", "REJECTED")).upper()
                raw_conf = int(sub.get("confidence", 50))
                justification = str(sub.get("justification", ""))

                if verdict_str == "ACCEPTED":
                    confidence = max(92, min(100, raw_conf if raw_conf >= 76 else 92))
                    verdict = Verdict.LIKELY
                else:
                    confidence = max(0, min(50, raw_conf if raw_conf <= 50 else 25))
                    verdict = Verdict.REJECTED

                try:
                    votes.append(Vote(
                        stage=f"fan_out_voter_{i}",
                        verdict=verdict,
                        confidence=confidence,
                        justification=justification,
                    ))
                except Exception:
                    logger.warning("[fan_out_collapser] task '%s': child_%d produced invalid vote.", task_id, i)

            if not votes:
                condition = "fail"
                tally_outcome = "rejected"
            elif tally_strategy == "veto":
                condition = _tally_veto(votes)
                tally_outcome = "rejected" if condition == "fail" else "passed"
            else:
                tally = tally_votes(votes)
                tally_outcome = tally.outcome
                if tally_outcome in ("passed", "conditional_pass", "warned"):
                    condition = "pass"
                elif tally_outcome == "tie":
                    condition = "fail" if on_tie == "reject" else "pass"
                else:
                    condition = "fail"

            parent_fresh = get_task(parent_task_id)
            blob = dict((parent_fresh.content or {}) if parent_fresh else {})
            blob[output_key] = {
                "outcome": tally_outcome,
                "votes": [
                    {
                        "stage": v.stage,
                        "verdict": v.verdict.value,
                        "confidence": v.confidence,
                        "justification": v.justification,
                    }
                    for v in votes
                ],
            }
            update_task(parent_task_id, content=blob)

            db = SessionLocal()
            try:
                for v in votes:
                    db.add(TransitionVote(
                        task_id=parent_task_id,
                        transition=f"{parent_stage_key}_multiplier",
                        stage=v.stage,
                        verdict=v.verdict.value,
                        confidence=v.confidence,
                        justification=v.justification,
                        budget_id=budget_id,
                    ))
                db.commit()
            except Exception:
                db.rollback()
                logger.exception("[fan_out_collapser] task '%s': failed to save TransitionVote rows.", task_id)
            finally:
                db.close()

            advance_stage(parent_task_id, condition, from_stage=parent_stage_key)
            exit_reason = condition

        update_task(task_id, type="completed", stage_key="completed")
        logger.info(
            "[fan_out_collapser] task '%s': aggregated %d children → parent '%s' result '%s'.",
            task_id, len(submissions), parent_task_id, exit_reason,
        )

    except Exception:
        logger.exception("[fan_out_collapser] task '%s' raised.", task_id)
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=5.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
