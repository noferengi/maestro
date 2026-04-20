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
import os
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
    SURVEY_VERDICT_MAX_TOKENS,
    SURVEY_SUMMARY_MAX_TOKENS,
)
from app.agent.llm_client import is_shutting_down, signal_shutdown, signal_force_shutdown, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "Scheduler"

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------

# task_id -> threading.Thread for currently running loops
_active_sessions: dict[str, threading.Thread] = {}
_active_sessions_lock = threading.Lock()

# session key -> llm_id for all active sessions (tasks, file summaries, research, recovery)
# Protected by _active_sessions_lock.  Used to enforce the one-LLM-at-a-time policy.
_session_llm_ids: dict[str, int] = {}

# session key -> display title for background jobs (arch-gen, research, etc.)
# Protected by _active_sessions_lock.
_session_titles: dict[str, str] = {}

# Per-LLM active session count: llm_id -> count
_llm_session_counts: dict[int, int] = defaultdict(int)
_llm_counts_lock = threading.Lock()

# ---------------------------------------------------------------------------
# Dreamer state
# ---------------------------------------------------------------------------
# project_name -> timestamp (float) of the most recent pipeline activity we
# observed for that project.  Initialised to now() when the project is first
# seen so new projects get a grace period before Dreamer fires.
_project_last_activity: dict[str, float] = {}
_project_last_activity_lock = threading.Lock()

# Projects that currently have a Dreamer thread running.
_active_dreamer_projects: set[str] = set()
_active_dreamer_lock = threading.Lock()

# ---------------------------------------------------------------------------
# External pipeline session registry
# ---------------------------------------------------------------------------
# API-triggered pipelines (intake, planning, review, etc.) run as FastAPI
# BackgroundTasks threads - they are NOT dispatched by the scheduler and are
# therefore invisible to the one-LLM-at-a-time policy by default.  They
# register here so the scheduler sees them and won't dispatch conflicting work
# to a different model while they are running.

_external_sessions: dict[str, int] = {}   # session_key -> llm_id
_external_sessions_lock = threading.Lock()


def register_pipeline_session(key: str, llm_id: int) -> None:
    """Register an API-triggered pipeline so the scheduler's one-LLM policy sees it."""
    with _external_sessions_lock:
        _external_sessions[key] = llm_id
    with _llm_counts_lock:
        _llm_session_counts[llm_id] += 1


def unregister_pipeline_session(key: str, llm_id: int) -> None:
    """Deregister a completed API-triggered pipeline and release its LLM slot."""
    with _external_sessions_lock:
        _external_sessions.pop(key, None)
    with _llm_counts_lock:
        _llm_session_counts[llm_id] = max(0, _llm_session_counts[llm_id] - 1)


def wait_and_register_pipeline_session(
    key: str,
    llm_id: int,
    poll_interval: float = 3.0,
    timeout: float = 600.0,
) -> bool:
    """Block until the target LLM is safe to use, then register the session.

    "Safe to use" means:
      - No active session is using a *different* LLM (one-model-at-a-time),
      - The target LLM has at least one free parallel slot.

    Polls every *poll_interval* seconds.  Returns True if registered within
    *timeout* seconds, False if it gave up (caller should abort the pipeline).
    """
    from app.database import get_llm as _get_llm

    deadline = time.monotonic() + timeout
    llm = _get_llm(llm_id)
    max_slots = llm.parallel_sessions if llm else 1

    while time.monotonic() < deadline:
        if is_shutting_down():
            logger.info(
                "wait_and_register: aborting - server is shutting down (key='%s').", key
            )
            return False
        # Collect all active LLM IDs (scheduler threads + external pipelines)
        with _active_sessions_lock:
            active_llm_ids: set[int] = {
                lid for k, lid in _session_llm_ids.items()
                if k in _active_sessions and _active_sessions[k].is_alive()
            }
        with _external_sessions_lock:
            active_llm_ids.update(_external_sessions.values())

        # Gate 1 - one-model-at-a-time: only proceed if no OTHER model is active
        conflicting = active_llm_ids - {llm_id}
        if conflicting:
            logger.debug(
                "wait_and_register: LLM %d waiting - conflicting active LLMs: %s",
                llm_id, conflicting,
            )
            time.sleep(poll_interval)
            continue

        # Gate 2 - per-LLM capacity: only proceed if a slot is free
        with _llm_counts_lock:
            current = _llm_session_counts[llm_id]
            if current >= max_slots:
                logger.debug(
                    "wait_and_register: LLM %d at capacity (%d/%d), waiting.",
                    llm_id, current, max_slots,
                )
                time.sleep(poll_interval)
                continue
            # Atomically claim the slot
            _llm_session_counts[llm_id] += 1

        with _external_sessions_lock:
            _external_sessions[key] = llm_id

        logger.debug("wait_and_register: LLM %d slot registered for key '%s'.", llm_id, key)
        return True

    logger.warning(
        "wait_and_register: timed out after %.0fs waiting for LLM %d slot (key='%s').",
        timeout, llm_id, key,
    )
    return False

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
    registry - meaning it finished before we started waiting). Returns False
    on timeout.
    """
    with _pending_completions_lock:
        ev = _pending_completions.get(key)
    if ev is None:
        # Key already removed - job completed before we reached this call.
        return True
    return ev.wait(timeout=timeout)


def park_session(session_key: str, llm_id: int) -> None:
    """Temporarily release an agent's LLM slot while it waits for a child job.

    The thread stays in ``_active_sessions`` so the scheduler won't re-dispatch
    the task, but the LLM-id entry is removed from ``_session_llm_ids`` and the
    per-LLM count is decremented so the scheduler can assign that freed slot to
    the child research job.

    Must be paired with a matching ``unpark_session`` call.
    """
    with _active_sessions_lock:
        removed_lid = _session_llm_ids.pop(session_key, None)
    if removed_lid is not None:
        with _llm_counts_lock:
            _llm_session_counts[removed_lid] = max(0, _llm_session_counts[removed_lid] - 1)
        logger.debug(
            "[scheduler] Parked session '%s' (LLM %d freed for child job).",
            session_key, removed_lid,
        )


def unpark_session(session_key: str, llm_id: int) -> None:
    """Re-register an agent's LLM slot after its child job completes.

    Call once the child job's completion event fires and the agent is about to
    resume issuing LLM calls.
    """
    with _active_sessions_lock:
        if session_key in _active_sessions:
            _session_llm_ids[session_key] = llm_id
        else:
            # Thread was cleaned up while parked — re-insert the current thread so
            # the scheduler still knows this session is alive.
            _active_sessions[session_key] = threading.current_thread()
            _session_llm_ids[session_key] = llm_id
    with _llm_counts_lock:
        _llm_session_counts[llm_id] += 1
    logger.debug(
        "[scheduler] Unparked session '%s' (LLM %d re-acquired).",
        session_key, llm_id,
    )


# task_id -> timestamp of last failed dispatch (cooldown to avoid retry storms)
_failed_cooldowns: dict[str, float] = {}
_FAIL_COOLDOWN_SECONDS = 60.0  # Wait 60s before retrying a failed task

# task_id -> timestamp of last rejected intake run (longer inter-retry backoff)
# Rejection means "not ready yet", not "permanently blocked" — always retry unless exhausted.
_rejection_cooldowns: dict[str, float] = {}
_REJECTION_RETRY_COOLDOWN = 300.0   # 5 min between retries

# Planning gate failure limits.  After this many failed gate attempts the task
# is demoted back to IDEA and a forced-subdivide record is written so the
# stranded-subdivision detector can break it into smaller pieces.
_MAX_PLANNING_GATE_FAILURES = 5

# Background job retry / rescue parameters.
# Failed file-summary and research jobs are reset to 'pending' after these cooldowns
# so they flow through the existing dispatch machinery on the next tick.
# 'Running' jobs with no live thread (orphaned by a crash) are reset immediately.
_FILE_SUMMARY_RETRY_COOLDOWN = 300.0  # 5 min before re-queuing a failed file-summary job
_RESEARCH_JOB_RETRY_COOLDOWN = 300.0  # 5 min before re-queuing a failed research job
_ARCH_GEN_RETRY_COOLDOWN     = 300.0  # 5 min before re-queuing a failed arch-gen job
_ARCH_GEN_MAX_RETRIES        = 3      # abandon + inbox notification after this many failures

# Project-level failure throttling: project_name -> time.time() of most recent failure/rescue.
# Prevents a "retry storm" for background jobs (arch_gen, etc.) if a project is in a bad state.
_project_failure_cooldowns: dict[str, float] = {}
_PROJECT_FAILURE_COOLDOWN_SECONDS = 300.0  # 5 minutes


def _record_project_failure(project_name: str | None) -> None:
    """Mark a project as recently failed to throttle its background job queue."""
    if not project_name:
        return
    _project_failure_cooldowns[project_name] = time.time()


def _is_project_in_failure_cooldown(project_name: str | None) -> bool:
    """Return True if the project has a recent failure/rescue and should be throttled."""
    if not project_name:
        return False
    last_failure = _project_failure_cooldowns.get(project_name, 0)
    return (time.time() - last_failure) < _PROJECT_FAILURE_COOLDOWN_SECONDS


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

    # One-time startup cleanup: cancel any stale oversized or log-file jobs that
    # were created before size/log guards existed.  These would otherwise loop
    # forever: fail → 5-min cooldown → retry → fail.
    try:
        from app.database import cancel_bad_file_summary_jobs
        from app.agent.config import SUMMARY_MAX_FILE_SIZE
        n = cancel_bad_file_summary_jobs(SUMMARY_MAX_FILE_SIZE)
        if n:
            logger.info("Startup: cancelled %d oversized/log file summary job(s).", n)
    except Exception:
        logger.exception("Startup: cancel_bad_file_summary_jobs failed (non-fatal).")

    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="maestro-scheduler"
    )
    _scheduler_thread.start()
    logger.info("Scheduler started (tick every %.1fs).", SCHEDULER_TICK_INTERVAL)


def stop_scheduler(wait_for_sessions: bool = True, timeout: float = 60.0) -> None:
    """Signal the scheduler to stop and wait for it.

    Shutdown runs in two phases within the total *timeout* budget:

      Phase 1 (timeout − 5 s): graceful drain.  The shutdown flag is already set
        before this call, so streaming calls exit at their next between-chunk check
        and the poll loop exits within _SHUTDOWN_POLL_SLICE seconds.  Worker
        threads are given time to wrap up naturally.

      Phase 2 (5 s): forced exit.  Any thread still alive after phase 1 receives
        signal_force_shutdown(), which interrupts the streaming poll loop
        immediately.  Threads blocked in non-LLM work (DB writes, git, etc.) are
        not interruptible and are logged as survivors.
    """
    global _scheduler_thread
    if _scheduler_thread is None:
        return
    _scheduler_stop.set()
    _scheduler_thread.join(timeout=SCHEDULER_TICK_INTERVAL + 2)
    _scheduler_thread = None

    if not wait_for_sessions:
        logger.info("Scheduler stopped.")
        return

    with _active_sessions_lock:
        threads = [(key, t) for key, t in _active_sessions.items() if t.is_alive()]

    if not threads:
        logger.info("Scheduler stopped.")
        return

    # --- Phase 1: graceful drain ---
    phase1_secs = max(timeout - 5.0, 5.0)
    logger.info(
        "Shutdown phase 1: waiting up to %.0fs for %d session thread(s): %s",
        phase1_secs, len(threads), ", ".join(k for k, _ in threads),
    )
    deadline = time.monotonic() + phase1_secs
    try:
        for _key, t in threads:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            t.join(timeout=remaining)
    except KeyboardInterrupt:
        logger.warning("Shutdown interrupted during phase 1. Proceeding to phase 2.")

    # --- Phase 2: forced exit ---
    with _active_sessions_lock:
        still_alive = [(key, t) for key, t in _active_sessions.items() if t.is_alive()]

    if still_alive:
        logger.warning(
            "Shutdown phase 2: %d thread(s) still alive after %.0fs graceful window,"
            " signalling force shutdown: %s",
            len(still_alive), phase1_secs, ", ".join(k for k, _ in still_alive),
        )
        signal_force_shutdown()
        for _key, t in still_alive:
            t.join(timeout=5.0)

        with _active_sessions_lock:
            survivors = [key for key, t in _active_sessions.items() if t.is_alive()]
        if survivors:
            logger.warning(
                "Scheduler shutdown: %d thread(s) did not exit after force shutdown: %s",
                len(survivors), ", ".join(survivors),
            )

    logger.info("Scheduler stopped.")


def get_scheduler_status() -> dict:
    """Return a snapshot of the scheduler's state with active/queued/blocked task details."""
    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_llm

    with _active_sessions_lock:
        active_session_ids: set[str] = {
            tid for tid, t in _active_sessions.items() if t.is_alive()
        }
        # Determine currently pinned LLM (one-at-a-time policy)
        pinned_llm_id: int | None = None
        active_llm_set = {
            lid for key, lid in _session_llm_ids.items()
            if key in _active_sessions and _active_sessions[key].is_alive()
        }
        if active_llm_set:
            pinned_llm_id = next(iter(active_llm_set))
    
    with _external_sessions_lock:
        external_sessions_snapshot = dict(_external_sessions)
        for lid in external_sessions_snapshot.values():
            if pinned_llm_id is None:
                pinned_llm_id = lid

    with _llm_counts_lock:
        llm_counts = dict(_llm_session_counts)

    now = time.time()

    # LLM info cache: llm_id → {id, name, current, max}
    llm_info_cache: dict[int, dict] = {}

    def _llm_info(llm_id: int | None) -> dict | None:
        if llm_id is None:
            return None
        if llm_id not in llm_info_cache:
            llm = get_llm(llm_id)
            if llm:
                llm_info_cache[llm_id] = {
                    "id": llm.id,
                    "name": llm.model or f"LLM {llm.id}",
                    "current": llm_counts.get(llm.id, 0),
                    "max": llm.parallel_sessions,
                }
            else:
                llm_info_cache[llm_id] = None  # type: ignore[assignment]
        return llm_info_cache[llm_id]

    # Gather tasks and compute DAG
    try:
        all_tasks = get_all_tasks()
    except Exception:
        all_tasks = []

    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()
    ready_ids: set[str] = {t["id"] for t in ready_tasks}

    # Big-idea parents (have children - skipped by DAG as non-dispatchable)
    children_by_parent: set[str] = {
        t.get("parent_task_id")
        for t in task_dicts
        if t.get("parent_task_id")
    }

    dispatchable_set = set(SCHEDULER_DISPATCHABLE_TYPES)
    done_set = {s.lower() for s in PIPELINE_DONE_STATUSES}
    never_dispatch = {"completed", "cancelled", "subdividing", "accepted"}

    active_list: list[dict] = []
    queued_list: list[dict] = []
    blocked_list: list[dict] = []

    task_by_id = {t.id: t for t in all_tasks}

    # 1. Add scheduler-managed active tasks
    for tid in active_session_ids:
        task = task_by_id.get(tid)
        if task:
            info = _llm_info(task.llm_id)
            active_list.append({
                "id": tid,
                "title": (task.title or tid)[:80],
                "type": (task.type or "").lower(),
                "project": task.project or "",
                "llm_id": task.llm_id,
                "llm_name": info["name"] if info else "(no LLM)",
            })
        else:
            # Might be a background job (file summary, research)
            # These are identified by keys like "file-summary-123"
            llm_id = _session_llm_ids.get(tid)
            info = _llm_info(llm_id)
            active_list.append({
                "id": tid,
                "title": _session_titles.get(tid, tid),
                "type": "background",
                "project": "",
                "llm_id": llm_id,
                "llm_name": info["name"] if info else "(no LLM)",
            })

    # 2. Add external pipeline sessions
    for key, llm_id in external_sessions_snapshot.items():
        info = _llm_info(llm_id)
        active_list.append({
            "id": f"ext:{key}",
            "title": f"External: {key}",
            "type": "external",
            "project": "",
            "llm_id": llm_id,
            "llm_name": info["name"] if info else "(no LLM)",
        })

    for task in all_tasks:
        tid = task.id
        task_type = (task.type or "").lower()

        # Already in active_list?
        if tid in active_session_ids:
            continue

        # Only care about dispatchable types that aren't terminal
        if task_type not in dispatchable_set:
            continue
        if task_type in never_dispatch or task_type in done_set:
            continue

        info = _llm_info(task.llm_id)
        entry = {
            "id": tid,
            "title": (task.title or tid)[:80],
            "type": task_type,
            "project": task.project or "",
            "llm_id": task.llm_id,
            "llm_name": info["name"] if info else "(no LLM)",
        }

        if tid in ready_ids:
            # Ready but not dispatched - determine why
            if not task.llm_id:
                reason = "no_llm"
            elif tid in _failed_cooldowns and (now - _failed_cooldowns[tid]) < _FAIL_COOLDOWN_SECONDS:
                remaining = int(_FAIL_COOLDOWN_SECONDS - (now - _failed_cooldowns[tid]))
                reason = f"cooldown ({remaining}s)"
            elif info and llm_counts.get(task.llm_id, 0) >= info["max"]:
                reason = "at_capacity"
            else:
                reason = "pending"
            entry["reason"] = reason
            queued_list.append(entry)
        else:
            # Not ready - find blocking prerequisites
            blocking = [
                p for p in (task.prerequisites or [])
                if not resolver._is_effectively_done(p)  # noqa: SLF001
            ]
            # Also note if it's a parent-is-working case
            if tid in children_by_parent:
                continue  # Big Idea parent with children - not directly dispatchable
            entry["blocking_prereqs"] = blocking[:6]
            # Include titles for blocking prereqs where available
            entry["blocking_titles"] = [
                (task_by_id[p].title or p)[:40] if p in task_by_id else p
                for p in blocking[:6]
            ]
            blocked_list.append(entry)

    # Sort queued by scheduler priority
    by_mini = {t["id"]: t for t in task_dicts}
    queued_list.sort(key=lambda t: _compute_priority(by_mini.get(t["id"], {}), by_mini))

    # LLM capacities summary (only LLMs with tasks in our lists)
    seen_llm_ids = {
        t["llm_id"] for t in active_list + queued_list + blocked_list
        if t.get("llm_id") is not None
    }
    llm_capacities = {}
    for lid in seen_llm_ids:
        info = _llm_info(lid)
        if info:
            llm_capacities[str(lid)] = {
                "name": info["name"],
                "current": info["current"],
                "max": info["max"],
            }

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
        # Legacy fields kept for backwards compat
        "active_sessions": {tid: True for tid in active_session_ids},
        "llm_session_counts": llm_counts,
        "tick_interval": SCHEDULER_TICK_INTERVAL,
        "pending_research_jobs": pending_research,
        "pending_file_summary_jobs": pending_file_summaries,
        # One-LLM-at-a-time policy: the currently pinned LLM ID (null = no active sessions)
        "pinned_llm_id": pinned_llm_id,
        # Rich queue data
        "active": active_list,
        "queued": queued_list,
        "blocked": blocked_list,
        "llm_capacities": llm_capacities,
    }


