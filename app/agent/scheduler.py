"""
app/agent/scheduler.py
----------------------
Push-first eager task scheduler for the Maestro orchestration system.

Maintains a pool of pending tasks and dispatches them as soon as their
prerequisites are met and LLM capacity is available.  Tasks with unmet
prerequisites sit in an eager queue, re-evaluated every tick.

Capacity is tracked per-LLM: each LLM endpoint declares how many
parallel sessions it can handle.  The scheduler will not exceed that
limit.

Usage::

    from app.agent.scheduler import start_scheduler, stop_scheduler

    # On FastAPI startup:
    start_scheduler()

    # On FastAPI shutdown:
    stop_scheduler()
"""

from __future__ import annotations

import asyncio
import logging
import time
import threading
from collections import defaultdict
from typing import Any

from app.agent.config import (
    SCHEDULER_TICK_INTERVAL,
    SCHEDULER_ENABLED,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------

# task_id -> threading.Thread for currently running loops
_active_sessions: dict[str, threading.Thread] = {}
_active_sessions_lock = threading.Lock()

# Per-LLM active session count: llm_id -> count
_llm_session_counts: dict[int, int] = defaultdict(int)
_llm_counts_lock = threading.Lock()

# task_id -> timestamp of last failed dispatch (cooldown to avoid retry storms)
_failed_cooldowns: dict[str, float] = {}
_FAIL_COOLDOWN_SECONDS = 60.0  # Wait 60s before retrying a failed task

# Background thread that drives the scheduler tick
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """Start the background scheduler thread (if enabled in config)."""
    global _scheduler_thread
    if not SCHEDULER_ENABLED:
        logger.info("Scheduler disabled in config.")
        return
    if _scheduler_thread is not None and _scheduler_thread.is_alive():
        logger.warning("Scheduler already running.")
        return
    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="maestro-scheduler"
    )
    _scheduler_thread.start()
    logger.info("Scheduler started (tick every %.1fs).", SCHEDULER_TICK_INTERVAL)


def stop_scheduler() -> None:
    """Signal the scheduler to stop and wait for it."""
    global _scheduler_thread
    if _scheduler_thread is None:
        return
    _scheduler_stop.set()
    _scheduler_thread.join(timeout=SCHEDULER_TICK_INTERVAL + 2)
    _scheduler_thread = None
    logger.info("Scheduler stopped.")


def get_scheduler_status() -> dict:
    """Return a snapshot of the scheduler's state."""
    with _active_sessions_lock:
        active = {tid: t.is_alive() for tid, t in _active_sessions.items()}
    with _llm_counts_lock:
        llm_counts = dict(_llm_session_counts)
    return {
        "running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "active_sessions": active,
        "llm_session_counts": llm_counts,
        "tick_interval": SCHEDULER_TICK_INTERVAL,
    }


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def _scheduler_loop() -> None:
    """Main scheduler loop — runs in a background thread."""
    logger.info("Scheduler loop started.")
    while not _scheduler_stop.is_set():
        try:
            _tick()
        except Exception:
            logger.exception("Scheduler tick failed.")
        _scheduler_stop.wait(timeout=SCHEDULER_TICK_INTERVAL)
    logger.info("Scheduler loop exiting.")


def _tick() -> None:
    """
    Single scheduler tick:
      1. Clean up finished sessions.
      2. Discover DAG-ready tasks.
      3. For each ready task, check LLM capacity and dispatch if possible.
    """
    # Lazy imports to avoid circular deps at module load
    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_task, get_llm

    # 1. Cleanup finished sessions
    _cleanup_finished()

    # 2. Get all tasks, compute DAG readiness
    all_tasks = get_all_tasks()
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()

    if not ready_tasks:
        return

    # 3. Try to dispatch each ready task
    for task_dict in ready_tasks:
        task_id = task_dict["id"]
        task_type = task_dict.get("type", "")

        # Only auto-dispatch tasks in columns the MaestroLoop handles.
        # IDEA tasks require explicit human "Advance to Planning" action —
        # the scheduler never auto-fires the intake pipeline.
        if task_type not in ("planning", "indev"):
            continue

        # Already running?
        with _active_sessions_lock:
            if task_id in _active_sessions and _active_sessions[task_id].is_alive():
                continue

        # Cooldown after failure — don't retry for 60s
        if task_id in _failed_cooldowns:
            if time.time() - _failed_cooldowns[task_id] < _FAIL_COOLDOWN_SECONDS:
                continue

        # Resolve the LLM for capacity check
        db_task = get_task(task_id)
        if not db_task or not db_task.llm_id:
            continue

        llm = get_llm(db_task.llm_id)
        if not llm:
            continue

        # Check LLM capacity
        with _llm_counts_lock:
            current = _llm_session_counts[llm.id]
            if current >= llm.parallel_sessions:
                logger.debug(
                    "LLM %d at capacity (%d/%d), deferring task '%s'.",
                    llm.id, current, llm.parallel_sessions, task_id,
                )
                continue
            # Reserve a slot
            _llm_session_counts[llm.id] += 1

        # Dispatch
        logger.info(
            "Dispatching task '%s' (type=%s) to LLM %d (%s:%d %s) [slot %d/%d].",
            task_id, task_type, llm.id, llm.address, llm.port, llm.model,
            _llm_session_counts[llm.id], llm.parallel_sessions,
        )

        thread = threading.Thread(
            target=_run_task,
            args=(task_id, task_type, llm, db_task),
            daemon=True,
            name=f"maestro-task-{task_id}",
        )
        with _active_sessions_lock:
            _active_sessions[task_id] = thread
        thread.start()


