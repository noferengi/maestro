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

import json

from app.agent.config import (
    SCHEDULER_TICK_INTERVAL,
    SCHEDULER_ENABLED,
    SCHEDULER_DISPATCHABLE_TYPES,
    RESEARCH_JOB_PRIORITY_DEPTH_PENALTY,
    PIPELINE_COLUMN_ORDER,
    PIPELINE_DONE_STATUSES,
    MAX_TOKENS_PER_TURN,
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

# ---------------------------------------------------------------------------
# General-purpose completion registry
# ---------------------------------------------------------------------------
# Maps an opaque key (e.g. "file_summary:{sha1}:{size}") to a threading.Event.
# Agents block on event.wait(); workers call signal_completion() when done.

_pending_completions: dict[str, threading.Event] = {}
_pending_completions_lock = threading.Lock()


def get_or_create_completion_event(key: str) -> "tuple[threading.Event, bool]":
    """Thread-safe get-or-create for a completion event.

    Returns (event, created) where created=True means this call created it.
    """
    with _pending_completions_lock:
        if key in _pending_completions:
            return _pending_completions[key], False
        ev = threading.Event()
        _pending_completions[key] = ev
        return ev, True


def signal_completion(key: str) -> None:
    """Signal that a job identified by key has completed (success or failure).

    Removes the event from the registry and calls .set() so all waiters wake up.
    """
    with _pending_completions_lock:
        ev = _pending_completions.pop(key, None)
    if ev is not None:
        ev.set()


def wait_for_completion(key: str, timeout: float = 120.0) -> bool:
    """Block until the job identified by key completes or timeout expires.

    Returns True if the job completed (or was already cleaned up from the
    registry — meaning it finished before we started waiting). Returns False
    on timeout.
    """
    with _pending_completions_lock:
        ev = _pending_completions.get(key)
    if ev is None:
        # Key already removed — job completed before we reached this call.
        return True
    return ev.wait(timeout=timeout)


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
    try:
        from app.database import count_pending_research_jobs
        pending_research = count_pending_research_jobs()
    except Exception:
        pending_research = 0
    try:
        from app.database import count_pending_file_summary_jobs
        pending_file_summaries = count_pending_file_summary_jobs()
    except Exception:
        pending_file_summaries = 0
    return {
        "running": _scheduler_thread is not None and _scheduler_thread.is_alive(),
        "active_sessions": active,
        "llm_session_counts": llm_counts,
        "tick_interval": SCHEDULER_TICK_INTERVAL,
        "pending_research_jobs": pending_research,
        "pending_file_summary_jobs": pending_file_summaries,
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


def _estimate_worst_case_microcents(llm_id: int | None, budget_id: int | None) -> int:
    """Upper-bound cost of one full context + one max-token completion, in µ¢."""
    if llm_id is None or budget_id is None:
        return 0
    from app.database import get_llm
    llm = get_llm(llm_id)
    if llm is None:
        return 0
    pp_rate = getattr(llm, 'cost_per_million_prompt_tokens', 0.0) or 0.0
    tg_rate = getattr(llm, 'cost_per_million_completion_tokens', 0.0) or 0.0
    if pp_rate == 0 and tg_rate == 0:
        return 0
    worst_pp = int(llm.max_context * pp_rate * 100)
    worst_tg = int(MAX_TOKENS_PER_TURN * tg_rate * 100)
    return worst_pp + worst_tg


def _compute_dag_depth(task_id: str, by_id: dict) -> int:
    """Longest prerequisite chain depth for a task (0 = no prerequisites)."""
    task = by_id.get(task_id)
    if not task or not task.get("prerequisites"):
        return 0
    return max(
        (_compute_dag_depth(pid, by_id) for pid in task["prerequisites"] if pid in by_id),
        default=0,
    ) + 1


def _compute_priority(task_dict: dict, by_id: dict) -> float:
    """Lower score = higher priority. Shallower DAG depth first."""
    depth = _compute_dag_depth(task_dict["id"], by_id)
    try:
        col_idx = PIPELINE_COLUMN_ORDER.index(task_dict.get("type", ""))
    except ValueError:
        col_idx = len(PIPELINE_COLUMN_ORDER)
    return (
        depth * RESEARCH_JOB_PRIORITY_DEPTH_PENALTY
        + col_idx * 100
        + (task_dict.get("position") or 0)
    )


def _tick() -> None:
    """
    Single scheduler tick:
      0. Dispatch file summary jobs (highest priority — agents are blocked waiting).
      1. Clean up finished sessions.
      2. Discover DAG-ready tasks.
      3. Sort by priority (shallow DAG first).
      4. For each ready task, check LLM capacity and dispatch if possible.
      5. Dispatch pending research jobs.
    """
    # Lazy imports to avoid circular deps at module load
    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_task, get_llm

    # 0. File summary jobs first — blocked agents are waiting on these
    _dispatch_file_summary_jobs()

    # 1. Cleanup finished sessions
    _cleanup_finished()

    # 2. Get all tasks, compute DAG readiness
    all_tasks = get_all_tasks()
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()

    # 3. Sort by priority
    if ready_tasks:
        by_id = {t["id"]: t for t in task_dicts}
        ready_tasks.sort(key=lambda t: _compute_priority(t, by_id))

    # 4. Try to dispatch each ready task
    for task_dict in ready_tasks:
        task_id = task_dict["id"]
        task_type = task_dict.get("type", "")

        # Only auto-dispatch task types configured in SCHEDULER_DISPATCHABLE_TYPES.
        if task_type not in SCHEDULER_DISPATCHABLE_TYPES:
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

        # Budget pre-flight: skip if worst-case cost exceeds remaining budget
        from app.database import budget_has_capacity
        worst = _estimate_worst_case_microcents(db_task.llm_id, db_task.budget_id)
        if worst > 0 and not budget_has_capacity(db_task.budget_id, worst):
            logger.info(
                "[scheduler] Skipping task '%s' — budget %s insufficient (%d µ¢ worst-case).",
                task_id, db_task.budget_id, worst,
            )
            from app.database import append_task_history
            append_task_history(
                task_id, "budget_skip",
                message=f"Budget {db_task.budget_id} insufficient ({worst} µ¢ worst-case needed)",
            )
            continue

        # Resolve project path for git tool isolation
        from app.database import get_project_path as _get_project_path
        project_path = None
        if db_task.project:
            project_path = _get_project_path(db_task.project)
            if project_path is None:
                logger.warning(
                    "Task '%s' project '%s' has no path — git tools use PROJECT_ROOT.",
                    task_id, db_task.project,
                )

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
            args=(task_id, task_type, llm, db_task, project_path),
            daemon=True,
            name=f"maestro-task-{task_id}",
        )
        with _active_sessions_lock:
            _active_sessions[task_id] = thread
        thread.start()

    # 5. Dispatch pending research jobs
    _dispatch_research_jobs()


def _dispatch_file_summary_jobs() -> None:
    """Dispatch pending file summary jobs — top priority, agents are blocked waiting."""
    from app.database import get_pending_file_summary_jobs, update_file_summary_job, get_llm

    pending = get_pending_file_summary_jobs(limit=20)
    for job in pending:
        if not job.llm_id:
            continue

        job_key = f"file-summary-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        with _llm_counts_lock:
            current = _llm_session_counts[llm.id]
            if current >= llm.parallel_sessions:
                continue
            _llm_session_counts[llm.id] += 1

        update_file_summary_job(job.id, status='running')

        thread = threading.Thread(
            target=_run_file_summary_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-file-summary-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
        thread.start()


def _run_file_summary_job(job: Any, llm: Any) -> None:
    """Execute a single file summary job in its own thread + event loop."""
    from app.database import update_file_summary_job
    from app.agent.file_summary_agent import execute_file_summary

    completion_key = f"file_summary:{job.sha1_hash}:{job.file_size_bytes}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Re-read the full file from disk so chunked processing sees complete content.
        # The stored job.file_content is capped at 32k; fall back to it if file is gone.
        try:
            with open(job.file_path, "r", encoding="utf-8", errors="replace") as _fh:
                full_content = _fh.read()
        except OSError:
            full_content = job.file_content

        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        result = loop.run_until_complete(execute_file_summary(
            sha1=job.sha1_hash,
            filesize=job.file_size_bytes,
            file_path=job.file_path,
            file_content=full_content,
            static_analysis_json=job.static_analysis_json,
            task_id=job.task_id,
            llm_id=job.llm_id,
            budget_id=job.budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm.model,
            previous_summary=getattr(job, 'previous_summary', None),
        ))
        update_file_summary_job(
            job.id,
            status='completed',
            prompt_tokens=result.get('prompt_tokens', 0),
            completion_tokens=result.get('completion_tokens', 0),
        )
        logger.debug("file_summary job %d completed (sha1=%s…)", job.id, job.sha1_hash[:8])
    except Exception:
        logger.exception("File summary job %d failed.", job.id)
        update_file_summary_job(job.id, status='failed')
    finally:
        loop.close()
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        # Always signal — even on failure — so waiters never hang
        signal_completion(completion_key)


def _dispatch_research_jobs() -> None:
    """Dispatch pending research jobs that have an LLM assigned."""
    from app.database import get_pending_research_jobs, update_research_job, get_llm

    pending = get_pending_research_jobs(limit=10)
    for job in pending:
        if not job.llm_id:
            continue

        job_key = f"research-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        with _llm_counts_lock:
            current = _llm_session_counts[llm.id]
            if current >= llm.parallel_sessions:
                continue
            _llm_session_counts[llm.id] += 1

        thread = threading.Thread(
            target=_run_research_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-research-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
        thread.start()


def _run_research_job(job: Any, llm: Any) -> None:
    """Execute a single research job in its own thread + event loop."""
    from app.database import update_research_job, get_task as _get_task
    from app.agent.research import run_research
    from app.agent.tools import set_task_git_cwd
    from app.database import get_project_path as _get_project_path

    task = _get_task(job.task_id)
    if task and task.project:
        project_path = _get_project_path(task.project)
        if project_path:
            set_task_git_cwd(project_path)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        update_research_job(job.id, status="running")
        context = json.loads(job.context) if job.context else {}
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        result = loop.run_until_complete(run_research(
            question=job.question,
            context=context,
            task_id=job.task_id,
            llm_id=job.llm_id,
            budget_id=job.budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm.model,
        ))
        update_research_job(
            job.id, status="completed",
            verdict=json.dumps(result.vote),
            findings=result.findings,
            lives_used=result.lives_used,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    except Exception:
        logger.exception("Research job %d failed in scheduler.", job.id)
        update_research_job(job.id, status="failed")
    finally:
        loop.close()
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)


def _run_task(task_id: str, task_type: str, llm: Any, db_task: Any = None, project_path: str | None = None) -> None:
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
            _run_intake(task_id, llm_base_url, llm_model, project_path)
        elif task_type == "indev":
            _run_dev_orchestrator_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "conceptual_review":
            _run_conceptual_review_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "optimization":
            _run_optimization_security_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "full_review":
            _run_full_review_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        else:
            _run_maestro_loop(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
    except Exception:
        _failed_cooldowns[task_id] = time.time()
        logger.exception("Task '%s' failed in scheduler dispatch (cooldown %ds).", task_id, int(_FAIL_COOLDOWN_SECONDS))
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)


def _run_intake(task_id: str, llm_base_url: str, llm_model: str,
                project_path: str | None = None) -> None:
    """Run the intake pipeline for an IDEA task."""
    from app.agent.intake import run_intake_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import (
        get_task, get_all_tasks, update_task,
        create_transition_vote, create_transition_result,
    )

    task = get_task(task_id)
    if not task:
        return
    set_task_git_cwd(project_path)

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
                project=task.project or None,  # Must be configured or pipeline will fail
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
                      budget_id: int | None = None,
                      project_path: str | None = None) -> None:
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
            project_path=project_path,
        )
        loop.run_until_complete(maestro.run())
    finally:
        loop.close()


def _run_dev_orchestrator_task(task_id: str, llm_base_url: str, llm_model: str,
                                max_context: int | None = None,
                                llm_id: int | None = None,
                                budget_id: int | None = None,
                                project_path: str | None = None) -> None:
    """Run the DevOrchestrator for an IN DEV task."""
    from app.agent.dev_orchestrator import run_dev_orchestrator
    from app.agent.tools import set_task_git_cwd
    from app.database import get_planning_result, update_task
    import json

    set_task_git_cwd(project_path)

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


def _run_conceptual_review_task(task_id: str, llm_base_url: str, llm_model: str,
                                 max_context: int | None = None,
                                 llm_id: int | None = None,
                                 budget_id: int | None = None,
                                 project_path: str | None = None) -> None:
    """Run the conceptual review pipeline for a CONCEPTUAL_REVIEW task."""
    from app.agent.conceptual_review import run_conceptual_review
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task, get_planning_result
    from app.database import (
        create_transition_vote, create_transition_result,
    )
    from datetime import datetime
    import json as _json

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    planning_result_obj = get_planning_result(task_id)
    planning_result = {}
    if planning_result_obj:
        planning_result = {
            "file_manifest": _json.loads(planning_result_obj.file_manifest or "[]"),
            "dependency_graph": _json.loads(planning_result_obj.dependency_graph or "{}"),
            "implementation_steps": _json.loads(planning_result_obj.implementation_steps or "[]"),
            "test_strategy": _json.loads(planning_result_obj.test_strategy or "[]"),
        }

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_conceptual_review(
                task_id=task_id,
                task_description=task.description or "",
                planning_result=planning_result,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project_path=project_path,
            )
        )
        create_transition_result(
            task_id=task_id,
            transition="conceptual_to_optimization",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=result.get("total_prompt_tokens", 0),
            total_completion_tokens=result.get("total_completion_tokens", 0),
        )
        if result.get("outcome") == "passed":
            update_task(task_id, type="optimization")
            logger.info("Task '%s' advanced to OPTIMIZATION via scheduler.", task_id)
        else:
            update_task(task_id, type="indev")
            _record_demotion_inline(task_id, "conceptual_review", "indev", result.get("summary", ""))
            logger.info("Task '%s' demoted to INDEV from conceptual review via scheduler.", task_id)
    finally:
        loop.close()