# ---------------------------------------------------------------------------
# Core loop
# ---------------------------------------------------------------------------

def _scheduler_loop() -> None:
    """Main scheduler loop - runs in a background thread."""
    logger.info("Scheduler loop started.")
    while not _scheduler_stop.is_set():
        try:
            _tick()
        except ShutdownError:
            logger.info("Scheduler loop aborted due to server shutdown.")
            break
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


def _check_and_reserve_slot(
    llm: Any,
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
    label: str = "",
) -> bool:
    """Check LLM and compute-node capacity and atomically reserve a slot if available.

    Mutates *node_active_counts* and *node_session_counts* in-place on success so that
    subsequent dispatches within the same tick see the updated counts - this is what
    prevents over-dispatch when multiple jobs are dispatched in one tick.

    Returns True if a slot was reserved, False if either cap is already reached.
    """
    from app.database import get_compute_node as _gcn

    llm_id = llm.id

    # Resolve compute node for this LLM (cached per-tick in llm_node_cache)
    if llm_id not in llm_node_cache:
        raw_nid = getattr(llm, "compute_node_id", None)
        llm_node_cache[llm_id] = raw_nid if isinstance(raw_nid, int) else None
    node_id = llm_node_cache[llm_id]

    # Node-level cap checks
    if node_id is not None:
        if node_id not in node_obj_cache:
            node_obj_cache[node_id] = _gcn(node_id)
        node_obj = node_obj_cache.get(node_id)
        if node_obj is not None:
            with _llm_counts_lock:
                llm_already_loaded = _llm_session_counts[llm_id] > 0
            mlm = getattr(node_obj, "max_loaded_models", 1)
            if not llm_already_loaded and node_active_counts[node_id] >= mlm:
                logger.debug(
                    "Compute node %d model slots full (%d/%d), deferring%s.",
                    node_id, node_active_counts[node_id], mlm,
                    f" {label}" if label else "",
                )
                return False
            node_sess = node_session_counts[node_id]
            if node_sess >= node_obj.max_parallel_sessions:
                logger.debug(
                    "Compute node %d session cap reached (%d/%d), deferring%s.",
                    node_id, node_sess, node_obj.max_parallel_sessions,
                    f" {label}" if label else "",
                )
                return False

    # Per-LLM cap check + atomic reserve
    with _llm_counts_lock:
        current = _llm_session_counts[llm_id]
        if current >= llm.parallel_sessions:
            logger.debug(
                "LLM %d at capacity (%d/%d), deferring%s.",
                llm_id, current, llm.parallel_sessions,
                f" {label}" if label else "",
            )
            return False
        was_already_active = _llm_session_counts[llm_id] > 0
        _llm_session_counts[llm_id] += 1

    # Update tick-local node counters so subsequent dispatches within this tick see
    # the updated state.
    if node_id is not None:
        if not was_already_active:
            node_active_counts[node_id] += 1
        node_session_counts[node_id] += 1

    return True


