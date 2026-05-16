"""
card_factory — Phase 9 Card Factory System.

Public API
----------
run_factory(factory_stage_id, project_id, trigger_type, trigger_card_id=None)
    Execute the factory for the given pipeline stage in the given project.
    Creates a factory_runs audit row, runs mechanical or LLM-segmented card
    creation, updates the audit row, and returns the FactoryRun.

    Called by:
    - scheduler.py _run_factory_node()   (for all three trigger types)
    - main.py  trigger-factory endpoint  (manual trigger from API)

check_predecessor_triggers(tick_project_ids)
    Scan recently completed tasks and fire any factory stages whose
    predecessor_complete trigger is due.  Called once per scheduler tick.

check_cron_triggers(tick_project_ids)
    Scan factory stages with cron triggers and fire any that are due.
    Called once per scheduler tick.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _resolve_project_id(project_name: str) -> int | None:
    from app.database import get_project
    p = get_project(project_name)
    return p.id if p else None


def _get_stage(stage_id: int):
    from app.database import get_stage_by_id
    return get_stage_by_id(stage_id)


def _interpolate(template: str, item: dict) -> str:
    """Format `template` using `item` as keyword args; leave missing keys as-is."""
    try:
        return template.format_map(_DefaultDict(item))
    except Exception:
        return template


class _DefaultDict(dict):
    def __missing__(self, key):
        return f"{{{key}}}"


# ---------------------------------------------------------------------------
# Mechanical card creation
# ---------------------------------------------------------------------------

def _run_mechanical(
    *,
    factory_stage,
    project_name: str,
    llm_id: int | None,
    budget_id: int | None,
    trigger_card_id: str | None,
) -> int:
    """Create one card per data source item using template interpolation.

    Returns the number of cards created.
    """
    from app.agent.factory_sources import build_adapter
    from app.database import create_task, update_task

    cfg: dict = factory_stage.config or {}
    source_type: str = cfg.get("factory_source_type", "")
    source_config: dict = cfg.get("factory_source_config") or {}
    template: dict = cfg.get("factory_card_template") or {}
    entry_stage: str = cfg.get("factory_entry_stage") or "idea"
    title_tpl: str = template.get("title_template") or "New card: {filename}"
    desc_tpl: str = template.get("description_template") or ""

    adapter = build_adapter(source_type, source_config)
    cards_created = 0

    for i, item in enumerate(adapter.items()):
        title = _interpolate(title_tpl, item)
        description = _interpolate(desc_tpl, item)
        t = create_task(
            title=title,
            task_type=entry_stage,
            description=description,
            owner="system",
            llm_id=llm_id,
            budget_id=budget_id,
            project=project_name,
            stage_key=entry_stage,
            position=i,
        )
        if t:
            if trigger_card_id:
                update_task(t.id, parent_task_id=trigger_card_id)
            cards_created += 1

    return cards_created


# ---------------------------------------------------------------------------
# LLM-segmented card creation (CardFactoryAgent)
# ---------------------------------------------------------------------------

def _run_llm_segmented(
    *,
    factory_stage,
    project_name: str,
    llm_id: int | None,
    budget_id: int | None,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    trigger_card_id: str | None,
) -> int:
    """Dispatch CardFactoryAgent to perform LLM-based segmentation.

    The agent receives the source data and calls batch_create_cards itself.
    Returns the number of cards created.
    """
    from app.agent.factory_sources import build_adapter
    from app.agent.tools import _task_id_ctx, TOOL_REGISTRY, dispatch_tool, build_tool_schemas
    from app.agent.llm_client import call_llm, new_session_id
    from app.database import create_agent_session, close_agent_session

    cfg: dict = factory_stage.config or {}
    source_type: str = cfg.get("factory_source_type", "")
    source_config: dict = cfg.get("factory_source_config") or {}
    intent: str = cfg.get("intent") or "Segment the source data into cards."
    system_prompt_extra: str = cfg.get("system_prompt") or ""
    entry_stage: str = cfg.get("factory_entry_stage") or "idea"

    # Collect source data as a single text blob (capped at ~40 KiB for context safety)
    adapter = build_adapter(source_type, source_config)
    items = list(adapter.items())
    import json
    source_blob = json.dumps(items, ensure_ascii=False)
    if len(source_blob) > 40_000:
        source_blob = source_blob[:40_000] + "\n... (truncated)"

    system_prompt = (
        "You are CardFactoryAgent. Your task: analyze the provided source data "
        "and decide how to split it into individual work cards. "
        f"Entry stage for created cards: {entry_stage!r}. "
        f"Intent: {intent}"
    )
    if system_prompt_extra:
        system_prompt += f"\n\n{system_prompt_extra}"

    user_message = (
        f"Source data ({source_type}):\n\n{source_blob}\n\n"
        "Call batch_create_cards with the cards you decide to create. "
        "Then call submit_work with a brief summary of what you created."
    )

    allowed_tools = ["batch_create_cards", "submit_work"]
    tool_schemas = build_tool_schemas(allowed_tools)

    if not llm_id or not budget_id:
        logger.warning("[card_factory] Missing llm_id or budget_id for LLM-segmented factory")
        return 0

    # Run with the trigger card as the active task context so batch_create_cards
    # can inherit the project, llm_id, and budget_id.
    active_task_id = trigger_card_id or f"factory_{factory_stage.id}"
    token = _task_id_ctx.set(active_task_id)
    session_id = new_session_id()
    db_session_id = create_agent_session(
        task_id=active_task_id,
        agent_type="card_factory_agent",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="factory_llm",
    )

    cards_before = 0
    try:
        from app.database import get_child_tasks
        cards_before = len(get_child_tasks(active_task_id))
    except Exception:
        pass

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_message},
    ]

    try:
        for _ in range(20):  # max turns
            response = call_llm(
                messages=messages,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                max_context=max_context,
                llm_id=llm_id,
                budget_id=budget_id,
                task_id=active_task_id,
                session_id=session_id,
                agent_name="CardFactoryAgent",
                tool_schemas=tool_schemas,
            )
            content = response.get("content") or ""
            tool_calls = response.get("tool_calls") or []

            if not tool_calls:
                break

            messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
            done = False
            for tc in tool_calls:
                fn = tc.get("function", {})
                raw_args = fn.get("arguments", "{}")
                try:
                    arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                except Exception:
                    arguments = {}
                result = dispatch_tool(fn.get("name", ""), arguments)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.get("id", ""),
                    "content": str(result),
                })
                if fn.get("name") == "submit_work":
                    done = True
            if done:
                break
    except Exception:
        logger.exception("[card_factory] LLM-segmented factory error (stage %d)", factory_stage.id)
    finally:
        _task_id_ctx.reset(token)
        close_agent_session(db_session_id)

    try:
        from app.database import get_child_tasks
        cards_after = len(get_child_tasks(active_task_id))
        return max(0, cards_after - cards_before)
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Main dispatch entry point
# ---------------------------------------------------------------------------

def run_factory(
    factory_stage_id: int,
    project_id: int,
    trigger_type: str,
    *,
    trigger_card_id: str | None = None,
    llm_base_url: str = "http://localhost:8008/v1",
    llm_model: str = "local",
    max_context: int | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> "FactoryRun":
    """Execute a factory run and return the completed FactoryRun audit row."""
    from app.database import (
        create_factory_run, update_factory_run, get_factory_run,
    )
    from app.database import get_stage_by_id

    stage = get_stage_by_id(factory_stage_id)
    if not stage:
        raise ValueError(f"factory_stage_id={factory_stage_id} not found")

    cfg: dict = stage.config or {}
    segmentation_mode: str = cfg.get("factory_segmentation_mode", "mechanical")

    # Resolve project by id
    from app.database.session import SessionLocal
    from app.database.models import Project
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
    finally:
        db.close()
    project_name = project.name if project else "TheMaestro"

    # Resolve llm_id / budget_id if not supplied
    if llm_id is None and project and project.llm_id:
        llm_id = project.llm_id
    if budget_id is None and project and project.budget_id:
        budget_id = project.budget_id

    run = create_factory_run(
        factory_stage_id=factory_stage_id,
        project_id=project_id,
        trigger_type=trigger_type,
        trigger_card_id=trigger_card_id,
    )
    run_id = run.id if run else None

    cards_created = 0
    status = "failed"
    try:
        if segmentation_mode == "llm":
            cards_created = _run_llm_segmented(
                factory_stage=stage,
                project_name=project_name,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                max_context=max_context,
                trigger_card_id=trigger_card_id,
            )
        else:
            cards_created = _run_mechanical(
                factory_stage=stage,
                project_name=project_name,
                llm_id=llm_id,
                budget_id=budget_id,
                trigger_card_id=trigger_card_id,
            )
        status = "completed"
        logger.info(
            "[card_factory] Run %s completed: stage=%d trigger=%s cards=%d",
            run_id, factory_stage_id, trigger_type, cards_created,
        )
    except Exception:
        logger.exception(
            "[card_factory] Run %s failed: stage=%d trigger=%s",
            run_id, factory_stage_id, trigger_type,
        )

    if run_id:
        update_factory_run(run_id, status=status, cards_created=cards_created)
        return get_factory_run(run_id)
    return run


# ---------------------------------------------------------------------------
# Scheduler tick helpers
# ---------------------------------------------------------------------------

def _get_factory_stages_with_trigger(trigger_type: str):
    """Return all pipeline_stages with agent_type='factory_node' that include trigger_type."""
    from app.database.session import SessionLocal
    from app.database.models import PipelineStage
    db = SessionLocal()
    try:
        stages = (
            db.query(PipelineStage)
            .filter(PipelineStage.agent_type == "factory_node")
            .all()
        )
        result = []
        for s in stages:
            cfg = s.config or {}
            triggers = cfg.get("factory_trigger") or []
            if trigger_type in triggers:
                result.append(s)
        return result
    finally:
        db.close()


def _get_project_id_for_stage(stage) -> int | None:
    """Resolve project_id from the stage's pipeline template → project association."""
    from app.database.session import SessionLocal
    from app.database.models import Project
    db = SessionLocal()
    try:
        project = (
            db.query(Project)
            .filter(Project.pipeline_template_id == stage.template_id)
            .first()
        )
        return project.id if project else None
    finally:
        db.close()