def _run_optimization_security_task(task_id: str, llm_base_url: str, llm_model: str,
                                     max_context: int | None = None,
                                     llm_id: int | None = None,
                                     budget_id: int | None = None,
                                     project_path: str | None = None) -> None:
    """Run optimization then security pipeline for an OPTIMIZATION task."""
    from app.agent.optimization import run_optimization_pipeline
    from app.agent.security_review import run_security_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task
    from app.database import (
        create_transition_vote, create_transition_result,
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Run optimization first
        opt_result = loop.run_until_complete(
            run_optimization_pipeline(
                task_id=task_id,
                task_description=task.description or "",
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project_path=project_path,
            )
        )
        logger.info("[optimization] Task '%s' via scheduler: %s", task_id, opt_result.get("outcome"))

        # Run security review
        sec_result = loop.run_until_complete(
            run_security_pipeline(
                task_id=task_id,
                task_description=task.description or "",
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project_path=project_path,
            )
        )
        create_transition_result(
            task_id=task_id,
            transition="security_review",
            outcome=sec_result.get("outcome", "unknown"),
            vote_summary=sec_result,
            total_prompt_tokens=sec_result.get("total_prompt_tokens", 0),
            total_completion_tokens=sec_result.get("total_completion_tokens", 0),
        )

        if sec_result.get("outcome") == "passed":
            update_task(task_id, type="security")
            update_task(task_id, type="full_review")
            logger.info("[security] Task '%s' advanced to FULL REVIEW via scheduler.", task_id)
        else:
            demotion = sec_result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "security", demotion, sec_result.get("summary", ""))
            logger.warning("[security] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    finally:
        loop.close()


def _run_full_review_task(task_id: str, llm_base_url: str, llm_model: str,
                           max_context: int | None = None,
                           llm_id: int | None = None,
                           budget_id: int | None = None,
                           project_path: str | None = None) -> None:
    """Run the full review pipeline for a FULL_REVIEW task."""
    from app.agent.full_review import run_full_review_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task
    from app.database import (
        create_transition_vote, create_transition_result,
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(
            run_full_review_pipeline(
                task_id=task_id,
                task_description=task.description or "",
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                project_path=project_path,
            )
        )
        create_transition_result(
            task_id=task_id,
            transition="full_review",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=result.get("total_prompt_tokens", 0),
            total_completion_tokens=result.get("total_completion_tokens", 0),
        )

        if result.get("outcome") == "passed":
            # Auto-merge: run execute_merge inline
            from app.agent.merge import execute_merge
            from app.database import get_project_path as _get_project_path
            pp = project_path
            if not pp and task.project:
                pp = _get_project_path(task.project)
            merge_result = execute_merge(task_id, project_path=pp)
            if merge_result.status == "merged":
                logger.info("[merge] Task '%s' merged to main via scheduler.", task_id)
                _check_completion_rollup_inline(task_id)
            elif merge_result.status == "conflict":
                update_task(task_id, type="indev")
                _record_demotion_inline(task_id, "merge", "indev", merge_result.error_detail or "Merge conflict")
                logger.warning("[merge] Task '%s' merge conflict via scheduler.", task_id)
            elif merge_result.status == "test_failure":
                update_task(task_id, type="indev")
                _record_demotion_inline(task_id, "merge", "indev", merge_result.error_detail or "Tests failed")
                logger.warning("[merge] Task '%s' tests failed after merge via scheduler.", task_id)
            elif merge_result.status == "push_failure":
                update_task(task_id, type="full_review")
                _record_demotion_inline(task_id, "merge", "full_review", merge_result.error_detail or "Push failed")
                logger.error("[merge] Task '%s' push failed via scheduler.", task_id)
            else:
                logger.error("[merge] Task '%s' merge error via scheduler: %s", task_id, merge_result.error_detail)
        else:
            demotion = result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "full_review", demotion, result.get("summary", ""))
            logger.warning("[full_review] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    finally:
        loop.close()


def _record_demotion_inline(task_id: str, from_stage: str, to_stage: str, reason: str) -> None:
    """Record a demotion event — scheduler-local version (avoids importing from main.py)."""
    from datetime import datetime, timezone
    from app.database import get_task, update_task
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


def _check_completion_rollup_inline(task_id: str) -> None:
    """Mirror of main._check_completion_rollup — avoids circular import."""
    import app.database as db
    task = db.get_task(task_id)
    if not task or not task.parent_task_id:
        return
    parent = db.get_task(task.parent_task_id)
    if not parent:
        return
    children = db.get_active_child_tasks(parent.id)
    if not children:
        return
    all_done = all((c.type or "").lower() in PIPELINE_DONE_STATUSES for c in children)
    if all_done:
        db.update_task(parent.id, type="completed")
        logger.info("[rollup] All children of '%s' completed. Parent marked completed.", parent.id)
        _check_completion_rollup_inline(parent.id)


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