def _tick() -> None:
    """
    Single scheduler tick:
      0. Clean up finished sessions.
      1. Determine which LLM (if any) is already active - one-LLM-at-a-time policy.
      2. Dispatch file summary jobs (highest priority - agents are blocked waiting).
      3. Discover DAG-ready tasks and sort by priority.
      4. For each ready task, check LLM capacity and dispatch if possible.
      5. Dispatch pending research jobs.
      6. Recover stranded subdivision tasks (voted SUBDIVIDE but have no children).

    One-LLM-at-a-time policy: the llama.cpp router can only run one model at a time.
    Switching models requires unloading the current model first.  If we dispatch to
    multiple LLM IDs simultaneously the router thrashes between models and nothing
    makes progress.  Solution: once a session is active for a given LLM, only dispatch
    more work to that same LLM until ALL its sessions finish, then pick the next LLM.
    File summary jobs respect the same constraint (they use the same LLM as their task).
    """
    # Do not dispatch new work once shutdown has been signalled.
    from app.agent.llm_client import is_shutting_down
    if is_shutting_down():
        return

    # Lazy imports to avoid circular deps at module load
    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_task, get_llm, get_compute_node

    # 0. Cleanup finished sessions (also removes from _session_llm_ids)
    _cleanup_finished()

    # 0a. Rescue stale background jobs: orphaned 'running' and cooled-down 'failed'
    #     file-summary and research jobs are reset to 'pending' so they flow through
    #     the dispatch phases below in this same tick.
    _rescue_stale_jobs()

    # 0b. Build per-compute-node counters from current _llm_session_counts.
    #     Two separate caps are enforced on a ComputeNode:
    #       node_loaded_counts[node_id]  = distinct LLMs currently active (for max_loaded_models)
    #       node_session_counts[node_id] = total sessions across all LLMs  (for max_parallel_sessions)
    #     max_loaded_models  - how many different model weights can reside in VRAM simultaneously
    #     max_parallel_sessions - total concurrent request slots across all loaded models
    _llm_node_cache: dict[int, int | None] = {}   # llm_id -> compute_node_id (or None)
    _node_obj_cache: dict[int, object] = {}        # node_id -> ComputeNode object
    _node_active_llms: dict[int, set[int]] = defaultdict(set)  # node_id -> {llm_ids with active sessions}
    _node_session_counts: dict[int, int] = defaultdict(int)    # node_id -> total active sessions
    with _llm_counts_lock:
        snap = dict(_llm_session_counts)
    for llm_id, count in snap.items():
        if count <= 0:
            continue
        if llm_id not in _llm_node_cache:
            llm_obj = get_llm(llm_id)
            raw_nid = getattr(llm_obj, 'compute_node_id', None) if llm_obj else None
            _llm_node_cache[llm_id] = raw_nid if isinstance(raw_nid, int) else None
        node_id = _llm_node_cache[llm_id]
        if node_id is not None:
            _node_active_llms[node_id].add(llm_id)
            _node_session_counts[node_id] += count
    node_active_counts: dict[int, int] = defaultdict(int)   # distinct loaded LLMs per node
    for node_id, llm_set in _node_active_llms.items():
        node_active_counts[node_id] = len(llm_set)

    # 1. Determine the currently pinned LLM (one-at-a-time policy).
    #    allowed_llm_id = None  → nothing is running; first dispatch pins a new LLM.
    #    allowed_llm_id = N     → only dispatch to LLM N until it drains completely.
    #    Includes both scheduler-dispatched sessions (thread liveness check) AND
    #    API-triggered pipeline sessions registered via register_pipeline_session().
    with _active_sessions_lock:
        active_llm_ids: set[int] = {
            lid for key, lid in _session_llm_ids.items()
            if key in _active_sessions and _active_sessions[key].is_alive()
        }
    with _external_sessions_lock:
        active_llm_ids.update(_external_sessions.values())
    allowed_llm_id: int | None = next(iter(active_llm_ids)) if active_llm_ids else None
    if allowed_llm_id is not None:
        logger.debug(f"[{AGENT_NAME}] One-LLM policy: pinned to LLM %d.", allowed_llm_id)

    # 2. File summary jobs first - blocked agents are waiting on these.
    #    Pass allowed_llm_id and node-state dicts so they respect all capacity caps.
    allowed_llm_id = _dispatch_file_summary_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 3. Get all tasks, compute DAG readiness, sort by priority
    all_tasks = get_all_tasks()
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()

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

        # For 'idea' tasks: skip exhausted tasks (human reset required), skip ones
        # already successfully processed, and retry rejected ones after a cooldown.
        if task_type == "idea":
            from app.database import get_transition_results, get_task as _get_task_dispatch
            _db_task = _get_task_dispatch(task_id)
            if _db_task and _db_task.intake_exhausted_at:
                continue  # Intake exhausted — human must reset via /reset-intake
            existing = get_transition_results(task_id, transition="idea_to_planning")
            if existing:
                latest_outcome = existing[0].outcome
                if latest_outcome in ("passed", "subdivide"):
                    continue  # already handled - don't re-run intake
                # Rejected / needs_research / etc.: retry after cooldown
                if task_id in _rejection_cooldowns:
                    if time.time() - _rejection_cooldowns[task_id] < _REJECTION_RETRY_COOLDOWN:
                        continue

        # Already running?
        with _active_sessions_lock:
            if task_id in _active_sessions and _active_sessions[task_id].is_alive():
                continue

        # PIP resolution guard: don't re-dispatch a review stage while resolution
        # agents are still working on its PIPs.  The stage will re-enter naturally
        # on the next tick after all jobs reach 'done'.
        if task_type in {"conceptual_review", "optimization", "security", "full_review"}:
            from app.database import get_active_pip_resolution_jobs_for_task
            if get_active_pip_resolution_jobs_for_task(task_id):
                logger.debug(
                    "[pip] Skipping dispatch of '%s' (%s) — pip_resolution_jobs active.",
                    task_id, task_type,
                )
                continue

        # Planning-gate rejection cooldown (5 min) — mirrors the intake rejection
        # cooldown so a task that keeps failing the gate doesn't spin hot.
        if task_type == "planning":
            if task_id in _rejection_cooldowns:
                if time.time() - _rejection_cooldowns[task_id] < _REJECTION_RETRY_COOLDOWN:
                    continue

        # Cooldown after failure - don't retry for 60s
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

        # One-LLM-at-a-time: skip tasks whose LLM differs from the active one.
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue
        # Pin to this LLM for the rest of this tick.
        if allowed_llm_id is None:
            allowed_llm_id = llm.id
            logger.info(f"[{AGENT_NAME}] One-LLM policy: pinning to LLM %d (%s).", llm.id, llm.model)

        # Budget pre-flight: skip if worst-case cost exceeds remaining budget
        from app.database import budget_has_capacity
        worst = _estimate_worst_case_microcents(db_task.llm_id, db_task.budget_id)
        if worst > 0 and not budget_has_capacity(db_task.budget_id, worst):
            logger.info(
                f"[{AGENT_NAME}] Skipping task '%s' - budget %s insufficient (%d µ¢ worst-case).",
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
                    "Task '%s' project '%s' has no path - git tools use PROJECT_ROOT.",
                    task_id, db_task.project,
                )

        # Check and reserve capacity (node-level + per-LLM, updates tick-local counters)
        if not _check_and_reserve_slot(
            llm, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
            label=f"task '{task_id}'",
        ):
            continue

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
            _session_llm_ids[task_id] = llm.id
        try:
            thread.start()
        except Exception:
            logger.exception("Failed to start thread for task '%s'.", task_id)
            with _active_sessions_lock:
                _active_sessions.pop(task_id, None)
                _session_llm_ids.pop(task_id, None)
            with _llm_counts_lock:
                _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
            # local tick counters (node_active_counts, _node_session_counts) aren't
            # corrected here, but they only affect the remainder of this tick.

    # 5. Dispatch pending research jobs (respects one-LLM policy + full capacity caps)
    _dispatch_research_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 5.5. Dispatch pending arch gen jobs (lower priority than research; no caller blocking)
    _dispatch_arch_gen_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 5.5.5. Dispatch project survey jobs (hierarchical summarization)
    _dispatch_scope_survey_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 5.6. Dispatch pending PIP resolution jobs (research + resolution agents for blocked PIPs)
    _dispatch_pip_resolution_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 6. Recover stranded subdivision tasks (respects one-LLM policy + full capacity caps)
    _dispatch_stranded_subdivisions(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 7. Dreamer: resurrect stalled projects (fires when no pipeline progress for N ticks)
    _dispatch_dreamer(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )


def _project_has_dreamer_signal(project) -> bool:
    """Return True if the project has enough signal for the Dreamer to operate on.

    A project has signal when at least one of the following is true:
      - Its filesystem path contains source files (substantive codebase exists).
      - It has at least one ACTIVE task of any type (human or AI placed work here).

    Soft-deleted tasks are deliberately excluded — they represent cleaned-up
    history, not current intent.  A project the user has emptied and whose
    filesystem path is empty or absent is a placeholder shell; the Dreamer
    has nothing to latch onto.
    """
    import os
    from app.database.session import SessionLocal
    from app.database.models import Task

    # Check for any ACTIVE task (arch cards, idea cards, pipeline tasks)
    db = SessionLocal()
    try:
        any_task = (
            db.query(Task.id)
              .filter(Task.project_id == project.id, Task.is_active == True)
              .limit(1)
              .first()
        )
        if any_task:
            return True
    except Exception:
        pass
    finally:
        db.close()

    # Check if the project path contains any source files
    if project.path and os.path.isdir(project.path):
        from app.agent.path_filter import walk_safe
        for _root, dirs, files in walk_safe(project.path):
            if files:
                return True

    return False


def _dispatch_dreamer(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Fire a DreamerAgent for any project that has been stalled long enough.

    "Stalled" = no TransitionResult created for the project's tasks in the last
    DREAMER_STALL_TICKS * SCHEDULER_TICK_INTERVAL seconds.

    One Dreamer per project at a time.  Dreamers MUST respect LLM/node capacity —
    each Dreamer holds one session slot for its entire run so it doesn't pile on
    top of a full pipeline load.  The session key is 'dreamer-{project_name}'.
    """
    from app.agent.config import (
        DREAMER_ENABLED, DREAMER_STALL_TICKS, SCHEDULER_TICK_INTERVAL,
    )
    if not DREAMER_ENABLED:
        return
    if is_shutting_down():
        return

    from app.database import get_all_projects, get_tasks_by_project, get_llm
    from app.database import get_transition_results
    from app.database.session import SessionLocal
    from app.database.models import TransitionResult, Task

    stall_threshold_secs = DREAMER_STALL_TICKS * SCHEDULER_TICK_INTERVAL
    now = time.time()

    projects = get_all_projects()
    for project in projects:
        project_name = project.name

        # Skip projects without an LLM or budget configured
        if not project.llm_id or not project.budget_id:
            continue

        # Skip if a Dreamer is already running for this project
        with _active_dreamer_lock:
            if project_name in _active_dreamer_projects:
                continue

        # Determine last pipeline activity time for this project via DB
        try:
            db = SessionLocal()
            try:
                # Find the max created_at of any TransitionResult whose task belongs to this project
                result = (
                    db.query(TransitionResult.created_at)
                      .join(Task, Task.id == TransitionResult.task_id)
                      .filter(Task.project_id == project.id, Task.is_active == True)
                      .order_by(TransitionResult.created_at.desc())
                      .first()
                )
            finally:
                db.close()
            last_tr_time: float | None = None
            if result and result[0]:
                tr_dt = result[0]
                if hasattr(tr_dt, "timestamp"):
                    last_tr_time = tr_dt.timestamp()
                else:
                    # Stored as ISO string in some rows
                    try:
                        last_tr_time = datetime.fromisoformat(str(tr_dt)).timestamp()
                    except Exception:
                        last_tr_time = None
        except Exception as exc:
            logger.debug("[Dreamer] Activity query failed for '%s': %s", project_name, exc)
            continue

        # Initialise the grace-period clock on first sight
        with _project_last_activity_lock:
            if project_name not in _project_last_activity:
                _project_last_activity[project_name] = last_tr_time or now
            # Update stored value if DB shows more recent activity
            if last_tr_time and last_tr_time > _project_last_activity[project_name]:
                _project_last_activity[project_name] = last_tr_time
            last_activity = _project_last_activity[project_name]

        if (now - last_activity) < stall_threshold_secs:
            continue  # project is active — skip

        # Guard: only fire when the project has substantive signal to work with.
        # An empty project (no files, no tasks, no arch cards) has no intent for
        # the Dreamer to latch onto.  The Dreamer's own survey mode handles the
        # distinction between "has files but no active tasks" vs "truly empty".
        if not _project_has_dreamer_signal(project):
            logger.debug(
                "[Dreamer] Skipping '%s' — no signal (empty project: no files, "
                "no tasks, no arch cards).",
                project_name,
            )
            continue

        # Fire Dreamer
        llm = get_llm(project.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time: respect the pinned LLM for this tick
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            logger.debug(
                "[Dreamer] Skipping '%s' — LLM %d not pinned (pinned=%s).",
                project_name, llm.id, allowed_llm_id,
            )
            continue

        # Check and reserve a capacity slot — Dreamers must not bypass limits
        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"dreamer '{project_name}'",
        ):
            logger.debug(
                "[Dreamer] Skipping '%s' — LLM %d at capacity.", project_name, llm.id,
            )
            continue

        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        llm_model    = llm.model

        logger.info(
            "[Dreamer] Project '%s' stalled for %.0fs — starting DreamerAgent (LLM %d).",
            project_name, now - last_activity, llm.id,
        )

        with _active_dreamer_lock:
            _active_dreamer_projects.add(project_name)
        # Reset the activity clock so we don't fire again immediately after completion
        with _project_last_activity_lock:
            _project_last_activity[project_name] = now

        _start_dreamer_thread(
            project_name=project_name,
            project_path=project.path,
            llm_id=project.llm_id,
            budget_id=project.budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
        )


def _start_dreamer_thread(
    project_name: str,
    project_path: "str | None",
    llm_id: int,
    budget_id: int,
    llm_base_url: str,
    llm_model: str,
) -> None:
    """Spawn a daemon thread that runs DreamerAgent.run() for one project.

    The slot was already reserved by _check_and_reserve_slot in _dispatch_dreamer.
    Here we register the thread in _active_sessions / _session_llm_ids so that:
      - _cleanup_finished() can spot when it dies and decrement _llm_session_counts
      - the one-LLM-at-a-time policy pins the Dreamer's LLM for the duration
      - scheduler status endpoints show the Dreamer as an active session
    """
    import asyncio as _asyncio
    from app.agent.dreamer import DreamerAgent

    session_key = f"dreamer-{project_name}"

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            agent = DreamerAgent(
                project_name=project_name,
                project_path=project_path,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
            loop.run_until_complete(agent.run())
        except Exception as exc:
            logger.exception("[Dreamer] Thread for '%s' raised: %s", project_name, exc)
        finally:
            loop.close()
            # Release the capacity slot — mirrors what _cleanup_finished does for
            # normal sessions, but the dreamer does it explicitly so the count
            # drops immediately when the thread exits rather than waiting for the
            # next cleanup pass.
            with _llm_counts_lock:
                _llm_session_counts[llm_id] = max(0, _llm_session_counts[llm_id] - 1)
            with _active_sessions_lock:
                _active_sessions.pop(session_key, None)
                _session_llm_ids.pop(session_key, None)
                _session_titles.pop(session_key, None)
            with _active_dreamer_lock:
                _active_dreamer_projects.discard(project_name)
            logger.debug(
                "[Dreamer] Thread for '%s' exited (LLM %d slot released).",
                project_name, llm_id,
            )

    t = threading.Thread(target=_run, daemon=True, name=f"dreamer-{project_name[:24]}")
    # Register in the session tracking so the scheduler sees this as an active slot
    with _active_sessions_lock:
        _active_sessions[session_key] = t
        _session_llm_ids[session_key] = llm_id
        _session_titles[session_key] = f"Dreamer: {project_name}"
    t.start()


def _dispatch_file_summary_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> "int | None":
    """Dispatch pending file summary jobs - top priority, agents are blocked waiting.

    Respects the one-LLM-at-a-time policy and full node/LLM capacity caps.
    Returns the (possibly updated) allowed_llm_id so the caller can propagate
    the pin to subsequent dispatch phases.
    """
    from app.database import get_pending_file_summary_jobs, update_file_summary_job, get_llm, get_task as _get_task

    pending = get_pending_file_summary_jobs(limit=20)
    for job in pending:
        if not job.llm_id:
            continue

        # Throttling: Skip projects with recent failures/rescues
        if job.task_id:
            task = _get_task(job.task_id)
            if task and task.project and _is_project_in_failure_cooldown(task.project):
                logger.debug(
                    "Skipping file_summary job %d - project '%s' is in failure cooldown.",
                    job.id, task.project,
                )
                continue

        job_key = f"file-summary-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time gate
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"file-summary-{job.id}",
        ):
            continue

        update_file_summary_job(job.id, status='running')

        thread = threading.Thread(
            target=_run_file_summary_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-file-summary-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"File Summary: {os.path.basename(job.file_path)}"
        thread.start()

    return allowed_llm_id


def _is_log_like_path(path: str) -> bool:
    """Return True for log files and rotated log files (e.g. .log, .log.1, .log.2)."""
    import re as _re
    return bool(_re.search(r'\.log(\.\d+)?$', path, _re.IGNORECASE))


def _run_file_summary_job(job: Any, llm: Any) -> None:
    """Execute a single file summary job in its own thread + event loop."""
    from app.database import update_file_summary_job
    from app.agent.file_summary_agent import execute_file_summary

    completion_key = f"file_summary:{job.sha1_hash}:{job.file_size_bytes}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Reject oversized files and log files — these are not worth summarising
        # and can generate hundreds of LLM calls via the sliding-window chunker.
        from app.agent.config import SUMMARY_MAX_FILE_SIZE
        if job.file_size_bytes > SUMMARY_MAX_FILE_SIZE:
            logger.info(
                "[file_summary] Cancelling oversized job %d '%s' (%d bytes > %d byte cap).",
                job.id, job.file_path, job.file_size_bytes, SUMMARY_MAX_FILE_SIZE,
            )
            update_file_summary_job(job.id, status="cancelled",
                                    error_message=f"File too large: {job.file_size_bytes} bytes")
            signal_completion(completion_key)
            return
        if _is_log_like_path(job.file_path):
            logger.info(
                "[file_summary] Cancelling log file job %d '%s' — log files are not summarised.",
                job.id, job.file_path,
            )
            update_file_summary_job(job.id, status="cancelled",
                                    error_message="Log file excluded from summarisation")
            signal_completion(completion_key)
            return

        # Reject binary files before doing any LLM work — binary data cannot be
        # summarised as text and would produce garbage or overflow the context.
        try:
            with open(job.file_path, "rb") as _bin_fh:
                _header = _bin_fh.read(512)
            if b"\x00" in _header:
                logger.info(
                    "[file_summary] Skipping binary file '%s' — null bytes detected.",
                    job.file_path,
                )
                update_file_summary_job(job.id, status="completed")
                signal_completion(completion_key)
                return
        except OSError:
            pass  # File gone — fall through to existing content

        # Re-read the full file from disk so chunked processing sees complete content.
        # The stored job.file_content is capped at 32k; fall back to it if file is gone.
        try:
            with open(job.file_path, "r", encoding="utf-8", errors="replace") as _fh:
                full_content = _fh.read()
        except OSError:
            full_content = job.file_content

        from app.agent.config import FILE_SUMMARY_STREAM_IDLE_TIMEOUT
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
            stream_idle_timeout=FILE_SUMMARY_STREAM_IDLE_TIMEOUT,
            max_context=getattr(llm, 'max_context', 0),
        ))
        update_file_summary_job(
            job.id,
            status='completed',
            prompt_tokens=result.get('prompt_tokens', 0),
            completion_tokens=result.get('completion_tokens', 0),
        )
        logger.debug("file_summary job %d completed (sha1=%s…)", job.id, job.sha1_hash[:8])
    except ShutdownError:
        logger.info("File summary job %d aborted due to server shutdown.", job.id)
        update_file_summary_job(job.id, status='failed')
    except Exception:
        logger.exception("File summary job %d failed.", job.id)
        update_file_summary_job(job.id, status='failed')
        if job.task_id:
            from app.database import get_task as _get_task
            task = _get_task(job.task_id)
            if task and task.project:
                _record_project_failure(task.project)
    finally:
        # Release slot and signal BEFORE loop cleanup - shutdown_asyncgens() can
        # hang indefinitely on unclosed streaming generators, which would leave
        # _llm_session_counts permanently incremented and starve the scheduler.
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        signal_completion(completion_key)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _dispatch_research_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Dispatch pending research jobs that have an LLM assigned.

    Respects the one-LLM-at-a-time policy and full node/LLM capacity caps.
    """
    from app.database import get_pending_research_jobs, update_research_job, get_llm, get_task as _get_task

    pending = get_pending_research_jobs(limit=10)
    for job in pending:
        if not job.llm_id:
            continue

        # Throttling: Skip projects with recent failures/rescues
        task = _get_task(job.task_id)
        if task and task.project and _is_project_in_failure_cooldown(task.project):
            logger.debug(
                "Skipping research job %d - project '%s' is in failure cooldown.",
                job.id, task.project,
            )
            continue

        job_key = f"research-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time gate
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"research-{job.id}",
        ):
            continue

        thread = threading.Thread(
            target=_run_research_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-research-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Research: {job.question[:60]}..." if len(job.question) > 60 else f"Research: {job.question}"
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
            project_root=_get_project_path(task.project) if task and task.project else None,
        ))
        update_research_job(
            job.id, status="completed",
            verdict=json.dumps(result.vote),
            findings=result.findings,
            lives_used=result.lives_used,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
        )
    except ShutdownError:
        logger.info("Research job %d aborted due to server shutdown.", job.id)
        update_research_job(job.id, status="failed")
    except Exception:
        logger.exception("Research job %d failed in scheduler.", job.id)
        update_research_job(job.id, status="failed")
        if task and task.project:
            _record_project_failure(task.project)
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        # Wake any agent that called launch_research_agent and is parked waiting.
        signal_completion(f"research_job_{job.id}")
        with _active_sessions_lock:
            _active_sessions.pop(f"research-{job.id}", None)
            _session_llm_ids.pop(f"research-{job.id}", None)
            _session_titles.pop(f"research-{job.id}", None)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _dispatch_arch_gen_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Dispatch pending arch gen jobs - fire-and-forget card generation from file summaries.

    Lower priority than research (1.0 vs 0.0).  Respects the one-LLM-at-a-time
    policy and full node/LLM capacity caps.
    """
    from app.database import get_pending_arch_gen_jobs, update_arch_gen_job, get_llm

    pending = get_pending_arch_gen_jobs(limit=5)
    for job in pending:
        if not job.llm_id:
            continue

        # Throttling: Skip projects with recent failures/rescues
        if _is_project_in_failure_cooldown(job.project):
            logger.debug(
                "Skipping arch_gen job %d - project '%s' is in failure cooldown.",
                job.id, job.project,
            )
            continue

        job_key = f"arch-gen-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time gate
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"arch-gen-{job.id}",
        ):
            continue

        update_arch_gen_job(job.id, status='running')

        thread = threading.Thread(
            target=_run_arch_gen_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-arch-gen-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Arch Gen: {job.project} ({job.category})"
        thread.start()


def _dispatch_scope_survey_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Dispatch pending ScopeSurveyJobs. Runs after arch gen jobs."""
    from app.database import get_pending_scope_survey_jobs, get_llm, update_scope_survey_job
    from app.agent.config import SURVEY_MAX_CONCURRENT_JOBS

    pending = get_pending_scope_survey_jobs(limit=SURVEY_MAX_CONCURRENT_JOBS)
    for job in pending:
        if not job.llm_id:
            continue

        job_key = f"scope-survey-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time gate
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"scope-survey-{job.id}",
        ):
            continue

        update_scope_survey_job(job.id, status='running')

        thread = threading.Thread(
            target=_run_scope_survey_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-scope-survey-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Survey: {job.project_name} ({job.scope_key})"
        thread.start()


def _run_scope_survey_job(job: Any, llm: Any) -> None:
    """Worker thread for a single scope survey job.
    
    Implements recursive 'paging' for large scopes and bottom-up chaining.
    """
    from app.database import (
        update_scope_survey_job, get_file_summaries_for_project_root,
        get_project_path, list_scope_summaries, upsert_scope_summary,
        get_scope_summary, create_agent_session, close_agent_session,
        enqueue_scope_survey_job, get_pending_scope_survey_jobs
    )
    from app.agent.llm_client import call_llm
    from app.agent.survey_orchestrator import SurveyOrchestrator

    orchestrator = SurveyOrchestrator()
    session_key = f"scope-survey-{job.id}"
    _session_id = create_agent_session(
        task_id=f"survey-{job.id}",
        agent_type="survey",
        llm_id=llm.id,
        budget_id=job.budget_id,
        scheduler_reason="scheduler",
    )
    
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        project_root = get_project_path(job.project_name)
        if not project_root:
            raise RuntimeError(f"Project '{job.project_name}' root not found.")

        # --- Staleness & Patching Logic ---
        if job.action == "staleness_check":
            existing = get_scope_summary(job.project_name, job.scope_type, job.scope_key)
            if not existing:
                update_scope_survey_job(job.id, action="generate", status="pending")
                close_agent_session(_session_id, "completed", "No existing summary, switching to generate")
                return

            # Get diff for the files in this scope
            from app.agent.tools import run_shell
            diff_text = ""
            if existing.file_paths:
                file_list = json.loads(existing.file_paths)
                # Cap diff size
                diff_cmd = f"git diff {existing.git_commit or 'HEAD^'}..HEAD -- " + " ".join(file_list[:20])
                diff_text = loop.run_until_complete(run_shell(diff_cmd, cwd=project_root))

            prompt = (
                f"Here is the current summary for the '{job.scope_key}' {job.scope_type} in project '{job.project_name}':\n"
                f"{existing.summary}\n\n"
                f"Here is the git diff since it was generated:\n{diff_text[:3000]}\n\n"
                "Does this diff require (a) a full re-summarization (FULL_REINGEST), "
                "(b) a minor edit to the summary (EDIT_SUMMARY), or (c) no change (NO_CHANGE)? "
                "Answer with exactly one of those three words."
            )
            resp = loop.run_until_complete(call_llm(
                messages=[{"role": "user", "content": prompt}],
                base_url=f"http://{llm.address}:{llm.port}/v1", model=llm.model,
                llm_id=llm.id, budget_id=job.budget_id,
                max_tokens=SURVEY_VERDICT_MAX_TOKENS
            ))
            verdict = resp.get("content", "").strip().upper()
            if "FULL_REINGEST" in verdict:
                update_scope_survey_job(job.id, action="generate", status="pending")
            elif "EDIT_SUMMARY" in verdict:
                update_scope_survey_job(job.id, action="edit_summary", status="pending")
            else:
                upsert_scope_summary(
                    project_name=job.project_name, scope_type=job.scope_type, scope_key=job.scope_key,
                    summary=existing.summary, staleness_state="fresh", git_commit="HEAD"
                )
                update_scope_survey_job(job.id, status="done")
            
            close_agent_session(_session_id, "completed", f"Staleness check: {verdict}")
            return

        if job.action == "edit_summary":
            existing = get_scope_summary(job.project_name, job.scope_type, job.scope_key)
            if not existing:
                update_scope_survey_job(job.id, action="generate", status="pending")
                close_agent_session(_session_id, "completed", "No existing summary, switching to generate")
                return

            # Get diff for the files in this scope
            from app.agent.tools import run_shell
            diff_text = ""
            if existing.file_paths:
                file_list = json.loads(existing.file_paths)
                diff_cmd = f"git diff {existing.git_commit or 'HEAD^'}..HEAD -- " + " ".join(file_list[:20])
                diff_text = loop.run_until_complete(run_shell(diff_cmd, cwd=project_root))

            prompt = (
                f"You are updating the summary for the '{job.scope_key}' {job.scope_type} in project '{job.project_name}'.\n"
                f"Old Summary:\n{existing.summary}\n\n"
                f"Git Diff of changes:\n{diff_text[:4000]}\n\n"
                "Please provide an updated summary that incorporates these changes. Maintain the same style. "
                "Provide a detailed summary and a 2-sentence 'short_summary' at the very end of your response, "
                "prefixed with 'SHORT_SUMMARY: '."
            )
            resp = loop.run_until_complete(call_llm(
                messages=[{"role": "user", "content": prompt}],
                base_url=f"http://{llm.address}:{llm.port}/v1", model=llm.model,
                llm_id=llm.id, budget_id=job.budget_id,
                max_tokens=SURVEY_SUMMARY_MAX_TOKENS
            ))

            content = resp.get("content", "")
            summary = content
            short = ""
            if "SHORT_SUMMARY: " in content:
                parts = content.split("SHORT_SUMMARY: ")
                summary = parts[0].strip()
                short = parts[1].strip()
            else:
                short = content.split(". ")[0] + "." if ". " in content else content[:200]

            upsert_scope_summary(
                project_name=job.project_name, scope_type=job.scope_type, scope_key=job.scope_key,
                summary=summary, short_summary=short, staleness_state="fresh", git_commit="HEAD"
            )
            update_scope_survey_job(job.id, status="done")
            close_agent_session(_session_id, "completed", "Summary updated via patch")
            return

        # 1. Gather child items
        children = [] # List of strings/dicts representing inputs
        if job.scope_type == "directory":
            all_files = get_file_summaries_for_project_root(project_root)
            for f in all_files:
                rel_path = os.path.relpath(f.file_path, project_root).replace("\\", "/")
                rel_dir = os.path.dirname(rel_path)
                if rel_dir == ".": rel_dir = ""
                if rel_dir == job.scope_key:
                    children.append({
                        "id": rel_path,
                        "text": f.short_summary or f.summary or ""
                    })
        
        elif job.scope_type == "project":
            # Project summarizes Directories AND Modules
            all_scopes = list_scope_summaries(job.project_name)
            for s in all_scopes:
                if s.scope_type in ("directory", "module") and s.staleness_state == "fresh":
                    children.append({
                        "id": f"[{s.scope_type}] {s.scope_key}",
                        "text": s.short_summary or s.summary or ""
                    })

        elif job.scope_type == "module":
            # Module summarizes its specific files
            existing = get_scope_summary(job.project_name, "module", job.scope_key)
            if existing and existing.file_paths:
                file_list = json.loads(existing.file_paths)
                all_files = get_file_summaries_for_project_root(project_root)
                f_map = {os.path.relpath(f.file_path, project_root).replace("\\", "/"): f for f in all_files}
                for path in file_list:
                    f = f_map.get(path)
                    if f:
                        children.append({
                            "id": path,
                            "text": f.short_summary or f.summary or ""
                        })

        elif job.scope_type == "module_clustering":
            # Clustering needs ALL file summaries
            all_files = get_file_summaries_for_project_root(project_root)
            for f in all_files:
                rel_path = os.path.relpath(f.file_path, project_root).replace("\\", "/")
                children.append({
                    "id": rel_path,
                    "text": f.short_summary or f.summary or ""
                })

        # --- Recursive Partitioning (The "Pages" Strategy) ---
        branch_factor = orchestrator.get_branching_factor(llm.max_context)

        # Special case: module_clustering is one-shot (LLM must see everything to cluster)
        if job.scope_type != "module_clustering" and len(children) > branch_factor:
            import math
            page_count = math.ceil(len(children) / branch_factor)
            page_scope_type = f"{job.scope_type}_page"

            # Check whether page jobs for this parent already exist (any status).
            # On first partition: no rows → create them and log.
            # On subsequent ticks (LLM down / retrying): rows exist → don't re-create,
            # don't spam the log.  Reset any failed pages to pending so they retry via
            # call_llm's own backoff rather than accumulating duplicate rows each tick.
            from app.database import get_scope_survey_page_jobs
            existing_pages = get_scope_survey_page_jobs(
                job.project_name, page_scope_type, job.scope_key
            )

            if not existing_pages:
                # First partition: create page jobs and log.
                logger.info(
                    "[Survey] Partitioning %s '%s' into %d pages.",
                    job.scope_type, job.scope_key, page_count,
                )
                for i in range(page_count):
                    page_key = f"{job.scope_key}:page-{i + 1}"
                    enqueue_scope_survey_job(
                        job.project_name, page_scope_type, page_key,
                        action="generate", priority=job.priority - 0.1,
                        llm_id=llm.id, budget_id=job.budget_id,
                    )
            else:
                # Pages already exist — reset any that failed so they retry.
                failed_pages = [p for p in existing_pages if p.status == "failed"]
                if failed_pages:
                    logger.debug(
                        "[Survey] %d/%d page job(s) for '%s' failed; resetting to pending for retry.",
                        len(failed_pages), len(existing_pages), job.scope_key,
                    )
                    for p in failed_pages:
                        update_scope_survey_job(
                            p.id, status="pending",
                            retry_count=p.retry_count + 1,
                            error_message=None,
                        )

            update_scope_survey_job(job.id, status="pending", error_message=f"Waiting for {page_count} pages")
            close_agent_session(_session_id, "completed", "Waiting for page jobs")
            return

        # 2. Check if children are ready (some might be pages or missing file summaries)
        if not children:
            if job.scope_type == "project":
                # Check if we should wait for Level 1 jobs
                pending_seeds = get_pending_scope_survey_jobs(limit=1)
                if pending_seeds:
                    update_scope_survey_job(job.id, status="pending", priority=job.priority + 0.1)
                    close_agent_session(_session_id, "completed", "Waiting for Level 1 seeds")
                    return
            
            update_scope_survey_job(job.id, status="done", error_message="No children found to summarize")
            close_agent_session(_session_id, "completed", "No children")
            return

        # 3. Build prompt
        # Use orchestrator to determine how many chars we can afford per child.
        total_limit = orchestrator.get_summary_context_limit(llm.max_context)
        per_child_limit = max(200, int(total_limit / max(1, len(children))))
        
        child_block = "\n".join([f"- {c['id']}: {c['text'][:per_child_limit]}" for c in children])
        
        if job.scope_type == "module_clustering":
            prompt = (
                f"Given these file summaries for project '{job.project_name}', group them into 3-8 logical modules. "
                "A module may span multiple directories. For each module, provide a name, a 2-sentence purpose, "
                "and the list of files it contains.\n"
                "Output as JSON: [{\"name\": \"...\", \"purpose\": \"...\", \"files\": [\"...\", ...]}, ...]\n\n"
                "Files:\n" + child_block
            )
        else:
            prompt = (
                f"Summarize the health, purpose, and key components of the '{job.scope_key}' {job.scope_type} "
                f"in project '{job.project_name}'. Provide a detailed summary and a 2-sentence 'short_summary' "
                "at the very end of your response, prefixed with 'SHORT_SUMMARY: '.\n\n"
                "Child components:\n" + child_block
            )

        # 4. Call LLM
        resp = loop.run_until_complete(call_llm(
            messages=[{"role": "user", "content": prompt}],
            base_url=f"http://{llm.address}:{llm.port}/v1",
            model=llm.model,
            llm_id=llm.id,
            budget_id=job.budget_id,
            max_tokens=SURVEY_SUMMARY_MAX_TOKENS,
        ))

        content = resp.get("content", "")
        if job.scope_type == "module_clustering":
            try:
                # Basic JSON extraction
                if "```json" in content:
                    content = content.split("```json")[1].split("```")[0].strip()
                elif "```" in content:
                    content = content.split("```")[1].split("```")[0].strip()
                
                modules = json.loads(content)
                for m in modules:
                    upsert_scope_summary(
                        project_name=job.project_name,
                        scope_type="module",
                        scope_key=m["name"],
                        summary=m["purpose"],
                        short_summary=m["purpose"],
                        file_paths=m.get("files", []),
                        file_count=len(m.get("files", [])),
                        llm_id=llm.id,
                        budget_id=job.budget_id
                    )
                    # Enqueue the actual summary job for this newly created module
                    enqueue_scope_survey_job(
                        job.project_name, "module", m["name"],
                        action="generate", priority=job.priority + 0.1,
                        llm_id=llm.id, budget_id=job.budget_id
                    )
            except Exception as e:
                logger.error(f"Failed to parse module clustering JSON: {e}\nContent: {content[:500]}")
        else:
            summary = content
            short = ""
            if "SHORT_SUMMARY: " in content:
                parts = content.split("SHORT_SUMMARY: ")
                summary = parts[0].strip()
                short = parts[1].strip()
            else:
                short = content.split(". ")[0] + "." if ". " in content else content[:200]
            
            upsert_scope_summary(
                project_name=job.project_name,
                scope_type=job.scope_type,
                scope_key=job.scope_key,
                summary=summary,
                short_summary=short,
                file_count=len(children),
                llm_id=llm.id,
                budget_id=job.budget_id
            )

        update_scope_survey_job(
            job.id, status="done",
            prompt_tokens=resp.get("prompt_tokens", 0),
            completion_tokens=resp.get("completion_tokens", 0)
        )
        close_agent_session(_session_id, "completed", "Survey generation complete",
                            prompt_tokens=resp.get("prompt_tokens", 0),
                            completion_tokens=resp.get("completion_tokens", 0))

        # Bottom-up Chaining: Check if we can trigger the next level
        if job.scope_type in ("directory", "module", "directory_page", "module_page"):
            # Check if all siblings are done to trigger the Project summary
            # (In a real implementation, we'd check if all non-'project' jobs are done)
            pass

    except ShutdownError:
        update_scope_survey_job(job.id, status='failed', error_message="Server shutdown")
        close_agent_session(_session_id, "shutdown", "Server shutdown")
    except Exception as e:
        logger.exception("Scope survey job %d failed.", job.id)
        update_scope_survey_job(job.id, status='failed', error_message=str(e))
        close_agent_session(_session_id, "error", str(e))
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        signal_completion(session_key)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_arch_gen_job(job: Any, llm: Any) -> None:
    """Execute a single arch gen job in its own thread + event loop."""
    from app.database import update_arch_gen_job, get_project_path as _get_project_path

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        project_root = _get_project_path(job.project)
        if not project_root:
            raise RuntimeError(f"Project '{job.project}' has no path configured.")

        from app.agent.arch_gen_agent import execute_arch_gen_job
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        result = loop.run_until_complete(execute_arch_gen_job(
            project=job.project,
            category=job.category,
            project_root=project_root,
            llm_id=job.llm_id,
            budget_id=job.budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm.model,
            max_context=getattr(llm, 'max_context', None),
        ))
        update_arch_gen_job(
            job.id,
            status='completed',
            prompt_tokens=result.get('prompt_tokens', 0),
            completion_tokens=result.get('completion_tokens', 0),
        )
        logger.debug("[arch_gen] job %d completed (project=%s category=%s).", job.id, job.project, job.category)
    except ShutdownError:
        logger.info("[arch_gen] job %d aborted due to server shutdown.", job.id)
        update_arch_gen_job(job.id, status='failed', error_message="Server shutdown")
    except Exception as e:
        logger.exception("[arch_gen] job %d failed.", job.id)
        update_arch_gen_job(job.id, status='failed', error_message=str(e))
        _record_project_failure(job.project)
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


# ===========================================================================
# PIP Pre-flight Gate + Resolution Job Dispatch
# ===========================================================================

def _run_pip_preflight_and_gate(
    task_id: str,
    stage: str,
    llm_id: int,
    budget_id: int,
    project_path: "str | None",
    loop: "asyncio.AbstractEventLoop",
) -> bool:
    """Run pre-flight PIP verification for all PIPs on the task at the given stage.

    Returns True  — all PIPs passed (or no PIPs); the stage pipeline may proceed.
    Returns False — one or more PIPs failed; pip_resolution_jobs were created and
                    the stage pipeline must NOT run.
    """
    from app.database import get_pips_for_task, create_agent_session, close_agent_session
    from app.agent.pip_agent import run_pip_preflight

    pips = get_pips_for_task(task_id)
    if not pips:
        return True

    logger.info(
        "[pip_preflight] Task '%s' has %d PIP(s) — running pre-flight for stage '%s'.",
        task_id, len(pips), stage,
    )

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="pip_preflight",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )

    preflight = loop.run_until_complete(
        run_pip_preflight(task_id, stage, llm_id, budget_id, project_path)
    )

    if preflight["all_passed"]:
        n = len(preflight["results"])
        logger.info(
            "[pip_preflight] Task '%s' — all PIPs passed at stage '%s'. Proceeding.",
            task_id, stage,
        )
        close_agent_session(
            _session_id, "passed",
            exit_summary=f"All {n}/{n} PIPs passed at stage '{stage}'.",
        )
        return True

    failed = [r for r in preflight["results"] if r["outcome"] != "passed"]
    total = len(preflight["results"])
    snippets = "; ".join(r.get("summary", "")[:80] for r in failed[:3])
    logger.warning(
        "[pip_preflight] Task '%s' — %d PIP(s) failed at stage '%s'. Scheduling resolution.",
        task_id, len(failed), stage,
    )
    close_agent_session(
        _session_id, "pip_blocked",
        exit_summary=f"{len(failed)}/{total} PIPs failed at stage '{stage}': {snippets}",
    )
    _schedule_pip_resolution_jobs(task_id, failed, stage)
    return False


