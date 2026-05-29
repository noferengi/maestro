"""
pipeline_router — data-driven stage transition and dispatch.

Public API
----------
get_next_stage(task_id, condition) -> str | None
    Look up the DB pipeline graph for the task's current stage_key and return
    the stage_key of the highest-priority outgoing edge matching `condition`,
    or None if no edge fires.

advance_stage(task_id, condition) -> bool
    Resolve the next stage via get_next_stage(), then atomically write both
    task.stage_key and task.type.  Returns True on success, False if no edge.
    Falls back to _LEGACY_TRANSITIONS when no pipeline template is configured
    (backward compat for environments without a seeded template).

get_stage_config(task_id) -> StageConfig | None
    Return pipeline_stages metadata for the task's current stage.

dispatch_task(task_id, stage_key, *, llm_base_url, ...) -> bool
    Dispatch hierarchy:
      1. Stage-key handler (_stage_handlers) — the 8 hardcoded Software Dev stages.
      2. Generic path: read stage config, then:
         a. Return False for no-auto-dispatch agent types (human_gate, terminal).
         b. Agent-type executor (_agent_type_executors) — voting_panel, fan_out_judge, etc.
         c. Universal fallback — GenericStageAgent reads stage config directly.
    Returns False only when no dispatch fires (no stage config, or human_gate).

register_handler(stage_key, fn) -> None
    Register a callable as the dispatch handler for a stage.

register_agent_type_executor(agent_type, fn) -> None
    Register a callable as the executor for an agent_type key.  Called at module
    level by scheduler.py.  Same circular-import avoidance pattern as register_handler.
"""

import logging
from dataclasses import dataclass
from typing import Callable

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler registries — populated by scheduler.py at import time
# ---------------------------------------------------------------------------

_stage_handlers: dict[str, Callable] = {}
_agent_type_executors: dict[str, Callable] = {}

# Agent types that should never be auto-dispatched (require human action or are terminal).
_NO_AUTO_DISPATCH_AGENT_TYPES: frozenset[str] = frozenset({"human_gate", "terminal"})

# behavior_type values that require no auto-dispatch (same semantics, but arrived via a definition).
_NO_AUTO_DISPATCH_BEHAVIOR_TYPES: frozenset[str] = frozenset({"human_gate", "arch_gen"})

# Maps behavior_type → the stage-handler key registered by scheduler.py.
# When dispatch_task resolves a definition with behavior_type in this map, it
# calls _stage_handlers[mapped_key] so the full built-in pipeline runs.
# NOTE: maestro_loop/conceptual_review/optimization/security/final_review were
# removed — those stages were converted to multiplier_node in migrations 0126/0129
# and no longer have registered stage handlers.
_BEHAVIOR_TYPE_TO_STAGE_HANDLER: dict[str, str] = {
    "factory": "factory_node",
}


def register_handler(stage_key: str, fn: Callable) -> None:
    """Register `fn` as the dispatch handler for `stage_key`."""
    _stage_handlers[stage_key] = fn


def register_agent_type_executor(agent_type: str, fn: Callable) -> None:
    """Register `fn` as the executor for `agent_type` in the generic dispatch path."""
    _agent_type_executors[agent_type] = fn


# ---------------------------------------------------------------------------
# Stage config dataclass
# ---------------------------------------------------------------------------

@dataclass
class StageConfig:
    stage_key: str
    label: str
    agent_type: str
    position: int
    config: dict | None
    template_id: int
    stage_id: int


# ---------------------------------------------------------------------------
# Legacy transition fallback — safety net only; unreachable in normal operation
# (tasks without a pipeline_template_id cannot be created by normal flows post
# migration-0077). Retained for data-integrity edge cases only.
# ---------------------------------------------------------------------------

