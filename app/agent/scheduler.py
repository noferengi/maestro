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


# task_id -> timestamp of last failed dispatch (cooldown to avoid retry storms)
_failed_cooldowns: dict[str, float] = {}
_FAIL_COOLDOWN_SECONDS = 60.0  # Wait 60s before retrying a failed task

# task_id -> timestamp of last rejected intake run (longer inter-retry backoff)
# Rejection means "not ready yet", not "permanently blocked" - always retry.
_rejection_cooldowns: dict[str, float] = {}
_REJECTION_RETRY_COOLDOWN = 300.0   # 5 min between retries
_MAX_REJECTIONS_BEFORE_SUBDIVIDE = 3  # force subdivision after this many consecutive rejections

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

        # For 'idea' tasks, skip ones already successfully processed; retry rejected
        # ones after a cooldown, and force subdivision after repeated rejections.
        if task_type == "idea":
            from app.database import get_transition_results
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

    # 5.6. Dispatch pending PIP resolution jobs (research + resolution agents for blocked PIPs)
    _dispatch_pip_resolution_jobs(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 6. Recover stranded subdivision tasks (respects one-LLM policy + full capacity caps)
    _dispatch_stranded_subdivisions(
        allowed_llm_id, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )


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


def _run_file_summary_job(job: Any, llm: Any) -> None:
    """Execute a single file summary job in its own thread + event loop."""
    from app.database import update_file_summary_job
    from app.agent.file_summary_agent import execute_file_summary

    completion_key = f"file_summary:{job.sha1_hash}:{job.file_size_bytes}"
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
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
    from app.database import get_pips_for_task
    from app.agent.pip_agent import run_pip_preflight

    pips = get_pips_for_task(task_id)
    if not pips:
        return True

    logger.info(
        "[pip_preflight] Task '%s' has %d PIP(s) — running pre-flight for stage '%s'.",
        task_id, len(pips), stage,
    )

    preflight = loop.run_until_complete(
        run_pip_preflight(task_id, stage, llm_id, budget_id, project_path)
    )

    if preflight["all_passed"]:
        logger.info(
            "[pip_preflight] Task '%s' — all PIPs passed at stage '%s'. Proceeding.",
            task_id, stage,
        )
        return True

    failed = [r for r in preflight["results"] if r["outcome"] != "passed"]
    logger.warning(
        "[pip_preflight] Task '%s' — %d PIP(s) failed at stage '%s'. Scheduling resolution.",
        task_id, len(failed), stage,
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
    )
    from app.agent.project_snapshot import build_project_snapshot

    research_key = f"pip_research_{job.pip_id}"
    job_key = f"pip-research-{job.pip_id}"

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

        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        from app.agent.llm_client import call_llm as _call_llm
        response_text, _ = loop.run_until_complete(
            _call_llm(
                messages=[{"role": "user", "content": prompt}],
                llm_id=task.llm_id,
                budget_id=task.budget_id,
            )
        )
        update_pip_resolution_job(job.id, research_findings=response_text)
        logger.info("[pip_resolution] Research complete for pip %d.", job.pip_id)

    except ShutdownError:
        logger.info("[pip_resolution] Research for pip %d aborted (shutdown).", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    except Exception:
        logger.exception("[pip_resolution] Research for pip %d failed.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    finally:
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
        get_llm,
    )
    from app.agent.pip_resolution import PIPResolutionAgent

    resolve_key = f"pip-resolve-{job.pip_id}"
    completion_key = f"pip_resolution_{job.pip_id}"

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
        logger.info(
            "[pip_resolution] pip %d task '%s' — agent finished status=%s turns=%d.",
            job.pip_id, job.task_id, status, turns,
        )
        db_status = "failed" if status in ("stalled", "error") else "done"
        update_pip_resolution_job(job.id, status=db_status)

    except ShutdownError:
        logger.info("[pip_resolution] pip %d — shutdown.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    except Exception:
        logger.exception("[pip_resolution] pip %d — unexpected error.", job.pip_id)
        update_pip_resolution_job(job.id, status="failed")
    finally:
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
    except ShutdownError:
        logger.info("[intake] Task '%s' aborted due to server shutdown.", task_id)
        return
    except Exception:
        logger.exception("[intake] Pipeline for '%s' failed.", task_id)
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
            # Rejected - count how many consecutive non-passing results this task has.
            # After _MAX_REJECTIONS_BEFORE_SUBDIVIDE attempts, force subdivision so the
            # idea is broken into smaller pieces that can each be researched independently.
            # Otherwise, schedule a retry after _REJECTION_RETRY_COOLDOWN seconds.
            from app.database import get_transition_results as _gtr
            all_results = _gtr(task_id, transition="idea_to_planning") or []
            rejection_count = sum(
                1 for r in all_results
                if r.outcome not in ("passed", "subdivide")
            )
            if rejection_count >= _MAX_REJECTIONS_BEFORE_SUBDIVIDE:
                logger.info(
                    "Task '%s' rejected %d time(s) - forcing subdivision.",
                    task_id, rejection_count,
                )
                # Write a subdivide transition result so _dispatch_stranded_subdivisions
                # picks this task up on the next tick and runs the subdivision agent.
                from app.database import create_transition_result as _ctr
                _ctr(
                    task_id=task_id,
                    transition="idea_to_planning",
                    outcome="subdivide",
                    vote_summary={
                        "outcome": "subdivide",
                        "forced": True,
                        "reason": f"Forced after {rejection_count} rejections",
                        "votes": [],
                    },
                    total_prompt_tokens=0,
                    total_completion_tokens=0,
                )
                # Leave task type as 'idea' so the stranded-subdivision detector fires.
            else:
                _rejection_cooldowns[task_id] = time.time()
                logger.info(
                    "Task '%s' rejected (attempt %d/%d) - retry in %ds.",
                    task_id, rejection_count, _MAX_REJECTIONS_BEFORE_SUBDIVIDE,
                    int(_REJECTION_RETRY_COOLDOWN),
                )
    finally:
        try:
            loop.run_until_complete(asyncio.wait_for(loop.shutdown_asyncgens(), timeout=10.0))
        except Exception:
            pass
        try:
            loop.close()
        except Exception:
            pass


def _run_planning_task(task_id: str, llm_base_url: str, llm_model: str,
                       max_context: int | None = None,
                       llm_id: int | None = None,
                       budget_id: int | None = None,
                       project_path: str | None = None) -> None:
    """Run the planning pipeline for a PLANNING task."""
    from app.agent.planning import run_planning_pipeline
    from app.agent.planning_gate import run_planning_gate
    from app.database import update_task, get_task, get_all_tasks, create_transition_result, task_to_dict

    task = get_task(task_id)
    if not task:
        return
    all_tasks = [task_to_dict(t) for t in get_all_tasks()]

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
        create_transition_result(
            task_id=task_id,
            transition="planning_to_indev",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=result.get("total_prompt_tokens", 0),
            total_completion_tokens=result.get("total_completion_tokens", 0),
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
                update_task(task_id, type="indev")
                logger.info("[planning] Task '%s' advanced to IN DEV.", task_id)
            else:
                logger.warning("[planning] Task '%s' failed planning gate.", task_id)
        else:
            logger.info("[planning] Task '%s' planning result: %s", task_id, result.get('outcome'))

    except ShutdownError:
        logger.info("[planning] Pipeline for '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception("[planning] Pipeline for '%s' failed.", task_id)
    finally:
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
    from app.database import update_task, get_task, get_pips_for_task

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
        result = loop.run_until_complete(maestro.run())

        # Handle terminal transition
        if result.status == "ACCEPTED":
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
            update_task(task_id, type="planning")
            _record_demotion_inline(task_id, "indev", "planning", result.final_message or "Agent requested revert")
            logger.warning("Task '%s' reverted to PLANNING via scheduler: %s", task_id, result.final_message)

        elif result.status in ("MAX_TURNS", "ERROR"):
            # Agent exhausted turns or encountered error - still advance task to prevent infinite loops
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type == "planning":
                update_task(task_id, type="indev")
                logger.warning("Task '%s' advanced from PLANNING to INDEV (max_turns/error: %s).", task_id, result.status)
            elif current_type == "indev":
                update_task(task_id, type="conceptual_review")
                logger.warning("Task '%s' advanced from INDEV to CONCEPTUAL REVIEW (max_turns/error: %s).", task_id, result.status)
            else:
                logger.warning("Task '%s' reached terminal state (%s) but current type '%s' has no auto-transition.", task_id, result.status, current_type)

    except ShutdownError:
        logger.info(f"[{AGENT_NAME}] MaestroLoop for task '%s' aborted due to server shutdown.", task_id)
    except Exception as exc:
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
            # Pre-flight PIP gate runs at conceptual_review entry — no separate stage needed.
        else:
            update_task(task_id, type="planning")
            logger.info("Task '%s' reverted to PLANNING: %s", task_id, result.get("error_detail"))
    except ShutdownError:
        logger.info(f"[{AGENT_NAME}] Dev orchestrator for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception(f"[{AGENT_NAME}] Dev orchestrator for task '%s' failed.", task_id)
        update_task(task_id, type="planning")
        _record_demotion_inline(task_id, "indev", "planning", "Exception in dev orchestrator")
    finally:
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

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks stage entry if any PIP has unmet requirements
        if not _run_pip_preflight_and_gate(task_id, "conceptual_review", llm_id, budget_id, project_path, loop):
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
    except ShutdownError:
        logger.info(f"[{AGENT_NAME}] Conceptual review for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception(f"[{AGENT_NAME}] Conceptual review for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "conceptual_review", "indev", "Exception in conceptual review")
    finally:
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
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — runs at optimization stage entry
        if not _run_pip_preflight_and_gate(task_id, "optimization", llm_id, budget_id, project_path, loop):
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
        logger.info("[optimization] Task '%s' via scheduler: %s", task_id, opt_result.get("outcome"))

        # Advance to security stage so the card reflects progress and pre-flight
        # can check requirements at the correct stage.
        update_task(task_id, type="security")

        # Pre-flight PIP gate — blocks security entry if any PIP has unmet requirements
        if not _run_pip_preflight_and_gate(task_id, "security", llm_id, budget_id, project_path, loop):
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
        create_transition_result(
            task_id=task_id,
            transition="security_review",
            outcome=sec_result.get("outcome", "unknown"),
            vote_summary=sec_result,
            total_prompt_tokens=sec_result.get("total_prompt_tokens", 0),
            total_completion_tokens=sec_result.get("total_completion_tokens", 0),
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
        logger.info(f"[{AGENT_NAME}] Optimization/Security for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception(f"[{AGENT_NAME}] Optimization/Security for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "optimization", "indev", "Exception in optimization/security")
    finally:
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
    from app.database import create_transition_result

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks security pipeline if any PIP is unmet
        if not _run_pip_preflight_and_gate(task_id, "security", llm_id, budget_id, project_path, loop):
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
        create_transition_result(
            task_id=task_id,
            transition="security_review",
            outcome=sec_result.get("outcome", "unknown"),
            vote_summary=sec_result,
            total_prompt_tokens=sec_result.get("total_prompt_tokens", 0),
            total_completion_tokens=sec_result.get("total_completion_tokens", 0),
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
        logger.info(f"[{AGENT_NAME}] Security for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception(f"[{AGENT_NAME}] Security for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "security", "indev", "Exception in security pipeline")
    finally:
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
    )

    set_task_git_cwd(project_path)

    task = get_task(task_id)
    if not task:
        return

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — blocks full_review entry if any PIP is unmet
        if not _run_pip_preflight_and_gate(task_id, "full_review", llm_id, budget_id, project_path, loop):
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
        create_transition_result(
            task_id=task_id,
            transition="full_review",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=result.get("total_prompt_tokens", 0),
            total_completion_tokens=result.get("total_completion_tokens", 0),
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
                append_task_history(task_id, "ready_for_review", message="Full review passed. Virtual merge/test SUCCEEDED. Ready for final manual review and merge.")
                logger.info("[full_review] Task '%s' virtual merge SUCCEEDED.", task_id)
            else:
                append_task_history(task_id, "merge_test_failed", message=f"Full review passed, but VIRTUAL MERGE FAILED: {merge_test.status}. Detail: {merge_test.error_detail}")
                logger.warning("[full_review] Task '%s' virtual merge FAILED: %s", task_id, merge_test.status)
        else:
            demotion = result.get("demotion_target", "indev")
            update_task(task_id, type=demotion)
            _record_demotion_inline(task_id, "full_review", demotion, result.get("summary", ""))
            logger.warning("[full_review] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    except ShutdownError:
        logger.info(f"[{AGENT_NAME}] Full review for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        logger.exception(f"[{AGENT_NAME}] Full review for task '%s' failed.", task_id)
        update_task(task_id, type="indev")
        _record_demotion_inline(task_id, "full_review", "indev", "Exception in full review")
    finally:
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