def _schedule_pip_resolution_jobs(task_id: str, failed_results: list, stage: str) -> None:
    """Create pip_resolution_job rows for each failed PIP (idempotent)."""
    from app.database import create_pip_resolution_job
    for result in failed_results:
        job = create_pip_resolution_job(task_id, result["pip_id"], stage)
        if job:
            logger.info(
                "[pip_preflight] Created pip_resolution_job %s for task '%s' pip %d blocked at '%s'.",
                job.id, task_id, result["pip_id"], stage,
            )


def _dispatch_pip_resolution_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Dispatch pending PIP resolution jobs through research → resolution pipeline.

    Job lifecycle:
      pending     — create research thread (direct LLM call, finds what work is needed)
      researching — wait for research completion signal, then dispatch PIPResolutionAgent
      resolving   — wait for resolution completion signal, then mark done
      done/failed — terminal; no further action
    """
    from app.database import (
        get_pending_pip_resolution_jobs, update_pip_resolution_job,
        get_task, get_pips_for_task, get_llm,
    )

    jobs = get_pending_pip_resolution_jobs(limit=10)
    for job in jobs:
        task = get_task(job.task_id)
        if not task or not task.llm_id:
            continue

        llm = get_llm(task.llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if job.status == "pending":
            job_key = f"pip-research-{job.pip_id}"
            with _active_sessions_lock:
                if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                    continue

            if not _check_and_reserve_slot(
                llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
                label=f"pip-research-{job.pip_id}",
            ):
                continue

            update_pip_resolution_job(job.id, status="researching")
            thread = threading.Thread(
                target=_run_pip_resolution_research,
                args=(job, task, llm),
                daemon=True,
                name=f"maestro-pip-research-{job.pip_id}",
            )
            with _active_sessions_lock:
                _active_sessions[job_key] = thread
                _session_llm_ids[job_key] = llm.id
                _session_titles[job_key] = f"PIP Research: task {job.task_id} pip {job.pip_id}"
            thread.start()

        elif job.status == "researching":
            # Check whether the research thread signalled completion
            research_key = f"pip_research_{job.pip_id}"
            event, existed = get_or_create_completion_event(research_key)
            if not (existed and event.is_set()):
                continue  # still running

            # Research done — dispatch the PIPResolutionAgent
            resolve_key = f"pip-resolve-{job.pip_id}"
            with _active_sessions_lock:
                if resolve_key in _active_sessions and _active_sessions[resolve_key].is_alive():
                    continue  # already dispatched

            if not _check_and_reserve_slot(
                llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
                label=resolve_key,
            ):
                continue  # no capacity this tick

            update_pip_resolution_job(job.id, status="resolving")
            thread = threading.Thread(
                target=_run_pip_resolution_agent,
                args=(job, task, llm),
                daemon=True,
                name=f"maestro-pip-resolve-{job.pip_id}",
            )
            with _active_sessions_lock:
                _active_sessions[resolve_key] = thread
                _session_llm_ids[resolve_key] = llm.id
                _session_titles[resolve_key] = (
                    f"PIP Resolution: task {job.task_id} pip {job.pip_id}"
                )
            thread.start()
            logger.info(
                "[pip_resolution] Dispatched resolution agent for pip %d (task '%s').",
                job.pip_id, job.task_id,
            )

        elif job.status == "resolving":
            resolve_key = f"pip_resolution_{job.pip_id}"
            event, existed = get_or_create_completion_event(resolve_key)
            if not (existed and event.is_set()):
                continue
            update_pip_resolution_job(job.id, status="done")
            logger.info(
                "[pip_resolution] Resolution agent complete for pip %d (task '%s').",
                job.pip_id, job.task_id,
            )


def _run_pip_resolution_research(job: Any, task: Any, llm: Any) -> None:
    """Research what concrete work is needed to satisfy a failed PIP.

    Runs a single focused LLM call (no full ResearchAgent to keep it lightweight).
    Stores findings in the pip_resolution_job row and signals completion.
    """
    from app.database import (
        update_pip_resolution_job, get_pips_for_task,
        get_latest_pip_verification, get_project_path as _get_project_path,
        create_agent_session, close_agent_session,
    )
    from app.agent.project_snapshot import build_project_snapshot

    research_key = f"pip_research_{job.pip_id}"
    job_key = f"pip-research-{job.pip_id}"

    _session_id = create_agent_session(
        task_id=job.task_id,
        agent_type="pip_research",
        llm_id=getattr(task, "llm_id", None),
        budget_id=getattr(task, "budget_id", None),
        scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pips = get_pips_for_task(job.task_id)
        pip = next((p for p in pips if p.id == job.pip_id), None)
        if not pip:
            logger.warning("[pip_resolution] pip %d not found for task '%s'.", job.pip_id, job.task_id)
            update_pip_resolution_job(job.id, status="failed")
            return

        project_root = _get_project_path(task.project) if task.project else None
        snapshot = build_project_snapshot(project_root) if project_root else ""
        last_v = get_latest_pip_verification(pip.id, job.stage_blocked_at)
        last_findings = last_v.findings if last_v else "[]"

        import json as _json
        reqs = _json.loads(pip.requirements) if isinstance(pip.requirements, str) else pip.requirements
        req_text = "\n".join(f"- {r}" for r in reqs)

        prompt = (
            "Investigate what concrete work needs to be done to satisfy the following requirement. "
            "Do not implement anything — produce a findings report only.\n\n"
            f"PIP REQUIREMENT:\n{req_text}\n\n"
            f"WHAT FAILED IN LAST VERIFICATION:\n{last_findings}\n\n"
            f"PROJECT SNAPSHOT:\n{snapshot}"
        )

        from app.agent.llm_client import call_llm as _call_llm
        _resp = loop.run_until_complete(
            _call_llm(
                messages=[{"role": "user", "content": prompt}],
                llm_id=task.llm_id,
                budget_id=task.budget_id,
            )
        )
        response_text = _resp.get("content", "")
        update_pip_resolution_job(job.id, research_findings=response_text)
        _exit_reason = "completed"
        _exit_summary = f"Research findings recorded for pip {job.pip_id}."
        logger.info("[pip_resolution] Research complete for pip %d.", job.pip_id)

    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = f"Server shutdown during pip research for pip {job.pip_id}."
        logger.info("[pip_resolution] Research for pip %d aborted (shutdown).", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    except Exception:
        _exit_reason = "error"
        _exit_summary = f"Exception during pip research for pip {job.pip_id}."
        logger.exception("[pip_resolution] Research for pip %d failed.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary)
        signal_completion(research_key)
        with _active_sessions_lock:
            _active_sessions.pop(job_key, None)
            _session_llm_ids.pop(job_key, None)
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_pip_resolution_agent(job: Any, task: Any, llm: Any) -> None:
    """Run the PIPResolutionAgent for a single failed PIP.

    Dispatched as a daemon thread by _dispatch_pip_resolution_jobs() once
    research findings are available.  Signals pip_resolution_{pip_id} on
    every exit path so the scheduler can re-dispatch the parent stage.
    """
    from app.database import (
        update_pip_resolution_job, get_pips_for_task,
        get_latest_pip_verification, get_project_path as _get_project_path,
        get_llm, create_agent_session, close_agent_session,
    )
    from app.agent.pip_resolution import PIPResolutionAgent
    from app.agent.config import MAX_TURNS as _PIP_MAX_TURNS

    resolve_key = f"pip-resolve-{job.pip_id}"
    completion_key = f"pip_resolution_{job.pip_id}"

    _session_id = create_agent_session(
        task_id=job.task_id,
        agent_type="pip_resolution",
        llm_id=getattr(task, "llm_id", None),
        budget_id=getattr(task, "budget_id", None),
        scheduler_reason="scheduler",
        max_turns=_PIP_MAX_TURNS,
    )
    _exit_reason = "error"
    _exit_summary = ""
    _turn_count = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        pips = get_pips_for_task(job.task_id)
        pip = next((p for p in pips if p.id == job.pip_id), None)
        if not pip:
            logger.warning(
                "[pip_resolution] pip %d not found for task '%s'.",
                job.pip_id, job.task_id,
            )
            update_pip_resolution_job(job.id, status="failed")
            return

        project_root = _get_project_path(task.project) if task.project else None

        import json as _json
        reqs = _json.loads(pip.requirements) if isinstance(pip.requirements, str) else pip.requirements

        last_v = get_latest_pip_verification(pip.id, job.stage_blocked_at)
        last_findings: list[dict] = []
        if last_v and last_v.findings:
            try:
                last_findings = _json.loads(last_v.findings)
            except Exception:
                pass

        llm_obj = get_llm(task.llm_id) if task.llm_id else llm
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        max_context = getattr(llm, "max_context", None)

        agent = PIPResolutionAgent(
            task_id=job.task_id,
            pip_id=job.pip_id,
            requirements=reqs,
            research_findings=job.research_findings or "",
            last_verification_findings=last_findings,
            project_root=project_root,
            llm_id=task.llm_id,
            budget_id=task.budget_id,
            llm_base_url=llm_base_url,
            llm_model=getattr(llm, "model", None),
            max_context=max_context,
            task_title=getattr(task, "title", ""),
            origin_stage=pip.origin_stage,
        )

        result = loop.run_until_complete(agent.run())
        status = result.get("status", "done")
        turns = result.get("turns", 0)
        _turn_count = turns
        _exit_reason = status if status in ("stalled", "max_turns") else ("completed" if status == "done" else "error")
        _exit_summary = f"PIP resolution agent finished: status={status}, turns={turns}."
        logger.info(
            "[pip_resolution] pip %d task '%s' — agent finished status=%s turns=%d.",
            job.pip_id, job.task_id, status, turns,
        )
        db_status = "failed" if status in ("stalled", "error") else "done"
        update_pip_resolution_job(job.id, status=db_status)

    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = f"Server shutdown during pip resolution for pip {job.pip_id}."
        logger.info("[pip_resolution] pip %d — shutdown.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    except Exception:
        _exit_reason = "error"
        _exit_summary = f"Unexpected error during pip resolution for pip {job.pip_id}."
        logger.exception("[pip_resolution] pip %d — unexpected error.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary, turn_count=_turn_count)
        signal_completion(completion_key)
        with _active_sessions_lock:
            _active_sessions.pop(resolve_key, None)
            _session_llm_ids.pop(resolve_key, None)
            _session_titles.pop(resolve_key, None)
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _dispatch_stranded_subdivisions(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Detect Big Idea tasks that voted SUBDIVIDE but have no active children, and re-trigger subdivision.

    Two cases:
      - type == 'subdividing' with 0 children: subdivision ran but produced nothing (crashed, low confidence).
      - type == 'idea' with outcome='subdivide' in transition_results and 0 children: intake voted subdivide
        but subdivision was never started (ghost parent).
    """
    from app.database import get_all_tasks, get_task, get_llm, get_transition_results

    try:
        all_tasks = get_all_tasks()
    except Exception:
        return

    # Count active children per parent
    child_counts: dict[str, int] = {}
    for t in all_tasks:
        if t.parent_task_id:
            child_counts[t.parent_task_id] = child_counts.get(t.parent_task_id, 0) + 1

    for task in all_tasks:
        tid = task.id
        ttype = (task.type or "").lower()

        if ttype not in ("idea", "subdividing"):
            continue
        if child_counts.get(tid, 0) > 0:
            continue  # Has children - not stranded

        stored_result_str: str | None = None

        if ttype == "idea":
            # Must have a prior subdivide vote to be a stranded subdivision
            results = get_transition_results(tid, transition="idea_to_planning")
            if not results or results[0].outcome != "subdivide":
                continue
            stored_result_str = results[0].vote_summary
        # For "subdividing" with 0 children: always stranded regardless

        # Already running recovery for this task?
        recovery_key = f"subdivision-recovery-{tid}"
        with _active_sessions_lock:
            if recovery_key in _active_sessions and _active_sessions[recovery_key].is_alive():
                continue

        # Cooldown after prior failure
        if tid in _failed_cooldowns:
            if time.time() - _failed_cooldowns[tid] < _FAIL_COOLDOWN_SECONDS:
                continue

        if not task.llm_id:
            continue

        llm = get_llm(task.llm_id)
        if not llm:
            continue

        # One-LLM-at-a-time gate
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"stranded-subdivision '{tid}'",
        ):
            continue

        # Parse the stored intake result dict (contains "votes" array for context)
        stored_result: dict = {}
        if stored_result_str:
            try:
                stored_result = json.loads(stored_result_str)
            except Exception:
                stored_result = {"outcome": "subdivide", "votes": []}
        else:
            stored_result = {"outcome": "subdivide", "votes": []}

        logger.info(
            f"[{AGENT_NAME}] Dispatching subdivision recovery for stranded task '%s' (type=%s).",
            tid, ttype,
        )

        thread = threading.Thread(
            target=_run_subdivision_recovery,
            args=(tid, llm, stored_result),
            daemon=True,
            name=f"maestro-subdivision-recovery-{tid}",
        )
        with _active_sessions_lock:
            _active_sessions[recovery_key] = thread
            _session_llm_ids[recovery_key] = llm.id
            _session_titles[recovery_key] = f"Subdivision Recovery: {tid}"
        thread.start()