_LEGACY_TRANSITIONS: dict[str, dict[str, str]] = {
    "idea":              {"pass": "planning",          "always": "planning"},
    "planning":          {"pass": "indev",             "fail": "planning",  "subdivide": "idea"},
    "indev":             {"pass": "conceptual_review", "fail": "indev"},
    "conceptual_review": {"pass": "optimization",      "fail": "indev",      "reject": "planning"},
    "optimization":      {"pass": "security",          "fail": "indev"},
    "security":          {"pass": "final_review",      "fail": "indev",      "reject": "optimization"},
    "final_review":      {"pass": "human_review",      "fail": "indev",      "reject": "indev"},
}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_template_id_for_task(task) -> int | None:
    """Return the pipeline_template_id for a task, falling back to project then default."""
    # Prefer the task's own template (authoritative since migration 0077)
    tid = getattr(task, "pipeline_template_id", None)
    if isinstance(tid, int):
        return tid
    # Fallback for tasks pre-dating migration 0077
    if task.project_ref is not None:
        raw = getattr(task.project_ref, "pipeline_template_id", None)
        if isinstance(raw, int):
            return raw
    try:
        from app.database import get_default_template
        tmpl = get_default_template()
        if tmpl:
            return tmpl.id
    except Exception:
        pass
    return None


def _current_stage_key(task) -> str | None:
    """Return the task's current stage key as a string, preferring stage_key over type."""
    sk = task.stage_key
    if isinstance(sk, str) and sk:
        return sk
    tp = task.type
    if isinstance(tp, str) and tp:
        return tp
    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_next_stage(task_id: str, condition: str) -> str | None:
    """
    Resolve the next stage_key for `task_id` given `condition`.

    Reads the task's current stage_key → looks up outgoing pipeline_transitions
    matching `condition` → returns the stage_key of the highest-priority target.
    Returns None if no matching edge exists or if the task has no template.
    """
    from app.database import get_task, get_stage_by_key, get_transitions_for_template
    from app.database.session import SessionLocal
    from app.database.models import PipelineStage

    task = get_task(task_id)
    if not task:
        return None

    current_stage_key = _current_stage_key(task)
    if not current_stage_key:
        return None

    template_id = _get_template_id_for_task(task)
    if template_id is None:
        logger.debug(
            "[pipeline_router] Task %s has no pipeline template — cannot resolve '%s' transition",
            task_id, condition,
        )
        return None

    current_stage = get_stage_by_key(template_id, current_stage_key)
    if not current_stage:
        logger.warning(
            "[pipeline_router] Stage '%s' not found in template %d (task=%s)",
            current_stage_key, template_id, task_id,
        )
        return None

    transitions = get_transitions_for_template(template_id)
    matching = [
        t for t in transitions
        if t.from_stage_id == current_stage.id and t.condition == condition
    ]
    if not matching:
        logger.debug(
            "[pipeline_router] No '%s' edge from '%s' in template %d (task=%s)",
            condition, current_stage_key, template_id, task_id,
        )
        return None

    best = max(matching, key=lambda t: t.priority)

    db = SessionLocal()
    try:
        target = db.query(PipelineStage).filter(PipelineStage.id == best.to_stage_id).first()
        return target.stage_key if target else None
    finally:
        db.close()


def advance_stage(task_id: str, condition: str, *, from_stage: "str | None" = None) -> bool:
    """
    Transition `task_id` to the next stage as determined by the pipeline graph.

    Resolves the target via get_next_stage(), then writes both task.stage_key
    and task.type in a single update_task() call.

    `from_stage` — caller-supplied current stage key.  When provided it is used
    directly in the legacy fallback (avoids a second DB round-trip and ensures
    correctness when update_task is mocked in tests).  When omitted the task is
    re-fetched from the DB for the fallback.

    Falls back to _LEGACY_TRANSITIONS when no pipeline template is configured.

    Returns True if a transition fired, False if no transition matched.
    """
    next_stage = get_next_stage(task_id, condition)

    if next_stage is None:
        # Fallback: use the hardcoded Software Development transition map.
        stage = from_stage
        if stage is None:
            from app.database import get_task
            task = get_task(task_id)
            if task:
                stage = _current_stage_key(task)
        if stage:
            next_stage = _LEGACY_TRANSITIONS.get(stage, {}).get(condition)

    if next_stage is None:
        logger.warning(
            "[pipeline_router] advance_stage('%s', '%s'): no matching edge — stage unchanged",
            task_id, condition,
        )
        return False

    from app.database import update_task, get_task
    update_task(task_id, type=next_stage, stage_key=next_stage)

    # When a card reaches 'completed', auto-trigger goal verification if linked.
    if next_stage == "completed":
        try:
            task = get_task(task_id)
            if task and task.goal_id:
                from app.database import create_goal_verification_job, get_goal
                goal = get_goal(task.goal_id)
                if goal and goal.llm_id and goal.status == "active":
                    from app.database import get_project
                    proj = get_project(goal.project_id)
                    budget_id = proj.budget_id if proj else None
                    create_goal_verification_job(
                        goal.id,
                        triggered_by="card_completion",
                        llm_id=goal.llm_id,
                        budget_id=budget_id,
                    )
                    logger.info(
                        "[pipeline_router] queued goal verification for goal %d (task %s completed)",
                        goal.id, task_id,
                    )
        except Exception:
            logger.debug("[pipeline_router] goal auto-trigger skipped", exc_info=True)

        # When a card with an autopilot objective completes, trigger an autopilot tick.
        try:
            task = get_task(task_id)
            if task and task.autopilot_objective_id and task.project_id:
                proj_name = task.project or f"project-{task.project_id}"
                from app.agent.scheduler import _trigger_autopilot_tick
                _trigger_autopilot_tick(task.project_id, proj_name)
        except Exception:
            logger.debug("[pipeline_router] autopilot tick trigger skipped", exc_info=True)

    return True