def _run_task(task_id: str, task_type: str, llm: Any, db_task: Any = None) -> None:
    """
    Execute a single task in its own thread + event loop.
    Releases the LLM session slot when done.
    """
    try:
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        llm_model = llm.model
        max_context = llm.max_context
        llm_id = llm.id
        budget_id = db_task.budget_id if db_task else None

        if task_type == "idea":
            _run_intake(task_id, llm_base_url, llm_model)
        elif task_type == "indev":
            _run_dev_orchestrator_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id)
        else:
            _run_maestro_loop(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id)
    except Exception:
        _failed_cooldowns[task_id] = time.time()
        logger.exception("Task '%s' failed in scheduler dispatch (cooldown %ds).", task_id, int(_FAIL_COOLDOWN_SECONDS))
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)


def _run_intake(task_id: str, llm_base_url: str, llm_model: str) -> None:
    """Run the intake pipeline for an IDEA task."""
    from app.agent.intake import run_intake_pipeline
    from app.database import (
        get_task, get_all_tasks, update_task,
        create_transition_vote, create_transition_result,
    )

    task = get_task(task_id)
    if not task:
        return

    # Require description, llm_id, budget_id before advancing
    if not task.description or not task.llm_id or not task.budget_id:
        logger.debug("Task '%s' missing required fields for intake, skipping.", task_id)
        return

    all_tasks = get_all_tasks()
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_intake_pipeline(
                task_id=task_id,
                task_description=task.description or "",
                task_title=task.title,
                all_tasks=task_dicts,
                budget_id=task.budget_id,
                llm_id=task.llm_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
        )

        # Persist results
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
            update_task(task_id, type="planning")
            logger.info("Task '%s' advanced to PLANNING via scheduler.", task_id)
        else:
            logger.info("Task '%s' intake result: %s", task_id, result["outcome"])
    finally:
        loop.close()


def _run_maestro_loop(task_id: str, llm_base_url: str, llm_model: str,
                      max_context: int | None = None,
                      llm_id: int | None = None,
                      budget_id: int | None = None) -> None:
    """Run the MaestroLoop for a PLANNING/DEVELOPMENT task."""
    from app.agent.loop import MaestroLoop

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        maestro = MaestroLoop(
            task_id=task_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
        )
        loop.run_until_complete(maestro.run())
    finally:
        loop.close()


def _run_dev_orchestrator_task(task_id: str, llm_base_url: str, llm_model: str,
                                max_context: int | None = None,
                                llm_id: int | None = None,
                                budget_id: int | None = None) -> None:
    """Run the DevOrchestrator for an IN DEV task."""
    from app.agent.dev_orchestrator import run_dev_orchestrator
    from app.database import get_planning_result, update_task
    import json

    planning_result_obj = get_planning_result(task_id)
    if not planning_result_obj:
        logger.warning("No planning result for task '%s', skipping.", task_id)
        return

    planning_result = {
        "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
        "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
        "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
        "interface_contracts": json.loads(planning_result_obj.interface_contracts or "[]"),
        "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
    }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_dev_orchestrator(
                task_id=task_id,
                planning_result=planning_result,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
            )
        )
        if result.get("status") == "ACCEPTED":
            update_task(task_id, type="conceptual_review")
            logger.info("Task '%s' advanced to CONCEPTUAL REVIEW via scheduler.", task_id)
        else:
            update_task(task_id, type="planning")
            logger.info("Task '%s' reverted to PLANNING: %s", task_id, result.get("error_detail"))
    finally:
        loop.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _cleanup_finished() -> None:
    """Remove sessions whose threads have completed."""
    with _active_sessions_lock:
        finished = [tid for tid, t in _active_sessions.items() if not t.is_alive()]
        for tid in finished:
            del _active_sessions[tid]


def _task_to_mini_dict(task: Any) -> dict:
    """Minimal dict for DAGResolver — avoids importing task_to_dict."""
    return {
        "id": task.id,
        "type": task.type,
        "position": task.position,
        "prerequisites": task.prerequisites or [],
    }