def _run_subdivision_recovery(task_id: str, llm: Any, stored_result: dict) -> None:
    """Execute subdivision recovery for a stranded task in its own thread + event loop."""
    from app.main import _handle_subdivision_outcome
    from app.database import get_task

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    _apply_cooldown = True  # Set False on clean server shutdown so the task retries at next start
    try:
        task = get_task(task_id)
        if not task:
            logger.warning(f"[{AGENT_NAME}] Subdivision recovery: task '%s' not found.", task_id)
            return
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        _handle_subdivision_outcome(
            task, stored_result, llm_base_url, llm.model, llm.max_context, loop
        )
        logger.info(f"[{AGENT_NAME}] Subdivision recovery complete for task '%s'.", task_id)
    except ShutdownError:
        logger.info(f"[{AGENT_NAME}] Subdivision recovery for task '%s' aborted due to server shutdown.", task_id)
        _apply_cooldown = False
    except Exception:
        logger.exception(
            f"[{AGENT_NAME}] Subdivision recovery failed for task '%s' (cooldown %ds).",
            task_id, int(_FAIL_COOLDOWN_SECONDS),
        )
    finally:
        # Apply cooldown after every recovery attempt except clean server shutdown.
        # Previously this was only set in the except branch, so a quiet failure
        # (subdivision agent aborts with 3× LLM errors → low confidence → task reverts
        # to idea without raising) would leave _failed_cooldowns unset, causing the
        # scheduler to re-dispatch on the very next tick indefinitely.
        if _apply_cooldown:
            _failed_cooldowns[task_id] = time.time()
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        try:
            loop.run_until_complete(loop.shutdown_asyncgens())
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


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
            _run_intake(task_id, llm_base_url, llm_model, max_context, project_path)
        elif task_type == "planning":
            _run_planning_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "indev":
            _run_dev_orchestrator_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "conceptual_review":
            _run_conceptual_review_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "optimization":
            _run_optimization_security_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "security":
            _run_security_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        elif task_type == "full_review":
            _run_full_review_task(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
        else:
            _run_maestro_loop(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, project_path)
    except ShutdownError:
        logger.info("Task '%s' dispatch aborted due to server shutdown.", task_id)
    except Exception:
        _failed_cooldowns[task_id] = time.time()
        logger.exception("Task '%s' failed in scheduler dispatch (cooldown %ds).", task_id, int(_FAIL_COOLDOWN_SECONDS))
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)