def check_predecessor_triggers(
    *,
    llm_base_url: str = "http://localhost:8008/v1",
    llm_model: str = "local",
    max_context: int | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> None:
    """Check all factory stages for predecessor_complete triggers.

    Scans active tasks in the project for cards whose task.history contains a
    stage entry matching a predecessor stage key.  Uses predecessor_already_triggered
    as the idempotency guard so each (factory_stage, card) pair fires at most once.

    We scan history rather than the card's current stage_key because by the time
    the scheduler tick runs, the card may have already advanced past the predecessor.
    """
    from app.database import predecessor_already_triggered
    from app.database.session import SessionLocal
    from app.database.models import Task, PipelineTransition, PipelineStage

    factory_stages = _get_factory_stages_with_trigger("predecessor_complete")
    if not factory_stages:
        return

    db = SessionLocal()
    try:
        for factory_stage in factory_stages:
            # Predecessor stage keys: stages with a transition edge TO this factory stage
            incoming = (
                db.query(PipelineTransition)
                .filter(PipelineTransition.to_stage_id == factory_stage.id)
                .all()
            )
            predecessor_stage_ids = {t.from_stage_id for t in incoming}
            if not predecessor_stage_ids:
                continue

            pred_stages = (
                db.query(PipelineStage)
                .filter(PipelineStage.id.in_(predecessor_stage_ids))
                .all()
            )
            pred_keys = {s.stage_key for s in pred_stages}
            if not pred_keys:
                continue

            project_id = _get_project_id_for_stage(factory_stage)
            if project_id is None:
                continue

            # Scan all active tasks in this project for history entries from predecessor stages.
            tasks = (
                db.query(Task)
                .filter(Task.project_id == project_id, Task.is_active == True)
                .all()
            )

            for task in tasks:
                history = task.history or []
                was_in_pred = any(
                    (entry.get("status") or entry.get("type") or "") in pred_keys
                    for entry in history
                    if isinstance(entry, dict)
                )
                if not was_in_pred:
                    continue
                if predecessor_already_triggered(factory_stage.id, task.id):
                    continue

                logger.info(
                    "[card_factory] Predecessor trigger: stage=%d card=%s",
                    factory_stage.id, task.id,
                )
                stage_id = factory_stage.id
                card_id = task.id
                threading.Thread(
                    target=_fire_factory_thread,
                    args=(stage_id, project_id, "predecessor_complete", card_id,
                          llm_base_url, llm_model, max_context, llm_id, budget_id),
                    daemon=True,
                    name=f"factory-pred-{stage_id}-{card_id}",
                ).start()
    finally:
        db.close()



def _cron_is_due(cron_schedule: str, last_run_at: datetime | None) -> bool:
    """Return True if the cron schedule is due given the last run time.

    Supports simple cron expressions (minute/hour/day/month/weekday).
    Falls back to croniter if available; otherwise uses a minimal built-in check.
    """
    now = datetime.now(timezone.utc)
    try:
        from croniter import croniter  # type: ignore
        if last_run_at is None:
            last_run_at = datetime(2000, 1, 1, tzinfo=timezone.utc)
        cron = croniter(cron_schedule, last_run_at.replace(tzinfo=None))
        next_run = cron.get_next(datetime)
        return next_run.replace(tzinfo=timezone.utc) <= now
    except ImportError:
        pass

    # Minimal built-in: parse 5-field cron; only checks minute and hour
    try:
        parts = cron_schedule.strip().split()
        if len(parts) != 5:
            return False
        minute_field, hour_field = parts[0], parts[1]
        cur_minute, cur_hour = now.minute, now.hour

        def _matches(field: str, value: int) -> bool:
            if field == "*":
                return True
            if field.isdigit():
                return int(field) == value
            if "," in field:
                return value in [int(x) for x in field.split(",")]
            if "/" in field:
                step = int(field.split("/")[1])
                return value % step == 0
            return False

        minute_ok = _matches(minute_field, cur_minute)
        hour_ok = _matches(hour_field, cur_hour)
        if not (minute_ok and hour_ok):
            return False
        # Avoid re-firing within the same minute
        if last_run_at is not None:
            last_local = last_run_at.replace(tzinfo=timezone.utc) if last_run_at.tzinfo is None else last_run_at
            delta = (now - last_local).total_seconds()
            if delta < 55:
                return False
        return True
    except Exception:
        return False


def check_cron_triggers(
    *,
    llm_base_url: str = "http://localhost:8008/v1",
    llm_model: str = "local",
    max_context: int | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> None:
    """Fire any cron-scheduled factory stages that are due."""
    from app.database import get_last_cron_run_at

    factory_stages = _get_factory_stages_with_trigger("cron")
    for fs in factory_stages:
        cfg = fs.config or {}
        source_cfg = cfg.get("factory_source_config") or {}
        cron_schedule = source_cfg.get("cron_schedule") or cfg.get("cron_schedule") or ""
        if not cron_schedule:
            continue
        last_run = get_last_cron_run_at(fs.id)
        if not _cron_is_due(cron_schedule, last_run):
            continue
        project_id = _get_project_id_for_stage(fs)
        if project_id is None:
            continue
        logger.info("[card_factory] Cron trigger: stage=%d schedule=%r", fs.id, cron_schedule)
        stage_id = fs.id
        threading.Thread(
            target=_fire_factory_thread,
            args=(stage_id, project_id, "cron", None,
                  llm_base_url, llm_model, max_context, llm_id, budget_id),
            daemon=True,
            name=f"factory-cron-{stage_id}",
        ).start()


def _fire_factory_thread(
    stage_id: int,
    project_id: int,
    trigger_type: str,
    trigger_card_id: str | None,
    llm_base_url: str,
    llm_model: str,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
) -> None:
    try:
        run_factory(
            stage_id,
            project_id,
            trigger_type,
            trigger_card_id=trigger_card_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
        )
    except Exception:
        logger.exception("[card_factory] _fire_factory_thread failed: stage=%d", stage_id)