def get_stage_config(task_id: str) -> StageConfig | None:
    """
    Return pipeline_stages metadata for the task's current stage_key.
    Returns None if the task has no template or the stage is not found.
    """
    from app.database import get_task, get_stage_by_key

    task = get_task(task_id)
    if not task:
        return None

    stage_key = _current_stage_key(task)
    if not stage_key:
        return None

    template_id = _get_template_id_for_task(task)
    if template_id is None:
        return None

    stage = get_stage_by_key(template_id, stage_key)
    if not stage:
        return None

    return StageConfig(
        stage_key=stage.stage_key,
        label=stage.label,
        agent_type=stage.agent_type,
        position=stage.position,
        config=stage.config,
        template_id=template_id,
        stage_id=stage.id,
    )


def dispatch_task(
    task_id: str,
    stage_key: "str | None" = None,
    *,
    llm_base_url: str,
    llm_model: str,
    max_context: "int | None",
    llm_id: "int | None",
    budget_id: "int | None",
    project_path: "str | None",
) -> bool:
    """
    Dispatch `task_id` using the two-tier hierarchy:

    1. Template-driven path — reads stage config from the task's pipeline template,
       then routes to the registered executor (voting_panel, fan_out_judge, etc.) or
       GenericStageAgent. Takes priority so malleable node configs always win.
    2. Legacy stage-key handler — the hardcoded Software Dev stage functions, only
       reached when no template stage config exists (tasks without a pipeline template).

    Returns False only when nothing fires (no stage, no config, human_gate).
    Returning False causes _run_task to attempt the legacy MaestroLoop fallback.
    """
    if stage_key is None:
        from app.database import get_task
        task = get_task(task_id)
        if not task:
            logger.error("[pipeline_router] dispatch_task: task %s not found", task_id)
            return False
        stage_key = _current_stage_key(task)
        if not stage_key:
            logger.error("[pipeline_router] dispatch_task: task %s has no stage", task_id)
            return False

    # Tier 1: template-driven dispatch — look up stage config
    stage_config = get_stage_config(task_id)
    if not stage_config:
        # No template stage config — fall through to legacy handlers below
        pass
    else:
        agent_type = stage_config.agent_type or ""

        # 1a. No-auto-dispatch agent types
        if agent_type in _NO_AUTO_DISPATCH_AGENT_TYPES:
            logger.debug(
                "[pipeline_router] Stage '%s' agent_type='%s' is not auto-dispatchable (task=%s).",
                stage_key, agent_type, task_id,
            )
            return False

        # 1b. Definition-driven dispatch: agent_type may name a custom_agent_definitions row.
        #     If the definition has a behavior_type, route to the corresponding handler/executor
        #     so that built-in pipeline semantics fire even for user-named stages.
        defn = _load_definition_by_name(agent_type)
        if defn is not None and getattr(defn, "behavior_type", None):
            behavior_type = defn.behavior_type
            if behavior_type in _NO_AUTO_DISPATCH_BEHAVIOR_TYPES:
                logger.debug(
                    "[pipeline_router] Stage '%s' definition behavior_type='%s' is not auto-dispatchable (task=%s).",
                    stage_key, behavior_type, task_id,
                )
                return False
            # Merge behavior_config into stage_config.config (stage wins on conflict)
            merged_config = {**(defn.behavior_config or {}), **(stage_config.config or {})}
            merged_stage = StageConfig(
                stage_key=stage_config.stage_key,
                label=stage_config.label,
                agent_type=stage_config.agent_type,
                position=stage_config.position,
                config=merged_config,
                template_id=stage_config.template_id,
                stage_id=stage_config.stage_id,
            )
            # Try executor registry first (voting_panel, circuit_breaker, fan_out_judge)
            executor = _agent_type_executors.get(behavior_type)
            if executor is not None:
                logger.debug(
                    "[pipeline_router] Definition '%s' behavior_type='%s' -> executor (task=%s).",
                    agent_type, behavior_type, task_id,
                )
                executor(task_id, merged_stage, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
                return True
            # Try stage-handler map (intake_pipeline, planning_pipeline, maestro_loop, …)
            handler_key = _BEHAVIOR_TYPE_TO_STAGE_HANDLER.get(behavior_type)
            if handler_key:
                handler = _stage_handlers.get(handler_key)
                if handler is not None:
                    logger.debug(
                        "[pipeline_router] Definition '%s' behavior_type='%s' -> stage handler '%s' (task=%s).",
                        agent_type, behavior_type, handler_key, task_id,
                    )
                    handler(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
                    return True
            # single_pass_llm and unknown behavior types fall through to GenericStageAgent below

        # 1c. Agent-type-specific executor (registered directly, e.g. custom executor keys)
        executor = _agent_type_executors.get(agent_type)
        if executor is not None:
            executor(task_id, stage_config, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
            return True

        # 1d. Universal fallback: GenericStageAgent
        _run_generic_stage(task_id, stage_config, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        return True

    # Tier 2: legacy stage-key handlers (tasks without a pipeline template stage config)
    handler = _stage_handlers.get(stage_key)
    if handler is not None:
        handler(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        return True

    logger.warning(
        "[pipeline_router] No stage config or legacy handler for '%s' (task=%s) — nothing fired.",
        stage_key, task_id,
    )
    return False


def _load_definition_by_name(name: str):
    """Load a custom_agent_definitions row by name, returning None on miss or error."""
    try:
        from app.database.session import SessionLocal
        from app.database.models import CustomAgentDefinition
        db = SessionLocal()
        try:
            return (
                db.query(CustomAgentDefinition)
                .filter(CustomAgentDefinition.name == name)
                .first()
            )
        finally:
            db.close()
    except Exception:
        return None


def _run_generic_stage(
    task_id: str,
    stage_config: StageConfig,
    llm_base_url: str,
    llm_model: str,
    max_context: "int | None",
    llm_id: "int | None",
    budget_id: "int | None",
    project_path: "str | None",
) -> None:
    """Run GenericStageAgent for a custom pipeline stage with no registered handler."""
    import asyncio
    from app.agent.generic_stage_agent import GenericStageAgent
    from app.database import create_agent_session, close_agent_session

    session_id = create_agent_session(
        task_id=task_id,
        agent_type=f"generic:{stage_config.stage_key}",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    # Register with the scheduler so zombie cleanup doesn't close this session
    # while the thread is still running.  Local import avoids a module-level
    # circular import (scheduler → pipeline_router → scheduler).
    if session_id is not None:
        try:
            from app.agent.scheduler import register_db_session
            register_db_session(task_id, session_id)
        except Exception:
            pass  # non-fatal; zombie cleanup is still a safety net
    if project_path:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path, task_id=task_id)

    # Set _task_project_name so document store tools (list_documents, get_document,
    # store_document) can resolve the project_id from context.  Without this every
    # generic:* agent gets "No project context" from all doc-store tool calls.
    from app.database import get_task as _get_task_for_ctx
    from app.agent.tools import _task_project_name
    _ctx_task = _get_task_for_ctx(task_id)
    if _ctx_task and _ctx_task.project:
        _task_project_name.set(_ctx_task.project)

    exit_reason = "error"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        agent = GenericStageAgent(
            task_id=task_id,
            stage_config=stage_config,
            llm_id=llm_id,
            budget_id=budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
        )
        result = loop.run_until_complete(agent.run())
        exit_reason = result.get("condition", "error") if isinstance(result, dict) else "error"
    except Exception:
        logger.exception(
            "[generic_stage] task '%s' stage '%s' raised.",
            task_id, stage_config.stage_key,
        )
    finally:
        close_agent_session(session_id, exit_reason, "")
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