def _run_intake(task_id: str, llm_base_url: str, llm_model: str,
                max_context: int | None = None,
                project_path: str | None = None) -> None:
    """Run the intake pipeline for an IDEA task."""
    from app.agent.intake import run_intake_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import (
        get_task, get_all_tasks, update_task,
        create_transition_vote, create_transition_result,
        create_agent_session, close_agent_session,
    )

    task = get_task(task_id)
    if not task:
        return
    set_task_git_cwd(project_path)

    # Require description, llm_id, budget_id before advancing
    if not task.description or not task.llm_id or not task.budget_id:
        logger.debug("Task '%s' missing required fields for intake, skipping.", task_id)
        return

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="intake",
        llm_id=task.llm_id,
        budget_id=task.budget_id,
        scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = 0
    _completion_tokens = 0

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
        _exit_reason = result.get("outcome", "error")
        _prompt_tokens = result.get("total_prompt_tokens", 0)
        _completion_tokens = result.get("total_completion_tokens", 0)
        reasons = result.get("rejection_reasons", [])
        _exit_summary = "; ".join(reasons[:3]) if reasons else result.get("outcome", "")
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during intake."
        logger.info("[intake] Task '%s' aborted due to server shutdown.", task_id)
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        return
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Pipeline raised an unexpected exception."
        logger.exception("[intake] Pipeline for '%s' failed.", task_id)
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass
        return

    try:
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
        elif result["outcome"] == "subdivide":
            # Lazy import avoids circular import; main.py is fully loaded by call time.
            from app.main import _handle_subdivision_outcome
            _handle_subdivision_outcome(task, result, llm_base_url, llm_model, max_context, loop)
            logger.info("Task '%s' intake result: subdivide (subdivision dispatched via scheduler).", task_id)
        else:
            # Rejected — count all intake rejection results for this task.
            # After MAX_INTAKE_REJECTIONS attempts, mark as intake-exhausted so the
            # scheduler stops auto-retrying. Human must reset via /reset-intake.
            from app.database import get_transition_results as _gtr
            all_results = _gtr(task_id, transition="idea_to_planning") or []
            rejection_count = sum(
                1 for r in all_results
                if r.outcome in ("rejected", "needs_research")
            )
            MAX_INTAKE_REJECTIONS = 3
            if rejection_count >= MAX_INTAKE_REJECTIONS:
                from datetime import datetime as _dt
                from app.database import update_task as _update_task, append_task_history as _ath
                _update_task(task_id, intake_exhausted_at=_dt.utcnow().isoformat())
                _ath(
                    task_id, "intake_exhausted",
                    message=(
                        f"Intake pipeline rejected {rejection_count} times. "
                        f"Manual review required. Use Reset Intake to retry."
                    ),
                )
                logger.warning(
                    "[intake] Task '%s' intake exhausted after %d rejections — stopping auto-retry.",
                    task_id, rejection_count,
                )
            else:
                _rejection_cooldowns[task_id] = time.time()
                logger.info(
                    "[intake] Task '%s' rejected (attempt %d/%d) — retry in %ds.",
                    task_id, rejection_count, MAX_INTAKE_REJECTIONS,
                    int(_REJECTION_RETRY_COOLDOWN),
                )
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_planning_correction(
    loop,
    task_id: str,
    current_plan: dict,
    planning_result_id: int,
    hard_failures: list[dict],
    llm_base_url: str | None,
    llm_model: str | None,
    max_context: int | None,
    llm_id: int | None,
    budget_id: int | None,
    project_path: str | None,
    task_title: str = "",
    task_description: str = "",
) -> dict:
    """Run PlanningCorrectionAgent inline and increment correction_attempts."""
    from app.agent.planning_correction import PlanningCorrectionAgent
    from app.database import update_planning_result, create_agent_session, close_agent_session
    from app.database.session import SessionLocal as _SL

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning_correction",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="gate_repair",
    )
    _exit_reason = "error"
    _exit_summary = ""

    agent = PlanningCorrectionAgent(
        task_id=task_id,
        planning_result_id=planning_result_id,
        current_plan=current_plan,
        gate_failures=hard_failures,
        project_root=project_path,
        llm_id=llm_id,
        budget_id=budget_id,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        max_context=max_context,
        task_title=task_title,
        task_description=task_description,
    )
    try:
        correction_result = loop.run_until_complete(agent.run())
        _exit_reason = correction_result.get("outcome", "error")
        _exit_summary = (
            f"correction outcome={_exit_reason}, "
            f"fields_patched={correction_result.get('fields_patched', [])}"
        )
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Shutdown during planning correction."
        correction_result = {"outcome": "error", "fields_patched": []}
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Unexpected error in planning correction."
        logger.exception("[planning_correction] Unexpected error for task '%s'.", task_id)
        correction_result = {"outcome": "error", "fields_patched": []}
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary)
        # Increment correction_attempts on the planning result row
        _db = _SL()
        try:
            from app.database.models import PlanningResult as _PR
            _pr_row = _db.query(_PR).filter(_PR.id == planning_result_id).first()
            if _pr_row is not None:
                update_planning_result(
                    _db, planning_result_id,
                    correction_attempts=_pr_row.correction_attempts + 1,
                )
        except Exception:
            logger.debug(
                "[planning_correction] Could not increment correction_attempts for result %d.",
                planning_result_id,
            )
        finally:
            _db.close()

    return correction_result


def _run_planning_task(task_id: str, llm_base_url: str, llm_model: str,
                       max_context: int | None = None,
                       llm_id: int | None = None,
                       budget_id: int | None = None,
                       project_path: str | None = None) -> None:
    """Run the planning pipeline for a PLANNING task."""
    from app.agent.planning import run_planning_pipeline
    from app.agent.planning_gate import run_planning_gate
    from app.database import update_task, get_task, get_all_tasks, create_transition_result, task_to_dict
    from app.database import create_agent_session, close_agent_session

    task = get_task(task_id)
    if not task:
        return
    all_tasks = [task_to_dict(t) for t in get_all_tasks()]

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = 0
    _completion_tokens = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # 1. Run planning pipeline
        result = loop.run_until_complete(
            run_planning_pipeline(
                task_id=task_id,
                task_title=task.title,
                task_description=task.description or "",
                all_tasks=all_tasks,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                llm_id=llm_id,
                budget_id=budget_id,
                max_context=max_context,
                project_path=project_path,
            )
        )

        # 2. Store transition result
        _exit_reason = result.get("outcome", "error")
        _prompt_tokens = result.get("total_prompt_tokens", 0)
        _completion_tokens = result.get("total_completion_tokens", 0)
        create_transition_result(
            task_id=task_id,
            transition="planning_to_indev",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=_prompt_tokens,
            total_completion_tokens=_completion_tokens,
        )

        if result.get("outcome") == "passed":
            # 3. Run planning gate
            gate_result = loop.run_until_complete(
                run_planning_gate(
                    task_id=task_id,
                    planning_result=result,
                    all_tasks=all_tasks,
                    max_context=max_context,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=llm_id,
                    budget_id=budget_id,
                    project_path=project_path,
                )
            )
            if gate_result.get("passed"):
                _exit_summary = "Planning passed and gate checks confirmed. Advanced to INDEV."
                update_task(task_id, type="indev")
                logger.info("[planning] Task '%s' advanced to IN DEV.", task_id)
            else:
                _exit_reason = "rejected"
                _exit_summary = "Planning pipeline passed but gate checks failed."
                logger.warning("[planning] Task '%s' failed planning gate.", task_id)
                # Record the gate failure as a separate transition so we can count
                # it reliably (the entry written above has outcome="passed" because
                # the planning *pipeline* passed; only the gate check failed).
                create_transition_result(
                    task_id=task_id,
                    transition="planning_gate",
                    outcome="rejected",
                    vote_summary=gate_result,
                    total_prompt_tokens=gate_result.get("prompt_tokens", 0),
                    total_completion_tokens=gate_result.get("completion_tokens", 0),
                )
                from app.database import get_transition_results as _gtr_plan
                prior_gate = _gtr_plan(task_id, transition="planning_gate") or []
                gate_fail_count = len(prior_gate)
                if gate_fail_count >= _MAX_PLANNING_GATE_FAILURES:
                    logger.warning(
                        "[planning] Task '%s' failed planning gate %d time(s) — "
                        "demoting to IDEA for forced subdivision.",
                        task_id, gate_fail_count,
                    )
                    update_task(task_id, type="idea")
                    create_transition_result(
                        task_id=task_id,
                        transition="idea_to_planning",
                        outcome="subdivide",
                        vote_summary={
                            "outcome": "subdivide",
                            "forced": True,
                            "reason": f"Forced after {gate_fail_count} planning gate failures",
                            "votes": [],
                        },
                        total_prompt_tokens=0,
                        total_completion_tokens=0,
                    )
                    _exit_summary = (
                        f"Planning gate failed {gate_fail_count} time(s) — "
                        "demoted to IDEA for forced subdivision."
                    )
                else:
                    # Try correction agent before applying cooldown.
                    hard_failures = [
                        c for c in gate_result.get("checks", [])
                        if not c.get("passed") and c.get("hard_fail")
                    ]
                    from app.database import get_planning_result as _gpr_corr
                    _pr_for_correction = _gpr_corr(task_id)
                    from app.agent.config import CORRECTION_SKIP_AFTER_FAILURES as _CORR_SKIP
                    _corr_attempts = getattr(_pr_for_correction, "correction_attempts", 0) if _pr_for_correction else 0
                    if hard_failures and _corr_attempts < _CORR_SKIP and _pr_for_correction:
                        correction_result = _run_planning_correction(
                            loop=loop,
                            task_id=task_id,
                            current_plan=result,
                            planning_result_id=_pr_for_correction.id,
                            hard_failures=hard_failures,
                            llm_base_url=llm_base_url,
                            llm_model=llm_model,
                            max_context=max_context,
                            llm_id=llm_id,
                            budget_id=budget_id,
                            project_path=project_path,
                            task_title=task.title or "",
                            task_description=task.description or "",
                        )
                        if correction_result.get("outcome") == "corrected":
                            # Re-run gate on the patched plan
                            _pr_patched = _gpr_corr(task_id)
                            if _pr_patched:
                                import json as _json_corr
                                patched_plan = {
                                    "implementation_steps": _json_corr.loads(_pr_patched.implementation_steps or "[]"),
                                    "file_manifest": _json_corr.loads(_pr_patched.file_manifest or "[]"),
                                    "dependency_graph": _json_corr.loads(_pr_patched.dependency_graph or "{}"),
                                    "interface_contracts": _json_corr.loads(_pr_patched.interface_contracts or "[]"),
                                    "test_strategy": _json_corr.loads(_pr_patched.test_strategy or "[]"),
                                }
                                gate_result2 = loop.run_until_complete(
                                    run_planning_gate(
                                        task_id=task_id,
                                        planning_result=patched_plan,
                                        all_tasks=all_tasks,
                                        max_context=max_context,
                                        llm_base_url=llm_base_url,
                                        llm_model=llm_model,
                                        llm_id=llm_id,
                                        budget_id=budget_id,
                                        project_path=project_path,
                                    )
                                )
                                if gate_result2.get("passed"):
                                    _exit_summary = (
                                        "Correction agent patched plan; gate now passes. "
                                        "Advanced to INDEV."
                                    )
                                    update_task(task_id, type="indev")
                                    logger.info(
                                        "[planning] Task '%s' advanced to INDEV after correction.",
                                        task_id,
                                    )
                                    return
                                create_transition_result(
                                    task_id=task_id,
                                    transition="planning_gate",
                                    outcome="rejected",
                                    vote_summary=gate_result2,
                                    total_prompt_tokens=gate_result2.get("prompt_tokens", 0),
                                    total_completion_tokens=gate_result2.get("completion_tokens", 0),
                                )
                                logger.info(
                                    "[planning] Task '%s' — corrected plan still fails gate; "
                                    "applying cooldown.",
                                    task_id,
                                )

                    # Not at the cap yet — apply a 5-min cooldown so the scheduler
                    # doesn't immediately re-dispatch the same failing plan.
                    _rejection_cooldowns[task_id] = time.time()
                    logger.info(
                        "[planning] Task '%s' gate failure %d/%d — retry in %ds.",
                        task_id, gate_fail_count, _MAX_PLANNING_GATE_FAILURES,
                        int(_REJECTION_RETRY_COOLDOWN),
                    )
        else:
            outcome = result.get("outcome", "unknown")
            _exit_summary = f"Planning outcome: {outcome}"
            logger.info("[planning] Task '%s' planning result: %s", task_id, outcome)
            # If the planning pipeline itself voted to subdivide (plan too broad/deep),
            # demote back to IDEA immediately so the stranded-subdivision detector
            # can break it into smaller pieces on the next scheduler tick.
            if outcome == "subdivide":
                logger.info(
                    "[planning] Task '%s' planning voted subdivide — demoting to IDEA.",
                    task_id,
                )
                update_task(task_id, type="idea")
                create_transition_result(
                    task_id=task_id,
                    transition="idea_to_planning",
                    outcome="subdivide",
                    vote_summary={
                        "outcome": "subdivide",
                        "forced": False,
                        "reason": result.get("scope_reason", "Planning pipeline voted subdivide"),
                        "votes": [],
                    },
                    total_prompt_tokens=0,
                    total_completion_tokens=0,
                )
                _exit_summary = "Planning voted subdivide — demoted to IDEA for subdivision."

    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during planning pipeline."
        logger.info("[planning] Pipeline for '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Planning pipeline raised an unexpected exception."
        logger.exception("[planning] Pipeline for '%s' failed.", task_id)
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_maestro_loop(task_id: str, llm_base_url: str, llm_model: str,
                      max_context: int | None = None,
                      llm_id: int | None = None,
                      budget_id: int | None = None,
                      project_path: str | None = None) -> None:
    """Run the MaestroLoop for a PLANNING/DEVELOPMENT task."""
    from app.agent.loop import MaestroLoop
    from app.agent.config import MAX_TURNS as _MAX_TURNS
    from app.database import update_task, get_task, get_pips_for_task
    from app.database import create_agent_session, close_agent_session

    _session_id = None
    _exit_reason = "error"
    _exit_summary = ""
    _turn_count = None

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        _session_id = create_agent_session(
            task_id=task_id,
            agent_type="maestro_loop",
            llm_id=llm_id,
            budget_id=budget_id,
            scheduler_reason="scheduler",
            max_turns=_MAX_TURNS,
        )

        maestro = MaestroLoop(
            task_id=task_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=project_path,
        )
        result = loop.run_until_complete(maestro.run())
        _turn_count = result.turns
        _exit_summary = result.final_message or ""

        # Handle terminal transition
        if result.status == "ACCEPTED":
            _exit_reason = "completed"
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type == "planning":
                update_task(task_id, type="indev")
                logger.info("Task '%s' advanced from PLANNING to INDEV via scheduler.", task_id)
            elif current_type == "indev":
                update_task(task_id, type="conceptual_review")
                logger.info("Task '%s' advanced from INDEV to CONCEPTUAL REVIEW via scheduler.", task_id)
            else:
                logger.info("Task '%s' reached ACCEPTED but current type '%s' has no auto-transition.", task_id, current_type)

        elif result.status == "REVERT_TO_DESIGN":
            _exit_reason = "rejected"
            update_task(task_id, type="planning")
            _record_demotion_inline(task_id, "indev", "planning", result.final_message or "Agent requested revert")
            logger.warning("Task '%s' reverted to PLANNING via scheduler: %s", task_id, result.final_message)

        elif result.status == "MAX_TURNS":
            _exit_reason = "max_turns"
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type == "planning":
                update_task(task_id, type="indev")
                logger.warning("Task '%s' advanced from PLANNING to INDEV (max_turns).", task_id)
            elif current_type == "indev":
                update_task(task_id, type="conceptual_review")
                logger.warning("Task '%s' advanced from INDEV to CONCEPTUAL REVIEW (max_turns).", task_id)
            else:
                logger.warning("Task '%s' reached terminal state (MAX_TURNS) but current type '%s' has no auto-transition.", task_id, current_type)

        elif result.status == "ERROR":
            _exit_reason = "error"
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type == "planning":
                update_task(task_id, type="indev")
                logger.warning("Task '%s' advanced from PLANNING to INDEV (error).", task_id)
            elif current_type == "indev":
                update_task(task_id, type="conceptual_review")
                logger.warning("Task '%s' advanced from INDEV to CONCEPTUAL REVIEW (error).", task_id)
            else:
                logger.warning("Task '%s' reached terminal state (ERROR) but current type '%s' has no auto-transition.", task_id, current_type)

    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown while loop was running."
        logger.info(f"[{AGENT_NAME}] MaestroLoop for task '%s' aborted due to server shutdown.", task_id)
    except Exception as exc:
        _exit_reason = "error"
        _exit_summary = str(exc)
        logger.exception("MaestroLoop failed for task '%s': %s", task_id, exc)
        # Even on exception, try to advance the task to prevent infinite loops
        task = get_task(task_id)
        if task:
            current_type = (task.type or "").lower()
            if current_type == "planning":
                update_task(task_id, type="indev")
                logger.warning("Task '%s' advanced from PLANNING to INDEV (exception).", task_id)
            elif current_type == "indev":
                update_task(task_id, type="conceptual_review")
                logger.warning("Task '%s' advanced from INDEV to CONCEPTUAL REVIEW (exception).", task_id)
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary, turn_count=_turn_count)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_dev_orchestrator_task(task_id: str, llm_base_url: str, llm_model: str,
                                max_context: int | None = None,
                                llm_id: int | None = None,
                                budget_id: int | None = None,
                                project_path: str | None = None) -> None:
    """Run the DevOrchestrator for an IN DEV task."""
    from app.agent.dev_orchestrator import run_dev_orchestrator
    from app.agent.tools import set_task_git_cwd
    from app.database import get_planning_result, update_task
    from app.database import create_agent_session, close_agent_session
    import json

    set_task_git_cwd(project_path)

    planning_result_obj = get_planning_result(task_id)
    if not planning_result_obj:
        logger.warning("No planning result for task '%s', demoting to planning.", task_id)
        update_task(task_id, type="planning")
        _record_demotion_inline(task_id, "indev", "planning", "Missing planning results")
        return

    planning_result = {
        "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
        "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
        "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
        "interface_contracts": json.loads(planning_result_obj.interface_contracts or "[]"),
        "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
    }

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="dev_orchestrator",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = 0
    _completion_tokens = 0

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
        _prompt_tokens = result.get("prompt_tokens", 0)
        _completion_tokens = result.get("completion_tokens", 0)
        if result.get("status") == "ACCEPTED":
            _exit_reason = "completed"
            _exit_summary = f"Dev orchestrator completed. {result.get('batches_completed', 0)}/{result.get('total_batches', 0)} batches done."
            update_task(task_id, type="conceptual_review")
            logger.info("Task '%s' advanced to CONCEPTUAL REVIEW via scheduler.", task_id)
        else:
            _exit_reason = "rejected"
            _exit_summary = result.get("error_detail") or "Dev orchestrator returned non-ACCEPTED status."
            update_task(task_id, type="planning")
            logger.info("Task '%s' reverted to PLANNING: %s", task_id, result.get("error_detail"))
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during dev orchestrator."
        logger.info(f"[{AGENT_NAME}] Dev orchestrator for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Dev orchestrator raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Dev orchestrator for task '%s' failed.", task_id)
        update_task(task_id, type="planning")
        _record_demotion_inline(task_id, "indev", "planning", "Exception in dev orchestrator")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


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
        create_agent_session, close_agent_session,
    )
    from datetime import datetime
    import json as _json

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    planning_result_obj = get_planning_result(task_id)
    if not planning_result_obj:
        logger.warning("No planning result for task '%s' in conceptual review. Demoting to indev.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "conceptual_review", "indev", "Missing planning results")
        return

    planning_result = {
        "file_manifest": _json.loads(planning_result_obj.file_manifest or "[]"),
        "dependency_graph": _json.loads(planning_result_obj.dependency_graph or "{}"),
        "implementation_steps": _json.loads(planning_result_obj.implementation_steps or "[]"),
        "test_strategy": _json.loads(planning_result_obj.test_strategy or "[]"),
    }

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="conceptual_review",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = 0
    _completion_tokens = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks stage entry if any PIP has unmet requirements
        if not _run_pip_preflight_and_gate(task_id, "conceptual_review", llm_id, budget_id, project_path, loop):
            _exit_reason = "pip_blocked"
            _exit_summary = "PIP pre-flight gate blocked stage entry."
            return  # card stays in conceptual_review; resolution jobs dispatched

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
        _exit_reason = result.get("outcome", "error")
        _exit_summary = result.get("summary", "")
        _prompt_tokens = result.get("total_prompt_tokens", 0)
        _completion_tokens = result.get("total_completion_tokens", 0)
        create_transition_result(
            task_id=task_id,
            transition="conceptual_to_optimization",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=_prompt_tokens,
            total_completion_tokens=_completion_tokens,
        )
        if result.get("outcome") == "passed":
            update_task(task_id, type="optimization")
            logger.info("Task '%s' advanced to OPTIMIZATION via scheduler.", task_id)
        else:
            update_task(task_id, type="indev")
            _record_demotion_inline(task_id, "conceptual_review", "indev", result.get("summary", ""))
            logger.info("Task '%s' demoted to INDEV from conceptual review via scheduler.", task_id)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during conceptual review."
        logger.info(f"[{AGENT_NAME}] Conceptual review for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Conceptual review raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Conceptual review for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "conceptual_review", "indev", "Exception in conceptual review")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


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
        create_agent_session, close_agent_session,
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    # Two separate sessions: one for optimization, one for security.
    _opt_session_id = create_agent_session(
        task_id=task_id, agent_type="optimization",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    _opt_exit_reason = "error"
    _opt_exit_summary = ""
    _sec_session_id = None
    _sec_exit_reason = "error"
    _sec_exit_summary = ""
    _opt_prompt = _opt_compl = _sec_prompt = _sec_compl = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — runs at optimization stage entry
        if not _run_pip_preflight_and_gate(task_id, "optimization", llm_id, budget_id, project_path, loop):
            _opt_exit_reason = "pip_blocked"
            _opt_exit_summary = "PIP pre-flight gate blocked optimization entry."
            return  # card stays in optimization; resolution jobs dispatched

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
        _opt_exit_reason = opt_result.get("outcome", "error")
        _opt_exit_summary = opt_result.get("improvement_summary", "")
        _opt_prompt = opt_result.get("total_prompt_tokens", 0)
        _opt_compl = opt_result.get("total_completion_tokens", 0)
        logger.info("[optimization] Task '%s' via scheduler: %s", task_id, opt_result.get("outcome"))

        # Close optimization session before starting security
        close_agent_session(_opt_session_id, _opt_exit_reason, _opt_exit_summary,
                            prompt_tokens=_opt_prompt, completion_tokens=_opt_compl)
        _opt_session_id = None  # prevent double-close in finally

        # Advance to security stage so the card reflects progress and pre-flight
        # can check requirements at the correct stage.
        update_task(task_id, type="security")

        _sec_session_id = create_agent_session(
            task_id=task_id, agent_type="security",
            llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
        )

        # Pre-flight PIP gate — blocks security entry if any PIP has unmet requirements
        if not _run_pip_preflight_and_gate(task_id, "security", llm_id, budget_id, project_path, loop):
            _sec_exit_reason = "pip_blocked"
            _sec_exit_summary = "PIP pre-flight gate blocked security entry."
            return  # card stays in security; resolution jobs dispatched

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
        _sec_exit_reason = sec_result.get("outcome", "error")
        _sec_exit_summary = sec_result.get("summary", "")
        _sec_prompt = sec_result.get("total_prompt_tokens", 0)
        _sec_compl = sec_result.get("total_completion_tokens", 0)
        create_transition_result(
            task_id=task_id,
            transition="security_review",
            outcome=sec_result.get("outcome", "unknown"),
            vote_summary=sec_result,
            total_prompt_tokens=_sec_prompt,
            total_completion_tokens=_sec_compl,
        )

        if sec_result.get("outcome") == "passed":
            update_task(task_id, type="full_review")
            logger.info("[security] Task '%s' advanced to FULL REVIEW via scheduler.", task_id)
        else:
            demotion = sec_result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "security", demotion, sec_result.get("summary", ""))
            logger.warning("[security] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    except ShutdownError:
        _opt_exit_reason = _opt_exit_reason if _opt_session_id else _opt_exit_reason
        _sec_exit_reason = "shutdown"
        _sec_exit_summary = "Server shutdown during optimization/security."
        if _opt_session_id:
            _opt_exit_reason = "shutdown"
            _opt_exit_summary = "Server shutdown before optimization completed."
        logger.info(f"[{AGENT_NAME}] Optimization/Security for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        if _opt_session_id:
            _opt_exit_reason = "error"
            _opt_exit_summary = "Exception during optimization/security pipeline."
        else:
            _sec_exit_reason = "error"
            _sec_exit_summary = "Exception during security pipeline."
        logger.exception(f"[{AGENT_NAME}] Optimization/Security for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "optimization", "indev", "Exception in optimization/security")
    finally:
        if _opt_session_id is not None:
            close_agent_session(_opt_session_id, _opt_exit_reason, _opt_exit_summary,
                                prompt_tokens=_opt_prompt, completion_tokens=_opt_compl)
        if _sec_session_id is not None:
            close_agent_session(_sec_session_id, _sec_exit_reason, _sec_exit_summary,
                                prompt_tokens=_sec_prompt, completion_tokens=_sec_compl)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_security_task(task_id: str, llm_base_url: str, llm_model: str,
                        max_context: int | None = None,
                        llm_id: int | None = None,
                        budget_id: int | None = None,
                        project_path: str | None = None) -> None:
    """Run security pipeline for a task already in the 'security' stage.

    Called when a task re-enters the security column after PIP resolution.
    Optimization has already passed; this function runs the security pre-flight
    gate and the security pipeline only.
    """
    from app.agent.security_review import run_security_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task
    from app.database import create_transition_result, create_agent_session, close_agent_session

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    _session_id = create_agent_session(
        task_id=task_id, agent_type="security",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = _completion_tokens = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks security pipeline if any PIP is unmet
        if not _run_pip_preflight_and_gate(task_id, "security", llm_id, budget_id, project_path, loop):
            _exit_reason = "pip_blocked"
            _exit_summary = "PIP pre-flight gate blocked security entry."
            return  # card stays in security; resolution jobs dispatched

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
        _exit_reason = sec_result.get("outcome", "error")
        _exit_summary = sec_result.get("summary", "")
        _prompt_tokens = sec_result.get("total_prompt_tokens", 0)
        _completion_tokens = sec_result.get("total_completion_tokens", 0)
        create_transition_result(
            task_id=task_id,
            transition="security_review",
            outcome=sec_result.get("outcome", "unknown"),
            vote_summary=sec_result,
            total_prompt_tokens=_prompt_tokens,
            total_completion_tokens=_completion_tokens,
        )

        if sec_result.get("outcome") == "passed":
            update_task(task_id, type="full_review")
            logger.info("[security] Task '%s' advanced to FULL REVIEW via scheduler.", task_id)
        else:
            demotion = sec_result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "security", demotion, sec_result.get("summary", ""))
            logger.warning("[security] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during security pipeline."
        logger.info(f"[{AGENT_NAME}] Security for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Security pipeline raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Security for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "security", "indev", "Exception in security pipeline")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


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
        create_agent_session, close_agent_session,
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    _session_id = create_agent_session(
        task_id=task_id, agent_type="full_review",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = _completion_tokens = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks full_review entry if any PIP is unmet
        if not _run_pip_preflight_and_gate(task_id, "full_review", llm_id, budget_id, project_path, loop):
            _exit_reason = "pip_blocked"
            _exit_summary = "PIP pre-flight gate blocked full_review entry."
            return  # card stays in full_review; resolution jobs dispatched

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
        _exit_reason = result.get("outcome", "error")
        _exit_summary = result.get("summary", "")
        _prompt_tokens = result.get("total_prompt_tokens", 0)
        _completion_tokens = result.get("total_completion_tokens", 0)
        create_transition_result(
            task_id=task_id,
            transition="full_review",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=_prompt_tokens,
            total_completion_tokens=_completion_tokens,
        )

        if result.get("outcome") == "passed":
            # Pushed to FINAL REVIEW (full_review column) - do a virtual merge test.
            from app.agent.merge import execute_merge
            from app.database import get_project_path as _get_project_path
            pp = project_path
            if not pp and task.project:
                pp = _get_project_path(task.project)

            logger.info("[full_review] Task '%s' passed. Running virtual merge test.", task_id)
            merge_test = execute_merge(task_id, project_path=pp, dry_run=True)

            from app.database import append_task_history
            if merge_test.status == "virtual_passed":
                _exit_summary = "Full review passed. Virtual merge SUCCEEDED."
                append_task_history(task_id, "ready_for_review", message="Full review passed. Virtual merge/test SUCCEEDED. Ready for final manual review and merge.")
                logger.info("[full_review] Task '%s' virtual merge SUCCEEDED.", task_id)
            else:
                _exit_summary = f"Full review passed, but virtual merge FAILED: {merge_test.status}."
                append_task_history(task_id, "merge_test_failed", message=f"Full review passed, but VIRTUAL MERGE FAILED: {merge_test.status}. Detail: {merge_test.error_detail}")
                logger.warning("[full_review] Task '%s' virtual merge FAILED: %s", task_id, merge_test.status)
        else:
            demotion = result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "full_review", demotion, result.get("summary", ""))
            logger.warning("[full_review] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during full review."
        logger.info(f"[{AGENT_NAME}] Full review for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Full review raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Full review for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "full_review", "indev", "Exception in full review")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt_tokens, completion_tokens=_completion_tokens)
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _record_demotion_inline(task_id: str, from_stage: str, to_stage: str, reason: str) -> None:
    """Record a demotion event - scheduler-local version (avoids importing from main.py)."""
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

    # Trigger PIP generation if demoted from a review stage
    review_stages = {"conceptual_review", "optimization", "security", "full_review"}
    if from_stage in review_stages:
        logger.info("[pip] Triggering PIP generation for task '%s' demoted from '%s'.", task_id, from_stage)
        # We're in a daemon thread, but we can still use the loop if one exists
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(generate_pip(task_id, from_stage, reason))
        except RuntimeError:
            asyncio.run(generate_pip(task_id, from_stage, reason))


def _check_completion_rollup_inline(task_id: str) -> None:
    """Mirror of main._check_completion_rollup - avoids circular import."""
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
    """Remove sessions whose threads have completed and re-sync capacity counts."""
    with _active_sessions_lock:
        finished = [tid for tid, t in _active_sessions.items() if not t.is_alive()]
        for tid in finished:
            del _active_sessions[tid]
            _session_llm_ids.pop(tid, None)
            _session_titles.pop(tid, None)

    # Re-sync _llm_session_counts from the ground truth (live threads + external registry)
    new_counts: dict[int, int] = defaultdict(int)

    with _active_sessions_lock:
        for tid, llm_id in _session_llm_ids.items():
            # If the thread is in _active_sessions and is_alive, it counts.
            # (Dead threads were just removed from _session_llm_ids above).
            if tid in _active_sessions and _active_sessions[tid].is_alive():
                new_counts[llm_id] += 1

    with _external_sessions_lock:
        for llm_id in _external_sessions.values():
            new_counts[llm_id] += 1

    with _llm_counts_lock:
        _llm_session_counts.clear()
        _llm_session_counts.update(new_counts)


def _rescue_stale_jobs() -> None:
    """Reset orphaned 'running' and cooled-down 'failed' background jobs to 'pending'.

    Two categories rescued each tick:

    1. Orphaned running jobs - status='running' but no live thread in _active_sessions.
       Happens when the server process crashed mid-job. Reset immediately to 'pending'
       so they are picked up in the same tick's dispatch phase.

    2. Failed jobs past cooldown - status='failed' with completed_at older than the
       per-type retry cooldown. Reset to 'pending' so they get another attempt.
       completed_at is cleared so the auto-set logic in update_*_job fires correctly
       on the next terminal transition.
    """
    from app.database import (
        get_retriable_file_summary_jobs,
        get_retriable_research_jobs,
        get_retriable_arch_gen_jobs,
        update_file_summary_job,
        update_research_job,
        update_arch_gen_job,
    )

    with _active_sessions_lock:
        active_keys = set(_active_sessions.keys())

    # --- File summary jobs ---
    from app.database import get_task as _get_task
    for job in get_retriable_file_summary_jobs(
        failed_cooldown_seconds=_FILE_SUMMARY_RETRY_COOLDOWN
    ):
        session_key = f"file-summary-{job.id}"
        if job.status == 'running':
            if session_key in active_keys:
                continue  # thread is alive - leave it alone
            logger.warning(
                "[rescue] file_summary job %d stuck in 'running' with no thread - marking as failed.",
                job.id,
            )
            update_file_summary_job(
                job.id, 
                status='failed', 
                error_message="Orphaned job rescued from 'running' status (process crash?)"
            )
            # Record failure for the project if possible
            if job.task_id:
                task = _get_task(job.task_id)
                if task and task.project:
                    _record_project_failure(task.project)
        else:
            logger.info(
                "[rescue] Retrying failed file_summary job %d (was failed, cooldown elapsed).",
                job.id,
            )
            update_file_summary_job(job.id, status='pending', completed_at=None)

    # --- Research jobs ---
    for job in get_retriable_research_jobs(
        failed_cooldown_seconds=_RESEARCH_JOB_RETRY_COOLDOWN
    ):
        session_key = f"research-{job.id}"
        if job.status == 'running':
            if session_key in active_keys:
                continue  # thread is alive - leave it alone
            logger.warning(
                "[rescue] research job %d stuck in 'running' with no thread - marking as failed.",
                job.id,
            )
            update_research_job(
                job.id, 
                status='failed', 
                findings="Orphaned job rescued from 'running' status (process crash?)"
            )
            # Record failure for the project
            task = _get_task(job.task_id)
            if task and task.project:
                _record_project_failure(task.project)
        else:
            logger.info(
                "[rescue] Retrying failed research job %d (was failed, cooldown elapsed).",
                job.id,
            )
            update_research_job(job.id, status='pending', completed_at=None)

    # --- Arch gen jobs ---
    for job in get_retriable_arch_gen_jobs(
        failed_cooldown_seconds=_ARCH_GEN_RETRY_COOLDOWN
    ):
        session_key = f"arch-gen-{job.id}"
        if job.status == 'running':
            if session_key in active_keys:
                continue  # thread is alive - leave it alone
            logger.warning(
                "[rescue] arch_gen job %d stuck in 'running' with no thread - marking as failed.",
                job.id,
            )
            update_arch_gen_job(
                job.id, 
                status='failed', 
                error_message="Orphaned job rescued from 'running' status (process crash?)"
            )
            _record_project_failure(job.project)
        else:
            retry_count = getattr(job, 'retry_count', 0) or 0
            if retry_count >= _ARCH_GEN_MAX_RETRIES:
                logger.warning(
                    "[rescue] arch_gen job %d (%s / %s) exhausted %d retries — abandoning.",
                    job.id, job.project, job.category, _ARCH_GEN_MAX_RETRIES,
                )
                update_arch_gen_job(
                    job.id,
                    status='abandoned',
                    error_message=(
                        f"Abandoned after {_ARCH_GEN_MAX_RETRIES} failed attempts. "
                        f"Last error: {job.error_message or 'unknown'}"
                    ),
                )
                from app.database import create_inbox_message
                create_inbox_message(
                    subject=f"Arch Gen failed: {job.project} / {job.category}",
                    source_type="arch_gen_failure",
                    task_id=None,
                    task_title=f"{job.category} Architecture ({job.project})",
                    outcome="abandoned",
                    data_json=json.dumps({
                        "job_id": job.id,
                        "project": job.project,
                        "category": job.category,
                        "retry_count": retry_count,
                        "last_error": job.error_message or "unknown",
                    }),
                )
            else:
                logger.info(
                    "[rescue] Retrying arch_gen job %d (%s / %s) — attempt %d/%d.",
                    job.id, job.project, job.category,
                    retry_count + 1, _ARCH_GEN_MAX_RETRIES,
                )
                update_arch_gen_job(
                    job.id,
                    status='pending',
                    completed_at=None,
                    retry_count=retry_count + 1,
                )


def _task_to_mini_dict(task: Any) -> dict:
    """Minimal dict for DAGResolver - avoids importing task_to_dict."""
    return {
        "id": task.id,
        "type": task.type,
        "position": task.position,
        "prerequisites": task.prerequisites or [],
        "parent_task_id": getattr(task, "parent_task_id", None),
    }
