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
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone as _tz


def _now_utc() -> str:
    return datetime.now(_tz.utc).strftime("%Y-%m-%d %H:%M UTC")
import threading
from collections import defaultdict
from typing import Any, Callable

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
from app.agent.llm_client import is_shutting_down, signal_shutdown, signal_force_shutdown, ShutdownError, TaskDeactivatedError

logger = logging.getLogger(__name__)
AGENT_NAME = "Scheduler"


# ---------------------------------------------------------------------------
# Autopilot / Mission state machine
# ---------------------------------------------------------------------------

@dataclass
class MissionConfig:
    time_limit_seconds: "int | None" = None
    token_budget: "int | None" = None
    card_count_target: "int | None" = None
    goal_card_id: "str | None" = None


@dataclass
class MissionState:
    config: MissionConfig
    started_at: datetime = field(default_factory=datetime.utcnow)
    completed_cards: int = 0
    tokens_used: int = 0
    active: bool = True

    def check_termination(self) -> "str | None":
        """Returns the fired condition name, or None if the mission is still running."""
        if self.config.time_limit_seconds:
            elapsed = (datetime.utcnow() - self.started_at).total_seconds()
            if elapsed >= self.config.time_limit_seconds:
                return "time_limit"
        if self.config.token_budget and self.tokens_used >= self.config.token_budget:
            return "token_budget"
        if self.config.card_count_target and self.completed_cards >= self.config.card_count_target:
            return "card_count"
        if self.config.goal_card_id:
            from app.database import get_task as _get_task
            card = _get_task(self.config.goal_card_id)
            if card and card.type == "completed":
                return "goal_card"
        return None


# Module-level mission state — None means no active mission
_mission_state: "MissionState | None" = None
_mission_lock = threading.Lock()


def set_mission(config: "MissionConfig | None") -> None:
    """Install (or clear) the active mission.  Called by the /api/settings/autopilot endpoint."""
    global _mission_state
    with _mission_lock:
        if config is None:
            _mission_state = None
        else:
            _mission_state = MissionState(config=config)


def get_mission_state() -> "MissionState | None":
    with _mission_lock:
        return _mission_state


def _should_autopilot_dispatch() -> bool:
    """Return True if the global autopilot setting allows dispatching right now."""
    from app.database import get_system_setting as _gs
    autopilot = _gs("maestro_autopilot", "off")
    if autopilot != "on":
        return False
    try:
        start = int(_gs("autopilot_start_hour", 0) or 0)
        stop  = int(_gs("autopilot_stop_hour",  24) or 24)
    except (TypeError, ValueError):
        return True  # malformed config → don't block
    now_hour = datetime.utcnow().hour
    if start == stop or stop == 24:
        return True  # no schedule restriction
    if start < stop:
        return start <= now_hour < stop
    # overnight wrap: e.g. 23–07
    return now_hour >= start or now_hour < stop


def _tick_mission() -> None:
    """Check mission termination conditions; fire mission report if any condition is met."""
    global _mission_state
    with _mission_lock:
        ms = _mission_state
    if ms is None or not ms.active:
        return

    reason = ms.check_termination()
    if reason is None:
        return

    logger.info("[Autopilot] Mission terminated: %s", reason)

    # 1. Flip autopilot off
    from app.database import set_system_setting as _ss
    _ss("maestro_autopilot", "off", "Global autopilot switch: on|off")

    # 2. Stop all running MaestroLoop sessions
    from app.agent.loop import request_stop as _request_stop
    with _active_sessions_lock:
        task_ids = list(_active_sessions.keys())
    for tid in task_ids:
        try:
            _request_stop(tid)
        except Exception:
            pass

    # 3. Create mission report arch card
    _create_mission_report(ms, reason)

    with _mission_lock:
        if _mission_state is ms:
            _mission_state = None


def _create_mission_report(ms: MissionState, reason: str) -> None:
    """Persist a mission report as an architecture task card."""
    from app.database import create_task, get_all_projects
    elapsed = int((datetime.utcnow() - ms.started_at).total_seconds())
    hours, rem = divmod(elapsed, 3600)
    mins = rem // 60

    reason_labels = {
        "time_limit":        "Time limit reached",
        "token_budget":      "Token budget exhausted",
        "card_count":        "Card count target met",
        "goal_card":         "Goal card completed",
    }
    label = reason_labels.get(reason, reason)

    body = (
        f"Mission completed — {label}\n"
        f"Duration: {hours}h {mins}m\n"
        f"Cards completed: {ms.completed_cards}\n"
        f"Tokens used: {ms.tokens_used:,}"
    )

    projects = get_all_projects()
    project_name = projects[0].name if projects else "TheMaestro"
    try:
        create_task(
            title="Mission Report",
            description=body,
            task_type="architecture",
            project=project_name,
            content={"category": "General", "priority": "normal"},
        )
    except Exception as exc:
        logger.warning("[Autopilot] Could not create mission report card: %s", exc)


class WorktreeIsolationError(Exception):
    """Raised when a task's git worktree cannot be created. Task will not run."""

# ---------------------------------------------------------------------------
# Scheduler state
# ---------------------------------------------------------------------------

# task_id -> threading.Thread for currently running loops
_active_sessions: dict[str, threading.Thread] = {}
_session_llm_ids: dict[str, int] = {}
_session_titles: dict[str, str] = {}
_session_types: dict[str, str] = {}
_session_started_at: dict[str, float] = {}
_session_ids: dict[str, str] = {}  # task_id -> llm_session_id
_active_sessions_lock = threading.Lock()

# session key -> llm_id for all active sessions (tasks, file summaries, research, recovery)
# Protected by _active_sessions_lock.  Used to enforce the one-LLM-at-a-time policy.
_session_llm_ids: dict[str, int] = {}

# session key -> display title for background jobs (arch-gen, research, etc.)
# Protected by _active_sessions_lock.
_session_titles: dict[str, str] = {}

# session key -> task type ("planning", "indev", etc.)
# Protected by _active_sessions_lock.
_session_types: dict[str, str] = {}

# session key -> wall-clock start time (time.time())
# Protected by _active_sessions_lock.
_session_started_at: dict[str, float] = {}

# session key -> AgentSession.id (DB PK) for the *current* open session.
# Protected by _active_sessions_lock.  Cleared alongside _active_sessions in
# _cleanup_finished().  Used by close_zombie_sessions_by_session_id() so that
# stale predecessor sessions (e.g. 50 previous planning runs on the same task)
# are closed even while the task thread is still alive.
_active_db_session_ids: dict[str, int] = {}


def register_db_session(session_key: str, db_session_id: int) -> None:
    """Record the current DB AgentSession PK for this session key.

    Call immediately after create_agent_session() returns a non-None id.
    Overwrites any previous entry for the same key — the latest session is
    always the one that should survive cleanup.
    """
    with _active_sessions_lock:
        _active_db_session_ids[session_key] = db_session_id


# Per-LLM active session count: llm_id -> count
_llm_session_counts: dict[int, int] = defaultdict(int)
_llm_counts_lock = threading.Lock()

_last_global_heartbeat = 0.0
_HEARTBEAT_INTERVAL_SECONDS = 300.0  # 5 minutes

_last_training_score = time.time()          # Don't fire on first tick; wait 1 hour
_TRAINING_SCORE_INTERVAL_SECONDS = 3600.0   # 1 hour

_last_training_export_check = time.time()   # Don't fire on first tick; wait 24 hours
_TRAINING_EXPORT_INTERVAL_SECONDS = 86400.0  # 24 hours

# Rate-limiting for Maestro logs to prevent log flooding
_project_last_maestro_log = {}  # project_name -> {log_type: timestamp}
_MAESTRO_LOG_INTERVAL = 300.0   # 5 minutes

def _log_project_maestro(project_name: str, log_type: str, message: str, level: int = logging.INFO) -> None:
    """Log a Maestro-related message for a project, with rate-limiting."""
    now = time.time()
    if project_name not in _project_last_maestro_log:
        _project_last_maestro_log[project_name] = {}
    
    last_log_time = _project_last_maestro_log[project_name].get(log_type, 0.0)
    if (now - last_log_time) >= _MAESTRO_LOG_INTERVAL:
        logger.log(level, message)
        _project_last_maestro_log[project_name][log_type] = now
    else:
        # Always log at DEBUG level even if rate-limited for INFO
        logger.debug(message)

# ---------------------------------------------------------------------------
# Inter-agent session query helpers (Gap 8)
# ---------------------------------------------------------------------------

def get_active_session_info(task_id: str) -> "dict | None":
    """Return metadata for a running session keyed by task_id, or None if not alive."""
    with _active_sessions_lock:
        thread = _active_sessions.get(task_id)
        if thread is None or not thread.is_alive():
            return None
        return {
            "task_id": task_id,
            "title": _session_titles.get(task_id),
            "type": _session_types.get(task_id),
            "llm_id": _session_llm_ids.get(task_id),
        }


def list_active_sessions(
    exclude_task_id: "str | None" = None,
    project_filter: "str | None" = None,
) -> "list[dict]":
    """Return all alive sessions, optionally excluding the caller and filtering by project."""
    with _active_sessions_lock:
        snapshot = [
            (tid, thread)
            for tid, thread in _active_sessions.items()
            if thread.is_alive() and tid != exclude_task_id
        ]
        entries = [
            {
                "session_id": tid,
                "task_id": tid,
                "task_title": _session_titles.get(tid, tid),
                "agent_type": _session_types.get(tid, "unknown"),
                "llm_id": _session_llm_ids.get(tid),
            }
            for tid, _ in snapshot
        ]

    if not project_filter:
        return entries

    # Filter by project — requires a DB lookup outside the lock
    filtered = []
    for entry in entries:
        try:
            from app.database import get_task as _db_get_task
            task = _db_get_task(entry["task_id"])
            if task is not None and task.project == project_filter:
                entry["project"] = task.project
                filtered.append(entry)
        except Exception:
            pass
    return filtered


# ---------------------------------------------------------------------------
# Session management
# ---------------------------------------------------------------------------

# project_name -> timestamp (float) of the most recent pipeline activity we
# observed for that project.  Initialised to now() when the project is first
# seen so new projects get a grace period before Maestro fires.
_project_last_activity: dict[str, float] = {}
_project_last_activity_lock = threading.Lock()

# Projects that currently have a Maestro thread running.
_active_maestro_projects: set[str] = set()
_active_maestro_lock = threading.Lock()

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


def register_bg_pipeline_thread(key: str, llm_id: int, title: str = "") -> None:
    """Register the current thread in _active_sessions so stop_scheduler drains it.

    Call this after wait_and_register_pipeline_session() succeeds.  The LLM slot
    is already counted by that call; this only adds the thread reference so the
    shutdown sequence can join() it.
    """
    with _active_sessions_lock:
        _active_sessions[key] = threading.current_thread()
        _session_llm_ids[key] = llm_id
        _session_titles[key] = title
        _session_types[key] = "bg_pipeline"
        _session_started_at[key] = time.time()


def unregister_bg_pipeline_thread(key: str) -> None:
    """Remove the thread from _active_sessions after the pipeline finishes."""
    with _active_sessions_lock:
        _active_sessions.pop(key, None)
        _session_llm_ids.pop(key, None)
        _session_titles.pop(key, None)
        _session_types.pop(key, None)
        _session_started_at.pop(key, None)


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
    on timeout or if the server is shutting down.
    """
    with _pending_completions_lock:
        ev = _pending_completions.get(key)
    if ev is None:
        # Key already removed - job completed before we reached this call.
        return True
    deadline = time.monotonic() + timeout
    _POLL = 1.0
    while True:
        if is_shutting_down():
            return False
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        if ev.wait(timeout=min(_POLL, remaining)):
            return True


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


# Hung session recovery: kill sessions that are alive but have made no LLM call recently.
# Min age prevents false positives on brand-new sessions still in their survey/setup phase.
_HUNG_SESSION_MIN_AGE_SECONDS = 60    # 1 min — allow brief startup/survey phase
_HUNG_SESSION_IDLE_SECONDS    = 300   # 5 min — kill if no LLM call in this window

# task_id -> timestamp of last failed dispatch (cooldown to avoid retry storms)
_failed_cooldowns: dict[str, float] = {}
_FAIL_COOLDOWN_SECONDS = 60.0  # Wait 60s before retrying a failed task

# LLM IDs dispatched during the current tick — used by _check_and_reserve_slot to
# correctly determine llm_already_loaded when a fast-completing thread has decremented
# _llm_session_counts back to 0 before the next dispatch check within the same tick.
_tick_dispatched_llm_ids: set[int] = set()

# task_id -> timestamp of last rejected intake run (longer inter-retry backoff)
# Rejection means "not ready yet", not "permanently blocked" — always retry unless exhausted.
_rejection_cooldowns: dict[str, float] = {}
_REJECTION_RETRY_COOLDOWN = 300.0   # 5 min between retries

# Planning gate failure limit.  After this many failed gate attempts the task is
# parked in _planning_stopped and requires a manual "Run Planning" trigger to retry.
_MAX_PLANNING_GATE_FAILURES = 5

# task_id -> human-readable reason: planning exhausted all design retries without
# producing a passing review.  Tasks in this dict are NOT re-dispatched by the
# scheduler — they require a manual "Run Planning" trigger from the user.
# Cleared by clear_planning_stopped() which is called from /run-planning endpoint.
_planning_stopped: dict[str, str] = {}

# Stage type names that should NEVER be auto-dispatched.
# 'human_review' is the canonical built-in stage key for the human gate.
# Custom pipelines using a non-standard stage_key with agent_type='human_gate'
# are caught by the agent_type check inside dispatch_task() (returns False).
_SCHEDULER_SKIP_STAGE_TYPES: frozenset[str] = frozenset({"human_review"})

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


# Project enabled/disabled cache — rebuilt at most once per 30 s to avoid per-tick DB queries.
# project_id -> bool (True = enabled, default)
_project_enabled_cache: dict[int, bool] = {}
# project_name -> project_id (for dispatchers that only have a name)
_project_name_id_cache: dict[str, int] = {}
_project_enabled_cache_time: float = 0.0
_PROJECT_ENABLED_CACHE_TTL = 30.0


def _refresh_project_enabled_cache() -> None:
    """Reload the project-enabled cache from project_settings."""
    global _project_enabled_cache, _project_name_id_cache, _project_enabled_cache_time
    try:
        from app.database.session import SessionLocal
        from app.database.models import Project, ProjectSettings
        db = SessionLocal()
        try:
            projects = db.query(Project).all()
            disabled_ids: set[int] = {
                row.project_id
                for row in db.query(ProjectSettings).filter_by(key="enabled")
                if row.value == "false"
            }
            _project_enabled_cache = {p.id: p.id not in disabled_ids for p in projects}
            _project_name_id_cache = {p.name: p.id for p in projects}
        finally:
            db.close()
    except Exception as exc:
        logger.warning("[scheduler] Failed to refresh project enabled cache: %s", exc)
    _project_enabled_cache_time = time.time()


def _is_project_enabled_by_id(project_id: int | None) -> bool:
    """Return True if the project is enabled (default when unknown)."""
    if project_id is None:
        return True
    if time.time() - _project_enabled_cache_time > _PROJECT_ENABLED_CACHE_TTL:
        _refresh_project_enabled_cache()
    return _project_enabled_cache.get(project_id, True)


def _is_project_enabled_by_name(project_name: str | None) -> bool:
    """Return True if the project is enabled (default when unknown)."""
    if not project_name:
        return True
    if time.time() - _project_enabled_cache_time > _PROJECT_ENABLED_CACHE_TTL:
        _refresh_project_enabled_cache()
    pid = _project_name_id_cache.get(project_name)
    if pid is None:
        return True
    return _project_enabled_cache.get(pid, True)


# Background thread that drives the scheduler tick
_scheduler_thread: threading.Thread | None = None
_scheduler_stop = threading.Event()

# Background thread that keeps last_activity_at fresh and sweeps stale sessions
_session_heartbeat_thread: "threading.Thread | None" = None
_session_heartbeat_stop = threading.Event()
_HEARTBEAT_SESSION_INTERVAL = 30    # seconds between heartbeat writes
_HEARTBEAT_STALE_INTERVAL   = 60    # seconds between stale-session sweeps
_HEARTBEAT_STALE_TIMEOUT    = 120   # sessions older than this are considered crashed


def _session_heartbeat_worker() -> None:
    """Daemon: keep last_activity_at fresh and detect/close stale sessions."""
    _last_stale_check = 0.0
    while not _session_heartbeat_stop.wait(_HEARTBEAT_SESSION_INTERVAL):
        # 1. Update last_activity_at for all currently registered sessions.
        with _active_sessions_lock:
            session_ids = set(_active_db_session_ids.values())
        if session_ids:
            try:
                from app.database import heartbeat_sessions
                heartbeat_sessions(session_ids)
            except Exception:
                logger.debug("[heartbeat] Batch heartbeat update failed.")

        # 2. Periodically sweep for stale sessions (crashed-process victims).
        now = time.time()
        if now - _last_stale_check >= _HEARTBEAT_STALE_INTERVAL:
            _last_stale_check = now
            try:
                from app.database import close_stale_sessions
                stale = close_stale_sessions(timeout_seconds=_HEARTBEAT_STALE_TIMEOUT)
                if stale:
                    logger.info(
                        "[heartbeat] Closed stale session(s) for tasks: %s", stale
                    )
            except Exception:
                logger.debug("[heartbeat] Stale session sweep failed.")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def start_scheduler() -> None:
    """Start the background scheduler thread (if enabled in config)."""
    global _scheduler_thread, _session_heartbeat_thread
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

    # Prune orphaned worktrees from a crashed previous server run.
    try:
        from app.database import get_all_projects as _get_all_projects
        from app.agent.worktree import prune_orphaned_worktrees
        prune_orphaned_worktrees([p.path for p in _get_all_projects() if p.path])
    except Exception:
        logger.exception("startup: prune_orphaned_worktrees failed (non-fatal)")

    # Safety: if autopilot was 'on' when the server crashed (no in-memory mission),
    # reset it to 'off' so the user must re-engage manually.
    try:
        from app.database import get_system_setting as _gss, set_system_setting as _sss
        if _gss("maestro_autopilot", "off") == "on":
            _sss("maestro_autopilot", "off", "Global autopilot switch: on|off")
            logger.warning(
                "Startup: autopilot was 'on' with no mission state (server restarted) — "
                "reset to 'off'. Re-engage autopilot from the UI."
            )
    except Exception:
        logger.exception("Startup: autopilot safety reset failed (non-fatal).")

    _scheduler_stop.clear()
    _scheduler_thread = threading.Thread(
        target=_scheduler_loop, daemon=True, name="maestro-scheduler"
    )
    _scheduler_thread.start()
    logger.info("Scheduler started (tick every %.1fs).", SCHEDULER_TICK_INTERVAL)

    _session_heartbeat_stop.clear()
    _session_heartbeat_thread = threading.Thread(
        target=_session_heartbeat_worker,
        daemon=True,
        name="maestro-session-heartbeat",
    )
    _session_heartbeat_thread.start()
    logger.info("Session heartbeat thread started (interval %ds).", _HEARTBEAT_SESSION_INTERVAL)


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
    global _scheduler_thread, _session_heartbeat_thread
    if _scheduler_thread is None:
        return

    # Stop the heartbeat thread first so it doesn't race with the drain below.
    _session_heartbeat_stop.set()
    if _session_heartbeat_thread and _session_heartbeat_thread.is_alive():
        _session_heartbeat_thread.join(timeout=5.0)
    _session_heartbeat_thread = None

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

    # Close any DB agent_session rows that threads left open (force-killed threads
    # never reach their own finally blocks, so their ended_at stays NULL).
    try:
        from app.database import close_zombie_sessions_by_session_id
        closed_tasks = close_zombie_sessions_by_session_id(exclude_ids=set())
        if closed_tasks:
            logger.info(
                "Scheduler shutdown: closed orphaned DB sessions for tasks: %s", closed_tasks
            )
    except Exception:
        logger.exception("Scheduler shutdown: failed to close orphaned DB sessions.")

    logger.info("Scheduler stopped.")


def clear_planning_stopped(task_id: str) -> None:
    """Remove task_id from the planning-stopped set so the scheduler will re-dispatch it.

    Called by the /run-planning endpoint when the user manually triggers a retry.
    """
    _planning_stopped.pop(task_id, None)
    # Also clear any rejection cooldown so the retry dispatches immediately.
    _rejection_cooldowns.pop(task_id, None)


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

    # Map open sessions to task IDs
    from app.database import get_open_sessions
    open_sessions = get_open_sessions()
    session_map = {}
    # We might have multiple sessions per task if the DB is messy; 
    # use the latest started_at for each task_id or job_id.
    for s in open_sessions:
        tid = s.task_id
        if not tid:
            continue
        if tid not in session_map or s.started_at > session_map[tid].started_at:
            session_map[tid] = s

    task_by_id = {t.id: t for t in all_tasks}
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()
    ready_ids: set[str] = {t["id"] for t in ready_tasks}

    # Refresh project-enabled cache once per tick, then filter disabled-project tasks out
    # of ready_tasks so _dispatch_pipeline_tasks_tiered never sees them.
    _refresh_project_enabled_cache()
    ready_tasks = [
        t for t in ready_tasks
        if _is_project_enabled_by_id(getattr(task_by_id.get(t["id"]), "project_id", None))
    ]

    # Big-idea parents (have children - skipped by DAG as non-dispatchable)
    children_by_parent: set[str] = {
        t.get("parent_task_id")
        for t in task_dicts
        if t.get("parent_task_id")
    }

    dispatchable_set = {s.lower() for s in SCHEDULER_DISPATCHABLE_TYPES}
    done_set = {s.lower() for s in PIPELINE_DONE_STATUSES}
    never_dispatch = {"completed", "cancelled", "subdividing", "accepted"}

    active_list: list[dict] = []
    queued_list: list[dict] = []
    blocked_list: list[dict] = []
    stopped_list: list[dict] = []

    # 1. Add scheduler-managed active tasks
    for tid in active_session_ids:
        task = task_by_id.get(tid)
        session = session_map.get(tid)
        
        last_act = session.last_activity_at if session else None
        idle_mins = 0
        is_zombie = False
        if last_act:
            try:
                la_dt = datetime.fromisoformat(last_act)
                # Ensure la_dt is timezone-aware if it's not (SQLite might store it plain)
                if la_dt.tzinfo is None:
                    la_dt = la_dt.replace(tzinfo=timezone.utc)
                idle_mins = (datetime.now(timezone.utc) - la_dt).total_seconds() / 60
                if idle_mins > 20:
                    is_zombie = True
            except Exception:
                pass

        if task:
            info = _llm_info(task.llm_id)
            active_list.append({
                "id": tid,
                "title": (task.title or tid)[:80],
                "type": (task.type or "").lower(),
                "project": task.project or "",
                "llm_id": task.llm_id,
                "llm_name": info["name"] if info else "(no LLM)",
                "last_activity_at": last_act,
                "idle_minutes": round(idle_mins, 1),
                "zombie": is_zombie,
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
                "last_activity_at": last_act,
                "idle_minutes": round(idle_mins, 1),
                "zombie": is_zombie,
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

        # Skip tasks belonging to disabled projects
        if not _is_project_enabled_by_id(getattr(task, "project_id", None)):
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

        if tid in _planning_stopped:
            # Stopped after exhausting design retries — needs manual re-trigger.
            entry["reason"] = _planning_stopped[tid]
            stopped_list.append(entry)
        elif tid in ready_ids:
            # Ready but not dispatched - determine why
            if task_type == 'idea':
                cs = getattr(task, 'clarification_status', 'none')
                if cs == 'pending':
                    reason = "clarifying"
                elif cs == 'awaiting_user':
                    reason = "awaiting_approval"
                elif cs != 'approved':
                    reason = "needs_clarification"
                else:
                    reason = None # Proceed to other checks
            else:
                reason = None

            if reason is None:
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
        t["llm_id"] for t in active_list + queued_list + blocked_list + stopped_list
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
        "stopped": stopped_list,
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


def _task_priority_tier(task_dict: dict) -> int:
    """Classify a pipeline task into dispatch tier.

    Tier 0: Human-created (owner='user', subdivision_generation=0) or starred.
            Always dispatched before Maestro work.
    Tier 1: Maestro-originated (owner='maestro').
            Dispatched before maintenance jobs (file summaries etc.).
    Tier 3: Everything else — subdivision children, factory-created, system work.
            Only dispatched when tiers 0–2 have no pending work.

    Tier 2 is deliberately unused for pipeline tasks; it is reserved for background
    job tables (file summaries, surveys, PIP resolution) which are dispatched as a
    separate phase in _tick().
    """
    if task_dict.get("is_starred"):
        return 0
    owner = task_dict.get("owner", "user") or "user"
    sub_gen = task_dict.get("subdivision_generation", 0) or 0
    if owner == "user" and sub_gen == 0:
        return 0
    if owner == "maestro":
        return 1
    return 3


def _compute_priority(task_dict: dict, by_id: dict) -> float:
    """Lower score = higher priority.

    Tier 0 (score ≤ 0):              human-created or starred
    Tier 1 (0 < score < 10_000_000): Maestro-originated tasks
    Tier 3 (score ≥ 30_000_000):     subdivision children, factory cards, system work

    Within each tier the most stale task (largest staleness_seconds) wins.
    Tier gaps of 10 M seconds (~115 days) ensure tiers never overlap regardless of
    staleness.  The 10–30 M range is intentionally unused, reserved for tier 2
    pipeline tasks if needed in future.
    """
    import datetime as _dt
    now = _dt.datetime.utcnow()
    raw = task_dict.get("last_progress_at")
    if isinstance(raw, str):
        try:
            raw = _dt.datetime.fromisoformat(raw)
        except ValueError:
            raw = None
    last_progress: _dt.datetime = raw if raw is not None else now
    staleness = (now - last_progress).total_seconds()

    tier = _task_priority_tier(task_dict)
    base = {0: 0.0, 1: 10_000_000.0, 3: 30_000_000.0}[tier]
    return base - staleness


def _free_slots(allowed_llm_id: "int | None") -> int:
    """Return the number of free parallel slots for the currently pinned LLM.

    Returns a large number when no LLM is pinned yet (nothing running) so the
    first dispatch is never gated.
    """
    if allowed_llm_id is None:
        return 999  # nothing pinned; let first dispatch pin it
    from app.database import get_llm as _get_llm
    llm = _get_llm(allowed_llm_id)
    if llm is None:
        return 0
    cap = getattr(llm, "parallel_sessions", 1) or 1
    with _llm_counts_lock:
        active = _llm_session_counts.get(allowed_llm_id, 0)
    return max(0, cap - active)


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
            # Also treat as loaded if this LLM was dispatched earlier in this tick:
            # a fast-completing thread may have decremented _llm_session_counts back
            # to 0 before we reach the next candidate, inflating the loaded-model cap.
            llm_already_loaded = llm_already_loaded or (llm_id in _tick_dispatched_llm_ids)
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

    # Record this LLM as dispatched this tick so llm_already_loaded stays True even
    # if the thread completes (and decrements _llm_session_counts) before the next
    # candidate in the same tick is evaluated.
    _tick_dispatched_llm_ids.add(llm_id)

    return True


def _check_model_block_timeout(task_id: str, required_llm_id: int) -> None:
    """If a task has been waiting for its required model beyond the timeout, mark it blocked."""
    from app.database import get_task
    from app.database.crud_tasks import mark_dispatch_waiting, set_task_blocked_on_model
    from app.agent.config import MODEL_BLOCK_TIMEOUT_MINUTES
    db_task = get_task(task_id)
    if db_task is None:
        return
    if db_task.dispatch_waiting_since is None:
        mark_dispatch_waiting(task_id)
        return
    wait_minutes = (datetime.utcnow() - db_task.dispatch_waiting_since.replace(tzinfo=None)).total_seconds() / 60
    if wait_minutes >= MODEL_BLOCK_TIMEOUT_MINUTES:
        logger.warning(
            "Task '%s' has been waiting %.1f min for LLM %d — marking blocked_on_model.",
            task_id, wait_minutes, required_llm_id,
        )
        set_task_blocked_on_model(task_id, required_llm_id)


def _dispatch_pipeline_tasks_tiered(
    tier: int,
    ready_tasks: list,
    allowed_llm_id: "int | None",
    node_active_counts: dict,
    node_session_counts: dict,
    node_obj_cache: dict,
    llm_node_cache: dict,
) -> "int | None":
    """Dispatch pipeline tasks of *tier* from the pre-sorted *ready_tasks* list.

    Returns the (possibly updated) allowed_llm_id.
    Tasks in *ready_tasks* are already DAG-ready and sorted by _compute_priority.
    Only tasks whose _task_priority_tier() equals *tier* are considered.
    """
    from app.database import get_task, get_llm, budget_has_capacity
    from app.database import get_project_path as _get_project_path
    from app.database import get_active_pip_resolution_jobs_for_task

    for task_dict in ready_tasks:
        if _task_priority_tier(task_dict) != tier:
            continue

        task_id = task_dict["id"]
        task_type = task_dict.get("type", "")

        if task_type in _SCHEDULER_SKIP_STAGE_TYPES:
            continue

        if task_type == "idea":
            from app.database import get_task as _get_task_dispatch
            _db_task = _get_task_dispatch(task_id)
            if _db_task and _db_task.intake_exhausted_at:
                continue
            cs = getattr(_db_task, 'clarification_status', 'none') if _db_task else 'none'
            if cs not in ('approved', 'skipped'):
                continue
            if task_id in _rejection_cooldowns:
                if time.time() - _rejection_cooldowns[task_id] < _REJECTION_RETRY_COOLDOWN:
                    continue

        with _active_sessions_lock:
            if task_id in _active_sessions and _active_sessions[task_id].is_alive():
                continue

        if task_type not in {"idea", "planning", "architecture"}:
            if get_active_pip_resolution_jobs_for_task(task_id):
                logger.debug(
                    "[pip] Skipping dispatch of '%s' (%s) — pip_resolution_jobs active.",
                    task_id, task_type,
                )
                continue

        if task_type == "planning":
            if task_id in _planning_stopped:
                continue
            if task_id in _rejection_cooldowns:
                if time.time() - _rejection_cooldowns[task_id] < _REJECTION_RETRY_COOLDOWN:
                    continue

        if task_id in _failed_cooldowns:
            if time.time() - _failed_cooldowns[task_id] < _FAIL_COOLDOWN_SECONDS:
                continue

        db_task = get_task(task_id)
        if not db_task:
            continue

        if not _is_project_enabled_by_id(getattr(db_task, 'project_id', None)):
            continue

        _content_blob = db_task.content or {}
        if _content_blob.get("_parked_at_stage") == task_type:
            continue

        if db_task.consultation_payload:
            try:
                cp = __import__("json").loads(db_task.consultation_payload)
                if cp.get("question") and not cp.get("hint"):
                    logger.debug("Skipping task '%s' - awaiting consultation hint.", task_id)
                    continue
            except Exception:
                pass

        from app.agent.config import resolve_llm_for_task
        required_llm_id = resolve_llm_for_task(db_task, db_task.stage_key or task_type)
        if not required_llm_id:
            continue

        llm = get_llm(required_llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            _check_model_block_timeout(task_id, required_llm_id)
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id
            logger.info("[%s] One-LLM policy: pinning to LLM %d (%s).", AGENT_NAME, llm.id, llm.model)

        worst = _estimate_worst_case_microcents(required_llm_id, db_task.budget_id)
        if worst > 0 and not budget_has_capacity(db_task.budget_id, worst):
            logger.info(
                "[%s] Skipping task '%s' - budget %s insufficient (%d µ¢ worst-case).",
                AGENT_NAME, task_id, db_task.budget_id, worst,
            )
            from app.database import append_task_history
            append_task_history(
                task_id, "budget_skip",
                message=f"Budget {db_task.budget_id} insufficient ({worst} µ¢ worst-case needed)",
            )
            continue

        project_path = None
        if db_task.project:
            project_path = _get_project_path(db_task.project)
            if project_path is None:
                logger.warning(
                    "Task '%s' project '%s' has no path - git tools use PROJECT_ROOT.",
                    task_id, db_task.project,
                )

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"task '{task_id}'",
        ):
            _check_model_block_timeout(task_id, required_llm_id)
            continue

        # Auto-assign llm_id on the task record (not pinned — routing table drove this)
        if db_task.llm_id != required_llm_id or db_task.dispatch_waiting_since is not None:
            from app.database import update_task
            update_task(task_id, llm_id=required_llm_id, llm_pinned=False,
                        dispatch_waiting_since=None, blocked_on_model_id=None)

        logger.info(
            "Dispatching task '%s' (type=%s, tier=%d) to LLM %d (%s:%d %s) [slot %d/%d].",
            task_id, task_type, tier, llm.id, llm.address, llm.port, llm.model,
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
            _session_types[task_id] = task_type
            _session_started_at[task_id] = time.time()
        try:
            thread.start()
        except Exception:
            logger.exception("Failed to start thread for task '%s'.", task_id)
            with _active_sessions_lock:
                _active_sessions.pop(task_id, None)
                _session_llm_ids.pop(task_id, None)
                _session_types.pop(task_id, None)
                _session_started_at.pop(task_id, None)
            with _llm_counts_lock:
                _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)

    return allowed_llm_id


def _tick() -> None:
    """
    Single scheduler tick — tiered priority dispatch.

    Tiers (higher tiers never wait for lower tiers to clear):
      Tier 0: Human-initiated work — clarification, Populate arch-gen, human pipeline cards
      Tier 1: Maestro-originated  — MaestroAgent stall/heartbeat, Maestro-created cards
      Tier 2: Maintenance         — file summaries, scope surveys, PIP resolution, research
      Tier 3: Background pipeline — subdivision children, factory cards, system-owned tasks

    Free LLM slots are filled from top tier downward each tick.  A lower tier only
    gets slots if there are no pending higher-tier jobs AND slots remain after dispatching
    higher-tier work.

    One-LLM-at-a-time policy: the llama.cpp router can only run one model at a time.
    Switching models requires unloading the current model first.  If we dispatch to
    multiple LLM IDs simultaneously the router thrashes between models and nothing
    makes progress.
    """
    # Do not dispatch new work once shutdown has been signalled.
    from app.agent.llm_client import is_shutting_down
    if is_shutting_down():
        return

    # Lazy imports to avoid circular deps at module load
    from app.agent.dag import DAGResolver
    from app.database import get_all_tasks, get_task, get_llm, get_compute_node

    # 0. Reset tick-local LLM dispatch tracking and cleanup finished sessions.
    global _tick_dispatched_llm_ids
    _tick_dispatched_llm_ids = set()
    _cleanup_finished()

    # 0a-pre. Kill sessions alive but LLM-idle for too long (hung tool calls, stalled HTTP).
    _recover_hung_sessions()

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

    # 0c. Clarification jobs — absolute highest priority.
    #     Run before the one-LLM policy check so clarification can pin the LLM
    #     before any pipeline task does. A new IDEA card's spec rewrite should
    #     never queue behind an already-running optimization or final-review.
    #     allowed_llm_id is None here (computed below), so clarification always
    #     gets the first free slot regardless of what else is queued.
    _dispatch_clarification_jobs(
        None, node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache,
    )

    # 1. Determine the currently pinned LLM (one-at-a-time policy).
    #    allowed_llm_id = None  → nothing is running; first dispatch pins a new LLM.
    #    allowed_llm_id = N     → only dispatch to LLM N until it drains completely.
    with _active_sessions_lock:
        active_llm_ids: set[int] = {
            lid for key, lid in _session_llm_ids.items()
            if key in _active_sessions and _active_sessions[key].is_alive()
        }
    with _external_sessions_lock:
        active_llm_ids.update(_external_sessions.values())
    allowed_llm_id: int | None = next(iter(active_llm_ids)) if active_llm_ids else None
    if allowed_llm_id is not None:
        logger.debug("[%s] One-LLM policy: pinned to LLM %d.", AGENT_NAME, allowed_llm_id)

    # 2. Pre-fetch all ready tasks (single DB round-trip) and sort by priority.
    #    Tasks are partitioned into tiers by _task_priority_tier() during dispatch.
    all_tasks = get_all_tasks()
    task_dicts = [_task_to_mini_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    ready_tasks = resolver.get_ready_tasks()
    if ready_tasks:
        by_id = {t["id"]: t for t in task_dicts}
        ready_tasks.sort(key=lambda t: _compute_priority(t, by_id))

    _cap_args = (node_active_counts, _node_session_counts, _node_obj_cache, _llm_node_cache)

    # ── TIER 0: Human-initiated ──────────────────────────────────────────────
    # Populate button arch-gen (tier=0), human-created pipeline tasks + starred cards.
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_arch_gen_jobs(allowed_llm_id, *_cap_args, only_tier=0)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_pipeline_tasks_tiered(0, ready_tasks, allowed_llm_id, *_cap_args)

    # ── TIER 1: Maestro-originated ───────────────────────────────────────────
    # MaestroAgent (stall recovery, heartbeat) and Maestro-created pipeline tasks.
    _expire_autopilot_objectives()  # cheap DB check; no LLM slot needed
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_maestro(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_heartbeat_maestro(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_pipeline_tasks_tiered(1, ready_tasks, allowed_llm_id, *_cap_args)

    # ── TIER 2: Maintenance / bookkeeping ────────────────────────────────────
    # File summaries, scope surveys, auto arch-gen, PIP resolution, research.
    # Stranded subdivision recovery and factory triggers are no-slot ops; always run.
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_file_summary_jobs(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_scope_survey_jobs(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_arch_gen_jobs(allowed_llm_id, *_cap_args, only_tier=2)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_pip_resolution_jobs(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_goal_verification_jobs(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_research_jobs(allowed_llm_id, *_cap_args)
    if _free_slots(allowed_llm_id) > 0:
        allowed_llm_id = _dispatch_episodic_summary_jobs(allowed_llm_id, *_cap_args)
    _dispatch_stranded_subdivisions(allowed_llm_id, *_cap_args)
    _dispatch_factory_triggers(allowed_llm_id)
    _run_episodic_cleanup()

    # ── TIER 3: Background pipeline cards ────────────────────────────────────
    # Subdivision children, factory-created cards, system-owned tasks.
    # Only run when all higher-tier work is satisfied.
    if _free_slots(allowed_llm_id) > 0:
        _dispatch_pipeline_tasks_tiered(3, ready_tasks, allowed_llm_id, *_cap_args)

    # ── API poll watches (Gap 9) ──────────────────────────────────────────────
    # No LLM slot needed to check; dispatch happens inside poll_due_watches.
    try:
        from app.agent.api_poller import poll_due_watches
        poll_due_watches()
    except Exception as _poll_exc:
        logger.debug("[Scheduler] api_poller error: %s", _poll_exc)

    # ── Training pipeline (Gap 11) ────────────────────────────────────────────
    # No LLM slot needed — pure DB reads/writes.
    global _last_training_score, _last_training_export_check
    _now = time.time()
    if (_now - _last_training_score) >= _TRAINING_SCORE_INTERVAL_SECONDS:
        _last_training_score = _now
        try:
            from app.database.crud_training import score_new_sessions
            import threading as _threading
            _threading.Thread(target=score_new_sessions, daemon=True,
                              name="training-scorer").start()
        except Exception as _train_exc:
            logger.debug("[Scheduler] training scorer error: %s", _train_exc)
    if (_now - _last_training_export_check) >= _TRAINING_EXPORT_INTERVAL_SECONDS:
        _last_training_export_check = _now
        try:
            from app.database.crud_training import count_qualified_unexported
            from app.agent.config import (
                TRAINING_EXPORT_THRESHOLD, TRAINING_EXPORT_MAX_PER_RUN,
                TRAINING_EXPORT_DIR, TRAINING_DEDUP_MAX,
            )
            if count_qualified_unexported() >= TRAINING_EXPORT_THRESHOLD:
                from app.agent.training_exporter import run_export
                import threading as _threading
                _threading.Thread(
                    target=run_export,
                    kwargs={"export_dir": TRAINING_EXPORT_DIR,
                            "export_max": TRAINING_EXPORT_MAX_PER_RUN,
                            "dedup_max": TRAINING_DEDUP_MAX},
                    daemon=True,
                    name="training-exporter",
                ).start()
        except Exception as _export_exc:
            logger.debug("[Scheduler] training export error: %s", _export_exc)


def _dispatch_heartbeat_maestro(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> "int | None":
    """Fire a MaestroAgent in heartbeat mode to survey system health every 5 minutes."""
    global _last_global_heartbeat
    from app.agent.config import MAESTRO_ENABLED
    from app.database import get_system_setting, get_llm, get_all_projects

    # Check if Maestro is enabled (dynamically or via config)
    maestro_enabled = get_system_setting("maestro_enabled", MAESTRO_ENABLED)
    if not maestro_enabled:
        return allowed_llm_id
    if is_shutting_down():
        return allowed_llm_id

    now = time.time()
    if (now - _last_global_heartbeat) < _HEARTBEAT_INTERVAL_SECONDS:
        return allowed_llm_id

    # Check if a heartbeat is already running
    session_key = "maestro-heartbeat"
    with _active_sessions_lock:
        if session_key in _active_sessions:
            return allowed_llm_id

    # 1. Try to get global Maestro config from system settings
    llm_id = get_system_setting("maestro_llm_id")
    budget_id = get_system_setting("maestro_budget_id")

    steward_name = "GlobalHeartbeat"
    steward_path = None

    # 2. Fallback to a steward project if not configured
    if not llm_id or not budget_id:
        projects = get_all_projects()
        for p in projects:
            if p.llm_id and p.budget_id:
                llm_id = llm_id or p.llm_id
                budget_id = budget_id or p.budget_id
                steward_name = p.name
                steward_path = p.path
                break

    if not llm_id or not budget_id:
        return allowed_llm_id

    llm = get_llm(llm_id)
    if not llm:
        return allowed_llm_id

    # Respect one-LLM policy
    if allowed_llm_id is not None and llm.id != allowed_llm_id:
        return allowed_llm_id
    if allowed_llm_id is None:
        allowed_llm_id = llm.id

    # Reserve slot
    if not _check_and_reserve_slot(
        llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
        label="maestro-heartbeat",
    ):
        return allowed_llm_id

    logger.info("[Heartbeat] Triggering system health survey via MaestroAgent (LLM %d).", llm.id)
    _last_global_heartbeat = now

    _start_maestro_heartbeat_thread(
        project_name=steward_name,
        project_path=steward_path,
        llm_id=llm_id,
        budget_id=budget_id,
        llm_base_url=f"http://{llm.address}:{llm.port}/v1",
        llm_model=llm.model,
    )
    return allowed_llm_id


def _start_maestro_heartbeat_thread(
    project_name: str,
    project_path: "str | None",
    llm_id: int,
    budget_id: int,
    llm_base_url: str,
    llm_model: str,
) -> None:
    """Spawn a thread for Maestro heartbeat monitor."""
    import asyncio as _asyncio
    from app.agent.maestro import MaestroAgent

    session_key = "maestro-heartbeat"

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            agent = MaestroAgent(
                project_name=project_name,
                project_path=project_path,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                is_heartbeat=True,
            )
            loop.run_until_complete(agent.run())
        except Exception as exc:
            logger.exception("[Heartbeat] Thread raised: %s", exc)
        finally:
            loop.close()
            with _llm_counts_lock:
                _llm_session_counts[llm_id] = max(0, _llm_session_counts[llm_id] - 1)
            with _active_sessions_lock:
                _active_sessions.pop(session_key, None)
                _session_llm_ids.pop(session_key, None)
                _session_titles.pop(session_key, None)
            logger.debug("[Heartbeat] Monitor thread exited.")

    t = threading.Thread(target=_run, daemon=True, name="maestro-heartbeat")
    with _active_sessions_lock:
        _active_sessions[session_key] = t
        _session_llm_ids[session_key] = llm_id
        _session_titles[session_key] = "System Heartbeat Monitor"
    t.start()



def _project_has_maestro_signal(project) -> bool:
    """Return True if the project has enough signal for the Maestro to operate on.

    A project has signal when at least one of the following is true:
      - Its filesystem path contains source files (substantive codebase exists).
      - It has at least one ACTIVE task of any type (human or AI placed work here).

    Soft-deleted tasks are deliberately excluded — they represent cleaned-up
    history, not current intent.  A project the user has emptied and whose
    filesystem path is empty or absent is a placeholder shell; the Maestro
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
    except Exception as e:
        logger.warning("[Maestro] Signal query failed: %s", e)
        pass
    finally:
        db.close()

    # Check if the project path contains any source files
    if project.path and os.path.isdir(project.path):
        logger.info("[Maestro] Signal: scanning path %s", project.path)
        from app.agent.path_filter import walk_safe
        for _root, dirs, files in walk_safe(project.path):
            if files:
                return True

    return False

def _dispatch_maestro(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> None:
    """Fire a MaestroAgent for any project that has been stalled long enough.

    "Stalled" = no TransitionResult created for the project's tasks in the last
    MAESTRO_STALL_TICKS * SCHEDULER_TICK_INTERVAL seconds.

    One Maestro per project at a time.  Maestros MUST respect LLM/node capacity —
    each Maestro holds one session slot for its entire run so it doesn't pile on
    top of a full pipeline load.  The session key is 'maestro-{project_name}'.
    """
    from app.agent.config import (
        MAESTRO_ENABLED, MAESTRO_STALL_TICKS, SCHEDULER_TICK_INTERVAL,
    )
    from app.database import get_system_setting

    # Check if Maestro is enabled (dynamically or via config)
    maestro_enabled = get_system_setting("maestro_enabled", MAESTRO_ENABLED)
    if not maestro_enabled:
        return

    # Autopilot gate: honour the on/off toggle and scheduled hours
    if not _should_autopilot_dispatch():
        return

    # Tick mission state machine (check termination conditions)
    _tick_mission()

    if is_shutting_down():        return

    from app.database import get_all_projects, get_tasks_by_project, get_llm
    from app.database import get_transition_results
    from app.database.session import SessionLocal
    from app.database.models import TransitionResult, Task

    stall_threshold_secs = MAESTRO_STALL_TICKS * SCHEDULER_TICK_INTERVAL
    now = time.time()

    projects = get_all_projects()
    for project in projects:
        project_name = project.name

        # Skip projects without an LLM or budget configured
        if not project.llm_id or not project.budget_id:
            continue

        # Per-project autopilot override
        from app.database import get_project_setting as _gps
        proj_override = _gps(project.id, "autopilot_override", "inherit")
        if proj_override == "force_off":
            continue  # this project opts out of autonomous dispatch

        # Skip if a Maestro is already running for this project
        with _active_maestro_lock:
            if project_name in _active_maestro_projects:
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
            logger.debug("[Maestro] Activity query failed for '%s': %s", project_name, exc)
            continue

        # Initialise the grace-period clock on first sight
        with _project_last_activity_lock:
            if project_name not in _project_last_activity:
                _project_last_activity[project_name] = last_tr_time or now
            # Update stored value if DB shows more recent activity
            if last_tr_time and last_tr_time > _project_last_activity[project_name]:
                _project_last_activity[project_name] = last_tr_time
            last_activity = _project_last_activity[project_name]

        # Determine if any task is thrashing (high demotion count) or stagnant (>24h)
        is_thrashing = False
        is_stagnant = False
        stagnant_task_id = None
        
        try:
            db = SessionLocal()
            try:
                # 1. Thrashing Check (bouncing between stages)
                thrashing_task = (
                    db.query(Task.id)
                      .filter(Task.project_id == project.id, Task.is_active == True, Task.demotion_count >= 5)
                      .first()
                )
                if thrashing_task:
                    is_thrashing = True
                    _log_project_maestro(
                        project_name, "thrashing",
                        f"[Maestro] Project '{project_name}' detected thrashing task: {thrashing_task[0]}"
                    )
                
                # 2. Stagnation Check (no progress for >24h)
                # We check transition_results for the latest timestamp per task
                from sqlalchemy import func
                subq = (
                    db.query(TransitionResult.task_id, func.max(TransitionResult.created_at).label("max_ca"))
                      .join(Task, Task.id == TransitionResult.task_id)
                      .filter(Task.project_id == project.id, Task.is_active == True)
                      .group_by(TransitionResult.task_id)
                      .subquery()
                )
                
                stagnant_threshold = datetime.utcnow() - timedelta(hours=24)
                stagnant_task = db.query(subq.c.task_id).filter(subq.c.max_ca < stagnant_threshold).first()
                
                if stagnant_task:
                    is_stagnant = True
                    stagnant_task_id = stagnant_task[0]
                    _log_project_maestro(
                        project_name, "stagnant",
                        f"[Maestro] Project '{project_name}' detected stagnant task: {stagnant_task_id} (>24h no progress)"
                    )

            finally:
                db.close()
        except Exception as e:
            logger.warning("[Maestro] Fault detection query failed for '%s': %s", project_name, e)
            pass

        if (now - last_activity) < stall_threshold_secs and not is_thrashing and not is_stagnant:
            # logger.debug("[Maestro] Skipping '%s' — active and healthy.", project_name)
            continue  # project is active and healthy — skip

        _log_project_maestro(
            project_name, "eligible",
            f"[Maestro] Project '{project_name}' eligible (stalled, thrashing, or stagnant). Checking signal..."
        )

        # Guard: only fire when the project has substantive signal to work with.
        if not _project_has_maestro_signal(project):
            _log_project_maestro(
                project_name, "no_signal",
                f"[Maestro] Skipping '{project_name}' — no signal."
            )
            continue

        # Fire Maestro
        llm = get_llm(project.llm_id)
        if not llm:
            _log_project_maestro(
                project_name, "no_llm",
                f"[Maestro] Skipping '{project_name}' — no LLM assigned."
            )
            continue

        # One-LLM-at-a-time: respect the pinned LLM for this tick
        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            _log_project_maestro(
                project_name, "llm_not_pinned",
                f"[Maestro] Skipping '{project_name}' — LLM {llm.id} not pinned (pinned={allowed_llm_id})."
            )
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

        # Check and reserve a capacity slot — Maestros must not bypass limits
        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=f"maestro-steward '{project_name}'",
        ):
            _log_project_maestro(
                project_name, "at_capacity",
                f"[Maestro] Skipping '{project_name}' — LLM {llm.id} at capacity."
            )
            continue

        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        llm_model    = llm.model

        if is_thrashing:
            logger.info(
                "[Maestro] Project '%s' has thrashing tasks — starting MaestroAgent (LLM %d).",
                project_name, llm.id,
            )
        elif is_stagnant:
            logger.info(
                "[Maestro] Project '%s' has stagnant tasks — starting MaestroAgent (LLM %d).",
                project_name, llm.id,
            )
        else:
            logger.info(
                "[Maestro] Project '%s' stalled for %.0fs — starting MaestroAgent (LLM %d).",
                project_name, now - last_activity, llm.id,
            )

        with _active_maestro_lock:
            _active_maestro_projects.add(project_name)
        # Reset the activity clock so we don't fire again immediately after completion
        with _project_last_activity_lock:
            _project_last_activity[project_name] = now

        _start_maestro_thread(
            project_name=project_name,
            project_path=project.path,
            llm_id=project.llm_id,
            budget_id=project.budget_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
        )

        # Autopilot objectives tick — runs alongside Maestro stall response
        _trigger_autopilot_tick(project.id, project_name)

    return allowed_llm_id


def _start_maestro_thread(
    project_name: str,
    project_path: "str | None",
    llm_id: int,
    budget_id: int,
    llm_base_url: str,
    llm_model: str,
) -> None:
    """Spawn a daemon thread that runs MaestroAgent.run() for one project.

    The slot was already reserved by _check_and_reserve_slot in _dispatch_maestro.
    Here we register the thread in _active_sessions / _session_llm_ids so that:
      - _cleanup_finished() can spot when it dies and decrement _llm_session_counts
      - the one-LLM-at-a-time policy pins the Maestro's LLM for the duration
      - scheduler status endpoints show the Maestro as an active session
    """
    import asyncio as _asyncio
    from app.agent.maestro import MaestroAgent

    session_key = f"maestro-{project_name}"

    def _run():
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            agent = MaestroAgent(
                project_name=project_name,
                project_path=project_path,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
            loop.run_until_complete(agent.run())
        except Exception as exc:
            logger.exception("[Maestro] Thread for '%s' raised: %s", project_name, exc)
        finally:
            loop.close()
            # Release the capacity slot — mirrors what _cleanup_finished does for
            # normal sessions, but the Maestro does it explicitly so the count
            # drops immediately when the thread exits rather than waiting for the
            # next cleanup pass.
            with _llm_counts_lock:
                _llm_session_counts[llm_id] = max(0, _llm_session_counts[llm_id] - 1)
            with _active_sessions_lock:
                _active_sessions.pop(session_key, None)
                _session_llm_ids.pop(session_key, None)
                _session_titles.pop(session_key, None)
            with _active_maestro_lock:
                _active_maestro_projects.discard(project_name)
            logger.debug(
                "[Maestro] Thread for '%s' exited (LLM %d slot released).",
                project_name, llm_id,
            )

    t = threading.Thread(target=_run, daemon=True, name=f"maestro-steward-{project_name[:16]}")
    # Register in the session tracking so the scheduler sees this as an active slot
    with _active_sessions_lock:
        _active_sessions[session_key] = t
        _session_llm_ids[session_key] = llm_id
        _session_titles[session_key] = f"Maestro: {project_name}"
    t.start()


def _run_autopilot_tick_for_project(project_id: int, project_name: str) -> None:
    """Run one autopilot tick for a project: spin detection + LLM assessment + card creation.

    Called from a background thread (non-blocking from the scheduler's perspective).
    """
    from app.database import (
        list_objectives, get_in_flight_count, update_objective_status,
        record_assessment, create_task, get_budget_spent_microcents,
        get_budget,
    )
    from app.database.session import SessionLocal
    from app.database.models import AutopilotObjective, Task, Project
    from app.agent.config import (
        AUTOPILOT_MAX_OBJECTIVES_PER_TICK,
        AUTOPILOT_SPIN_DEMOTION_THRESHOLD,
        AUTOPILOT_SPIN_CARD_THRESHOLD,
        AUTOPILOT_ASSESSMENT_MAX_TURNS,
    )

    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return

        active_objs = list_objectives(project_id, status="active")
        if not active_objs:
            return

        in_flight = get_in_flight_count(project_id)
        if in_flight >= project.autopilot_max_in_flight:
            logger.info(
                "[autopilot] project '%s' suppressed — board saturated (%d/%d in-flight)",
                project_name, in_flight, project.autopilot_max_in_flight,
            )
            return

        if project.autopilot_budget_id:
            budget = get_budget(project.autopilot_budget_id)
            if budget and budget.dollar_amount >= 0:
                spent = get_budget_spent_microcents(project.autopilot_budget_id)
                limit = int(budget.dollar_amount * 100_000_000)
                if spent >= limit:
                    logger.info(
                        "[autopilot] project '%s' suppressed — autopilot budget exhausted",
                        project_name,
                    )
                    return
    finally:
        db.close()

    # Process top-priority objectives (up to cap)
    for obj in active_objs[:AUTOPILOT_MAX_OBJECTIVES_PER_TICK]:
        try:
            _run_objective_assessment(obj, project_id, project_name)
        except Exception:
            logger.warning(
                "[autopilot] assessment failed for objective %d", obj.id, exc_info=True
            )


def _detect_spin(objective_id: int) -> bool:
    """Return True if this objective's spawned cards have cycled past the demotion threshold."""
    from app.database.session import SessionLocal
    from app.database.models import Task
    from app.agent.config import AUTOPILOT_SPIN_DEMOTION_THRESHOLD, AUTOPILOT_SPIN_CARD_THRESHOLD

    db = SessionLocal()
    try:
        count = (
            db.query(Task)
            .filter(
                Task.autopilot_objective_id == objective_id,
                Task.demotion_count >= AUTOPILOT_SPIN_DEMOTION_THRESHOLD,
                Task.is_active == True,
            )
            .count()
        )
        return count >= AUTOPILOT_SPIN_CARD_THRESHOLD
    finally:
        db.close()


def _run_objective_assessment(obj, project_id: int, project_name: str) -> None:
    """Run LLM self-assessment for one objective; create cards and update DB state."""
    import asyncio as _asyncio
    import json as _json
    from datetime import datetime, timezone
    from app.database import (
        record_assessment, update_objective_status,
        create_task, get_task, get_tasks_by_project,
        get_system_setting,
    )
    from app.database.session import SessionLocal
    from app.database.models import Task, AutopilotObjective, Project
    from app.agent.config import (
        AUTOPILOT_ASSESSMENT_MAX_TURNS,
        ORCHESTRATION_LLM_ID,
    )

    # --- spin detection (cheap, DB-only) ---
    if _detect_spin(obj.id):
        note = (
            "Auto-paused: spin detected. Multiple spawned cards have been demoted "
            "repeatedly without making progress. Human review needed."
        )
        update_objective_status(obj.id, "paused")
        record_assessment(obj.id, note, tick=0, appears_complete=False)
        logger.info("[autopilot] objective %d paused — spin detected", obj.id)
        return

    # --- resolve LLM to use for assessment ---
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            return
        maestro_llm_id = project.maestro_llm_id or ORCHESTRATION_LLM_ID
        if maestro_llm_id is None:
            raw = get_system_setting("maestro_llm_id")
            maestro_llm_id = int(raw) if raw else None
        if maestro_llm_id is None:
            logger.info(
                "[autopilot] objective %d skipped — no maestro_llm_id configured", obj.id
            )
            return
        budget_id = project.autopilot_budget_id or project.budget_id

        # Build tagged-card list for prompt
        tagged_tasks = (
            db.query(Task)
            .filter(Task.autopilot_objective_id == obj.id, Task.is_active == True)
            .all()
        )
        card_lines = "\n".join(
            f"  - [{t.stage_key or t.type}] {t.title} (demotions: {t.demotion_count})"
            for t in tagged_tasks
        ) or "  (none yet)"
    finally:
        db.close()

    time_box_str = f"{obj.time_box_hours}h" if obj.time_box_hours else "none"
    created_str = obj.created_at.isoformat() if obj.created_at else "unknown"
    prior_notes = obj.last_assessment or "(no prior assessment)"

    assessment_prompt = f"""You are The Maestro. Evaluate progress toward an autopilot objective and decide the next action.

Objective: {obj.description}
Time box: {time_box_str}
Created: {created_str}

Cards spawned by this objective:
{card_lines}

Prior assessment notes:
{prior_notes}

Evaluate and respond with a JSON object ONLY (no markdown, no explanation):
{{
  "appears_complete": <true|false>,
  "stuck": <true|false>,
  "assessment_notes": "<narrative summary of progress>",
  "new_cards": [
    {{"title": "<card title>", "description": "<brief description>"}}
  ]
}}

Rules:
1. appears_complete = true ONLY if you are confident the objective is fully achieved.
2. stuck = true if the objective is making no meaningful progress and a human should decide.
3. new_cards: 0-3 IDEA card proposals. Empty list if no new work is needed.
4. Dead ends are progress — "we know X doesn't work" is forward motion.
"""

    # --- call LLM via ConsultAgent-style single-turn call ---
    try:
        from app.agent.llm_client import call_llm
        from app.database import get_llm

        llm = get_llm(maestro_llm_id)
        if not llm:
            return
        llm_base_url = f"http://{llm.address}:{llm.port}/v1"

        loop = _asyncio.new_event_loop()
        try:
            raw_response = loop.run_until_complete(
                call_llm(
                    messages=[{"role": "user", "content": assessment_prompt}],
                    llm_id=maestro_llm_id,
                    budget_id=budget_id,
                    model=llm.model,
                    base_url=llm_base_url,
                    max_tokens=2048,
                    session_id=None,
                )
            )
        finally:
            loop.close()
    except Exception as exc:
        logger.warning("[autopilot] LLM call failed for objective %d: %s", obj.id, exc)
        return

    # --- parse response ---
    content = ""
    if raw_response and raw_response.get("choices"):
        content = raw_response["choices"][0].get("message", {}).get("content", "")

    try:
        # Strip markdown fences if present
        text = content.strip()
        if text.startswith("```"):
            text = text.split("```", 2)[-1].lstrip("json").strip().rstrip("`")
        result = _json.loads(text)
    except Exception:
        logger.warning(
            "[autopilot] Failed to parse JSON from assessment for objective %d: %r",
            obj.id, content[:200],
        )
        return

    appears_complete = bool(result.get("appears_complete", False))
    stuck = bool(result.get("stuck", False))
    notes = str(result.get("assessment_notes", ""))
    new_cards = result.get("new_cards", [])

    # --- record assessment in DB ---
    record_assessment(obj.id, notes, tick=0, appears_complete=appears_complete)

    # --- handle stuck ---
    if stuck:
        update_objective_status(obj.id, "paused")
        logger.info("[autopilot] objective %d paused — LLM assessed as stuck", obj.id)
        return

    # --- multi-tick completion confirmation ---
    db = SessionLocal()
    try:
        obj_fresh = db.query(AutopilotObjective).filter(AutopilotObjective.id == obj.id).first()
        if appears_complete and obj_fresh and obj_fresh.appears_complete_since is not None:
            from app.database import complete_objective
            complete_objective(obj.id)
            logger.info(
                "[autopilot] objective %d marked complete (confirmed on second tick)", obj.id
            )
            return
    finally:
        db.close()

    # --- create new IDEA cards ---
    from app.agent.config import MAESTRO_CAPABILITIES
    if isinstance(new_cards, list) and MAESTRO_CAPABILITIES.can_create_cards:
        for card_spec in new_cards[:3]:
            if not isinstance(card_spec, dict):
                continue
            title = str(card_spec.get("title", "")).strip()
            description = str(card_spec.get("description", "")).strip()
            if not title:
                continue
            try:
                create_task(
                    title=title,
                    description=description,
                    task_type="idea",
                    project_id=project_id,
                    stage_key="idea",
                    autopilot_objective_id=obj.id,
                )
                logger.info(
                    "[autopilot] created IDEA card '%s' for objective %d", title, obj.id
                )
            except Exception as exc:
                logger.warning(
                    "[autopilot] failed to create card '%s': %s", title, exc
                )


def _trigger_autopilot_tick(project_id: int, project_name: str) -> None:
    """Spawn a short-lived daemon thread to run an autopilot tick without blocking the scheduler."""
    t = threading.Thread(
        target=_run_autopilot_tick_for_project,
        args=(project_id, project_name),
        daemon=True,
        name=f"autopilot-tick-{project_id}",
    )
    t.start()


def _expire_autopilot_objectives() -> None:
    """Flip any time-boxed objectives that have passed their expires_at to complete."""
    from app.database.session import SessionLocal
    from app.database.models import AutopilotObjective
    from app.database import complete_objective
    from datetime import datetime, timezone

    db = SessionLocal()
    try:
        now = datetime.now(timezone.utc)
        expired = (
            db.query(AutopilotObjective)
            .filter(
                AutopilotObjective.status == "active",
                AutopilotObjective.expires_at != None,
                AutopilotObjective.expires_at <= now,
            )
            .all()
        )
        for obj in expired:
            db.close()
            db = None
            complete_objective(obj.id)
            logger.info(
                "[autopilot] objective %d expired — marked complete (time_box reached)", obj.id
            )
            db = SessionLocal()
    except Exception as exc:
        logger.warning("[autopilot] expire check failed: %s", exc)
    finally:
        if db:
            db.close()


def _dispatch_clarification_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: dict,
    node_session_counts: dict,
    node_obj_cache: dict,
    llm_node_cache: dict,
) -> "int | None":
    """Dispatch IDEA cards with clarification_status='pending'.

    Runs FIRST in every tick — before file summaries, arch-gen, and pipeline tasks —
    so a newly created IDEA card's clarification always gets the next free LLM slot.
    """
    from app.database import get_tasks_needing_clarification, get_llm
    from app.agent.clarify import run_clarification_for_task

    pending_tasks = get_tasks_needing_clarification()
    for task in pending_tasks:
        job_key = f"clarify-{task.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(task.llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=job_key,
        ):
            continue

        thread = threading.Thread(
            target=run_clarification_for_task,
            args=(task.id,),
            daemon=True,
            name=f"maestro-clarify-{task.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Clarification: {(task.title or '')[:60]}"
            _session_types[job_key] = "clarification"
        thread.start()
        logger.info("[Scheduler] Dispatched clarification for task '%s' on LLM %d", task.id, llm.id)
        allowed_llm_id = llm.id  # pin LLM for subsequent dispatch calls this tick

    return allowed_llm_id


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

        # Throttling: Skip projects with recent failures/rescues or disabled projects
        if job.task_id:
            task = _get_task(job.task_id)
            if task:
                if not _is_project_enabled_by_id(getattr(task, "project_id", None)):
                    continue
                if task.project and _is_project_in_failure_cooldown(task.project):
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
) -> "int | None":
    """Dispatch pending research jobs that have an LLM assigned.

    Respects the one-LLM-at-a-time policy and full node/LLM capacity caps.
    Returns the (possibly updated) allowed_llm_id.
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
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

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

    return allowed_llm_id


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


def _dispatch_goal_verification_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> "int | None":
    """Dispatch pending goal verification jobs. Runs in the maintenance tier."""
    from app.database import get_pending_goal_verification_jobs, update_goal_verification_job, get_llm

    pending = get_pending_goal_verification_jobs(limit=3)
    for job in pending:
        if not job.llm_id:
            continue

        job_key = f"goal-verify-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=job_key,
        ):
            continue

        update_goal_verification_job(job.id, status="running")

        thread = threading.Thread(
            target=_run_goal_verification_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-goal-verify-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Goal Verify: goal {job.goal_id}"
        thread.start()

    return allowed_llm_id


def _run_goal_verification_job(job: Any, llm: Any) -> None:
    """Worker thread for a single goal verification job."""
    from app.database import update_goal_verification_job

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    job_key = f"goal-verify-{job.id}"
    try:
        from app.agent.goal_verifier import run_goal_verification
        result = loop.run_until_complete(run_goal_verification(
            job_id=job.goal_id,
            llm_id=job.llm_id,
            budget_id=job.budget_id,
        ))
        update_goal_verification_job(
            job.id,
            status="done",
            result=result.get("verdict"),
            prompt_tokens=result.get("prompt_tokens", 0),
            completion_tokens=result.get("completion_tokens", 0),
        )
        logger.debug("[goal_verify] job %d completed (goal=%d).", job.id, job.goal_id)
    except ShutdownError:
        logger.info("[goal_verify] job %d aborted due to server shutdown.", job.id)
        update_goal_verification_job(job.id, status="failed", error_msg="Server shutdown")
    except Exception as exc:
        logger.exception("[goal_verify] job %d failed.", job.id)
        update_goal_verification_job(job.id, status="failed", error_msg=str(exc))
    finally:
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        with _active_sessions_lock:
            _active_sessions.pop(job_key, None)
            _session_llm_ids.pop(job_key, None)
            _session_titles.pop(job_key, None)
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
    only_tier: "int | None" = None,
) -> "int | None":
    """Dispatch pending arch gen jobs.

    If *only_tier* is given, only dispatch jobs whose tier matches that value.
    Respects the one-LLM-at-a-time policy and full node/LLM capacity caps.
    Returns the (possibly updated) allowed_llm_id.
    """
    from app.database import get_pending_arch_gen_jobs, update_arch_gen_job, get_llm

    pending = get_pending_arch_gen_jobs(limit=5)
    for job in pending:
        if not job.llm_id:
            continue

        # Tier filter: skip jobs that don't match the requested tier
        if only_tier is not None and getattr(job, "tier", 2) != only_tier:
            continue

        # Skip disabled projects and throttle projects with recent failures
        if not _is_project_enabled_by_name(getattr(job, "project", None)):
            continue
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
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

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

    return allowed_llm_id


def _dispatch_scope_survey_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> "int | None":
    """Dispatch pending ScopeSurveyJobs. Runs in the maintenance tier.
    Returns the (possibly updated) allowed_llm_id.
    """
    from app.database import get_pending_scope_survey_jobs, get_llm, update_scope_survey_job
    from app.agent.config import SURVEY_MAX_CONCURRENT_JOBS

    pending = get_pending_scope_survey_jobs(limit=SURVEY_MAX_CONCURRENT_JOBS)
    for job in pending:
        if not job.llm_id:
            continue

        if not _is_project_enabled_by_name(getattr(job, "project_name", None)):
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
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

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

    return allowed_llm_id


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
    if _session_id is not None:
        register_db_session(session_key, _session_id)

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
            import subprocess as _subprocess
            diff_text = ""
            if existing.file_paths:
                file_list = json.loads(existing.file_paths)
                diff_args = ["git", "diff", f"{existing.git_commit or 'HEAD^'}..HEAD", "--"] + file_list[:20]
                try:
                    _result = _subprocess.run(
                        diff_args, cwd=project_root, capture_output=True, text=True, timeout=30
                    )
                    diff_text = _result.stdout
                except Exception:
                    diff_text = ""

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
            import subprocess as _subprocess
            diff_text = ""
            if existing.file_paths:
                file_list = json.loads(existing.file_paths)
                diff_args = ["git", "diff", f"{existing.git_commit or 'HEAD^'}..HEAD", "--"] + file_list[:20]
                try:
                    _result = _subprocess.run(
                        diff_args, cwd=project_root, capture_output=True, text=True, timeout=30
                    )
                    diff_text = _result.stdout
                except Exception:
                    diff_text = ""

            prompt = (
                f"You are updating the summary for the '{job.scope_key}' {job.scope_type} in project '{job.project_name}'.\n"
                f"Old Summary:\n{existing.summary}\n\n"
                f"Git Diff of changes:\n{diff_text}\n\n"
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
        
        from app.agent.tools import build_tool_schemas, dispatch_tool
        cluster_tools = build_tool_schemas(["submit_work"])

        if job.scope_type == "module_clustering":
            prompt = (
                f"Given these file summaries for project '{job.project_name}', group them into 3-8 logical modules. "
                "A module may span multiple directories. For each module, provide a name, a 2-sentence purpose, "
                "and the list of files it contains.\n"
                "To complete your clustering, call the submit_work tool with:\n"
                "payload=[{\"name\": \"...\", \"purpose\": \"...\", \"files\": [\"...\", ...]}, ...]\n\n"
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
            tools=cluster_tools if job.scope_type == "module_clustering" else None,
            tool_choice="auto" if job.scope_type == "module_clustering" else None,
            llm_id=llm.id,
            budget_id=job.budget_id,
            max_tokens=SURVEY_SUMMARY_MAX_TOKENS,
        ))

        assistant_msg = resp.get("message") or {}
        content = assistant_msg.get("content", "")
        tool_calls = assistant_msg.get("tool_calls", [])

        if job.scope_type == "module_clustering":
            modules = None
            if tool_calls:
                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )
                    if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                        modules = json.loads(tc_result).get("payload")
                        break
            
            if modules is None:
                # Fallback to basic JSON extraction
                try:
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0].strip()
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0].strip()
                    modules = json.loads(content)
                except Exception:
                    modules = []

            if modules:
                try:
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
                    logger.error(f"Failed to process module clustering results: {e}")
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
    if _session_id is not None:
        register_db_session(task_id, _session_id)

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
) -> "int | None":
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

        if not _is_project_enabled_by_id(getattr(task, "project_id", None)):
            continue

        llm = get_llm(task.llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

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

    return allowed_llm_id


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
    if _session_id is not None:
        register_db_session(job_key, _session_id)
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
    if _session_id is not None:
        register_db_session(resolve_key, _session_id)
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
    """Detect tasks mid-subdivision (type='subdividing') with no children, and re-trigger.

    Only recovers type == 'subdividing' with 0 children — subdivision started but crashed or
    produced nothing.  Tasks sitting in IDEA with a prior subdivide vote are NOT auto-recovered
    here; they require a manual trigger so the user stays in control.
    """
    from app.database import get_all_tasks, get_task, get_llm

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

        if ttype != "subdividing":
            continue
        if child_counts.get(tid, 0) > 0:
            continue  # Has children - not stranded

        # type == 'subdividing' with 0 children: always stranded regardless

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

        stored_result: dict = {"outcome": "subdivide", "votes": []}

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
    import uuid
    from app.agent.llm_client import set_llm_session_context, clear_killed_session
    session_id = str(uuid.uuid4())
    set_llm_session_context(session_id)

    with _active_sessions_lock:
        _session_ids[task_id] = session_id

    # worktree_path is set before the try so the finally block can always clean up.
    worktree_path = project_path

    try:
        # Bootstrap: ensure git repo + venv exist before first worktree creation.
        if project_path:
            from app.agent.worktree import ensure_project_ready
            ensure_project_ready(project_path)

        # Worktree isolation: give each task its own git checkout.
        if project_path:
            from app.agent.worktree import setup_task_worktree
            wt = setup_task_worktree(task_id, project_path)
            if wt is None:
                raise WorktreeIsolationError(
                    f"Task '{task_id}': cannot create git worktree at '{project_path}'. "
                    f"Common causes: (1) not a git repo — fix with: git init && git commit --allow-empty -m 'init'; "
                    f"(2) ghost worktree directory exists with locked files — check server log for '[worktree]' entries."
                )
            worktree_path = wt

        llm_base_url = f"http://{llm.address}:{llm.port}/v1"
        llm_model = llm.model
        max_context = llm.max_context
        llm_id = llm.id
        budget_id = db_task.budget_id if db_task else None

        from app.agent.pipeline_router import dispatch_task as _pipeline_dispatch
        dispatched = _pipeline_dispatch(
            task_id,
            task_type,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
            project_path=worktree_path,
        )
        if not dispatched:
            # dispatch_task() returns False for: (a) no stage config found (legacy
            # tasks without a pipeline template), or (b) human_gate / terminal
            # agent types.  Only fall back to MaestroLoop for case (a).
            from app.agent.pipeline_router import get_stage_config as _get_sc
            _sc = _get_sc(task_id)
            if _sc is not None:
                logger.info(
                    "[scheduler] Task '%s' stage '%s' (agent_type='%s') not auto-dispatched — skipping.",
                    task_id, task_type, _sc.agent_type,
                )
            else:
                # No pipeline template — legacy fallback to MaestroLoop
                _run_maestro_loop(task_id, llm_base_url, llm_model, max_context, llm_id, budget_id, worktree_path)
    except TaskDeactivatedError as exc:
        logger.info("Task '%s' session halted: %s", task_id, exc)
    except WorktreeIsolationError as exc:
        _failed_cooldowns[task_id] = time.time()
        logger.error("[scheduler] Task '%s' aborted — worktree isolation failed: %s", task_id, exc)
        try:
            from app.database import append_task_history
            append_task_history(task_id, "worktree_isolation_error", message=str(exc))
        except Exception:
            pass
    except ShutdownError:
        logger.info("Task '%s' dispatch aborted due to server shutdown.", task_id)
    except Exception:
        _failed_cooldowns[task_id] = time.time()
        logger.exception("Task '%s' failed in scheduler dispatch (cooldown %ds).", task_id, int(_FAIL_COOLDOWN_SECONDS))
    finally:
        with _active_sessions_lock:
            _session_ids.pop(task_id, None)
        clear_killed_session(session_id)
        
        with _llm_counts_lock:
            _llm_session_counts[llm.id] = max(0, _llm_session_counts[llm.id] - 1)
        if project_path and worktree_path != project_path:
            from app.agent.worktree import teardown_task_worktree
            teardown_task_worktree(task_id, project_path)


def _run_intake(task_id: str, llm_base_url: str, llm_model: str,
                max_context: int | None = None,
                llm_id: int | None = None,
                budget_id: int | None = None,
                project_path: str | None = None) -> None:
    """Run the intake pipeline for an IDEA task."""
    from app.agent.intake import run_intake_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import (
        get_task, get_all_tasks, update_task,
        create_transition_vote, create_transition_result,
        create_agent_session, close_agent_session,
    )
    from app.agent.pipeline_router import advance_stage

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
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
            advance_stage(task_id, "pass", from_stage="idea")
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
    domain: str = "software",
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
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
        domain=domain,
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
    from app.agent.pipeline_router import advance_stage

    task = get_task(task_id)
    if not task:
        return

    # Derive planning domain from the project's pipeline template.
    _pipeline_template_id = getattr(task, 'pipeline_template_id', None)
    try:
        from app.agent.planning import _get_domain as _gd
        _domain = _gd(_pipeline_template_id, task.title, task.description or "")
    except Exception:
        _domain = "software"
    logger.debug("[planning] Task '%s' domain=%r (pipeline_template_id=%s)", task_id, _domain, _pipeline_template_id)

    # --- Planning cache gate ---
    # Compute content hash for this task spec. Used both to check for a reusable
    # cached result and to mark the result after the gate passes.
    _content_hash = None
    try:
        from hashlib import sha256 as _sha256
        _content_hash = _sha256(f"{task.title}||{task.description or ''}".encode()).hexdigest()
    except Exception:
        pass

    _cache_mode = getattr(task, 'cache_mode', None) or 'normal'
    if _cache_mode == 'normal' and _content_hash:
        try:
            from app.database import get_reusable_planning_result, restore_planning_result
            from app.database import supersede_planning_results as _spr_cache
            _cached = get_reusable_planning_result(task_id, _content_hash)
            if _cached:
                _spr_cache(task_id)
                restore_planning_result(_cached.id)
                advance_stage(task_id, "pass", from_stage="planning")
                logger.info(
                    "[planning] Cache HIT task '%s' — reusing plan %d, skipping 40-min pipeline.",
                    task_id, _cached.id,
                )
                return
        except Exception:
            logger.exception("[planning] Cache gate check failed for '%s' — running full pipeline.", task_id)
    elif _cache_mode in ('force_with_context', 'force_fresh'):
        try:
            update_task(task_id, cache_mode='normal')
        except Exception:
            pass

    # Collect prior failure context to inject into the planning prompts (skip on force_fresh).
    _prior_failures = []
    if _cache_mode != 'force_fresh':
        try:
            from app.database import get_prior_failure_context
            _prior_failures = get_prior_failure_context(task_id)
        except Exception:
            pass

    # RC4: Supersede any stale planning_results rows from prior runs so the new
    # session starts clean.  The API path already does this in main.py; this call
    # covers the scheduler-dispatched path which previously skipped it.
    try:
        from app.database import supersede_planning_results
        supersede_planning_results(task_id)
    except Exception:
        logger.exception("[planning] Failed to supersede prior planning results for task '%s'.", task_id)

    all_tasks = [task_to_dict(t) for t in get_all_tasks()]

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="planning",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
                prior_failure_context=_prior_failures,
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

        if result.get("outcome") == "rejected":
            # Circuit breaker: too many planning rejections
            from app.database import get_transition_results as _gtr_plan
            from app.agent.config import PLANNING_MAX_REJECTIONS as _MAX_PLANNING_REJECTIONS
            rejections = _gtr_plan(task_id, transition="planning_to_indev") or []
            fail_count = sum(1 for r in rejections if r.outcome == "rejected")
            
            if fail_count >= _MAX_PLANNING_REJECTIONS:
                logger.warning(
                    "[planning] Task '%s' rejected by review panel %d time(s) — "
                    "parking for manual review (user must re-trigger).",
                    task_id, fail_count,
                )
                _stop_reason = (
                    f"Design review exhausted ({fail_count}/{_MAX_PLANNING_REJECTIONS} attempts) — "
                    "revise the task description and click Run Planning to retry."
                )
                _planning_stopped[task_id] = _stop_reason
                _exit_summary = _stop_reason
                
                # Notify Inbox
                from app.database import create_inbox_message, get_task
                task_obj = get_task(task_id)
                create_inbox_message(
                    subject=f"Planning stopped: {(task_obj.title if task_obj else task_id)[:60]}",
                    source_type="card_stopped",
                    task_id=task_id,
                    project_id=task_obj.project if task_obj else None,
                    task_title=task_obj.title if task_obj else None,
                    outcome="stopped",
                    data_json=__import__("json").dumps({"reason": _stop_reason}),
                )
            else:
                # Park the card — requires manual re-trigger via "Run Planning" button.
                # Do not use _rejection_cooldowns (which would auto-retry after 5 min).
                _stop_reason = f"Design review failed ({fail_count}/{_MAX_PLANNING_REJECTIONS} attempts)"
                _planning_stopped[task_id] = _stop_reason
                _exit_summary = _stop_reason + " — click Run Planning to retry."

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
                    task_description=task.description or "",
                    domain=_domain,
                )
            )
            if gate_result.get("passed"):
                # Mark the planning result as gate-passed so future runs can reuse it.
                try:
                    from app.database import get_planning_result as _gpr_cache, mark_gate_passed
                    _pr_cache = _gpr_cache(task_id)
                    if _pr_cache and _content_hash:
                        mark_gate_passed(_pr_cache.id, _content_hash)
                except Exception:
                    pass
                _exit_summary = "Planning passed and gate checks confirmed. Advanced to INDEV."
                advance_stage(task_id, "pass", from_stage="planning")
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
                        "parking for manual review (user must re-trigger).",
                        task_id, gate_fail_count,
                    )
                    _stop_reason = (
                        f"Planning gate exhausted ({gate_fail_count}/{_MAX_PLANNING_GATE_FAILURES} attempts) — "
                        "review gate failures and click Run Planning to retry."
                    )
                    _planning_stopped[task_id] = _stop_reason
                    _exit_summary = _stop_reason

                    # Notify Inbox
                    from app.database import create_inbox_message, get_task
                    task_obj = get_task(task_id)
                    create_inbox_message(
                        subject=f"Planning stopped: {(task_obj.title if task_obj else task_id)[:60]}",
                        source_type="card_stopped",
                        task_id=task_id,
                        project_id=task_obj.project if task_obj else None,
                        task_title=task_obj.title if task_obj else None,
                        outcome="stopped",
                        data_json=__import__("json").dumps({"reason": _stop_reason}),
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
                            all_tasks=all_tasks,
                            llm_base_url=llm_base_url,
                            llm_model=llm_model,
                            max_context=max_context,
                            llm_id=llm_id,
                            budget_id=budget_id,
                            project_path=project_path,
                            task_title=task.title or "",
                            task_description=task.description or "",
                            domain=_domain,
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
                                        task_description=task.description or "",
                                        domain=_domain,
                                    )
                                )
                                if gate_result2.get("passed"):
                                    # Mark corrected plan as gate-passed for future cache reuse.
                                    try:
                                        from app.database import get_planning_result as _gpr_c2, mark_gate_passed
                                        _pr_c2 = _gpr_c2(task_id)
                                        if _pr_c2 and _content_hash:
                                            mark_gate_passed(_pr_c2.id, _content_hash)
                                    except Exception:
                                        pass
                                    _exit_summary = (
                                        "Correction agent patched plan; gate now passes. "
                                        "Advanced to INDEV."
                                    )
                                    advance_stage(task_id, "pass", from_stage="planning")
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
                advance_stage(task_id, "subdivide", from_stage="planning")
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
    from app.agent.pipeline_router import advance_stage

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
        if _session_id is not None:
            register_db_session(task_id, _session_id)

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
            if current_type in ("planning", "indev"):
                advance_stage(task_id, "pass", from_stage=current_type)
                logger.info("Task '%s' advanced from %s via scheduler (ACCEPTED).", task_id, current_type.upper())
            else:
                logger.info("Task '%s' reached ACCEPTED but current type '%s' has no auto-transition.", task_id, current_type)

        elif result.status == "NEEDS_HUMAN":
            _exit_reason = "needs_human"
            _exit_summary = result.final_message or "Agent escalated for human review."
            advance_stage(task_id, "pass", from_stage="indev")
            from app.database import create_inbox_message as _create_inbox
            task_obj = get_task(task_id)
            _create_inbox(
                subject=f"Human review needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="needs_human",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="needs_human",
                data_json=__import__("json").dumps({"summary": _exit_summary}),
            )
            logger.info("Task '%s' escalated to HUMAN REVIEW by agent: %s", task_id, _exit_summary)

        elif result.status == "CONSULTING":
            _exit_reason = "consulting"
            _exit_summary = result.final_message
            
            # Store the question in consultation_payload
            from app.database import update_task as _update_task, create_inbox_message as _create_inbox
            _update_task(task_id, consultation_payload=__import__("json").dumps({
                "question": result.consultation_question,
                "hint": None,
                "source": None
            }))
            
            # Notify the human (and Maestro) via Inbox
            task_obj = get_task(task_id)
            _create_inbox(
                subject=f"Consultation needed: {(task_obj.title if task_obj else task_id)[:60]}",
                source_type="consultation",
                task_id=task_id,
                project_id=task_obj.project if task_obj else None,
                task_title=task_obj.title if task_obj else None,
                outcome="consultation",
                data_json=__import__("json").dumps({
                    "question": result.consultation_question,
                    "summary": _exit_summary
                }),
            )
            logger.info("Task '%s' paused for CONSULTATION: %s", task_id, result.consultation_question)

        elif result.status in ("REVERT_TO_DESIGN", "REJECTED"):
            _exit_reason = "rejected"
            advance_stage(task_id, "fail", from_stage="indev")
            _record_demotion_inline(task_id, "indev", "planning", result.final_message or "Agent requested revert")
            logger.warning("Task '%s' reverted to PLANNING via scheduler: %s", task_id, result.final_message)

        elif result.status == "MAX_TURNS":
            _exit_reason = "max_turns"
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type in ("planning", "indev"):
                logger.warning("Task '%s' demoted to PLANNING (max_turns).", task_id)
                _record_demotion_inline(task_id, current_type, "planning", f"Max turns ({_MAX_TURNS}) exceeded without completion.")
                advance_stage(task_id, "fail", from_stage=current_type)
            else:
                logger.warning("Task '%s' reached terminal state (MAX_TURNS) but current type '%s' has no auto-transition.", task_id, current_type)

        elif result.status == "ERROR":
            _exit_reason = "error"
            task = get_task(task_id)
            if not task:
                return

            current_type = (task.type or "").lower()
            if current_type in ("planning", "indev"):
                logger.warning("Task '%s' demoted to PLANNING (error).", task_id)
                _record_demotion_inline(task_id, current_type, "planning", f"Execution error in {current_type} stage.")
                advance_stage(task_id, "fail", from_stage=current_type)
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
            if current_type in ("planning", "indev"):
                advance_stage(task_id, "fail", from_stage=current_type)
                logger.warning("Task '%s' demoted via advance_stage (exception in %s).", task_id, current_type)
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
    from app.agent.pipeline_router import advance_stage
    import json

    set_task_git_cwd(project_path, task_id=task_id)

    planning_result_obj = get_planning_result(task_id)
    if not planning_result_obj:
        logger.warning("No planning result for task '%s', demoting to planning.", task_id)
        advance_stage(task_id, "fail", from_stage="indev")
        _record_demotion_inline(task_id, "indev", "planning", "Missing planning results")
        _failed_cooldowns[task_id] = time.time()
        return

    try:
        planning_result = {
            "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
            "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
            "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
            "interface_contracts": json.loads(planning_result_obj.interface_contracts or "[]"),
            "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
        }
    except json.JSONDecodeError as exc:
        logger.warning("Corrupt planning result JSON for task '%s' (%s), demoting to planning.", task_id, exc)
        advance_stage(task_id, "fail", from_stage="indev")
        _record_demotion_inline(task_id, "indev", "planning", f"Corrupt planning result JSON: {exc}")
        return

    # Fetch the most recent review rejection so the dev agent knows what to fix.
    review_feedback: str | None = None
    try:
        from app.database import get_transition_results as _gtr_dev
        _review_transitions = {"conceptual_to_optimization", "optimization_to_security",
                                "security_to_final_review", "final_review_to_human_review"}
        for _tr in _gtr_dev(task_id):  # ordered desc by created_at
            if _tr.outcome in ("rejected", "failed") and _tr.transition in _review_transitions:
                _vs = _tr.vote_summary or {}
                _lines = [f"[PRIOR REVIEW REJECTION — {_tr.transition}]"]
                if isinstance(_vs, dict):
                    if _vs.get("summary"):
                        _lines.append(f"Summary: {_vs['summary']}")
                    for _f in _vs.get("high_severity_findings", []):
                        _lines.append(
                            f"  HIGH [{_f.get('stage', '')}]: {_f.get('justification', '')[:500]}"
                        )
                    for _f in _vs.get("medium_severity_findings", []):
                        _lines.append(
                            f"  MEDIUM [{_f.get('stage', '')}]: {_f.get('justification', '')[:400]}"
                        )
                elif isinstance(_vs, str):
                    _lines.append(_vs[:800])
                review_feedback = "\n".join(_lines)
                logger.info("[dev_orch] Loaded review feedback for task '%s': %s", task_id, _lines[0])
                break
    except Exception as _rf_exc:
        logger.warning("[dev_orch] Could not load review feedback for '%s': %s", task_id, _rf_exc)

    _session_id = create_agent_session(
        task_id=task_id,
        agent_type="dev_orchestrator",
        llm_id=llm_id,
        budget_id=budget_id,
        scheduler_reason="scheduler",
    )
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
                review_feedback=review_feedback,
                project_path=project_path,
            )
        )
        _prompt_tokens = result.get("prompt_tokens", 0)
        _completion_tokens = result.get("completion_tokens", 0)
        if result.get("status") == "ACCEPTED":
            _exit_reason = "completed"
            _exit_summary = f"Dev orchestrator completed. {result.get('batches_completed', 0)}/{result.get('total_batches', 0)} batches done."
            advance_stage(task_id, "pass", from_stage="indev")
            logger.info("Task '%s' advanced to CONCEPTUAL REVIEW via scheduler.", task_id)
        elif result.get("status") == "REVERT_TO_DESIGN":
            # Agent explicitly signalled the design is wrong — demote to planning.
            _exit_reason = "rejected"
            _error_detail = result.get("error_detail") or "Agent requested design revision."
            _exit_summary = _error_detail[:300]
            advance_stage(task_id, "reject", from_stage="indev")
            _record_demotion_inline(task_id, "indev", "planning", _exit_summary)
            logger.warning("Task '%s' reverted to PLANNING (agent REVERT_TO_DESIGN): %s", task_id, _exit_summary)
        else:
            # Transient failure (loop, LLM error, context saturation) — stay in INDEV.
            _exit_reason = "rejected"
            _error_detail = result.get("error_detail") or "Dev orchestrator transient failure."
            _exit_summary = _error_detail[:300]
            logger.warning("Task '%s' dev orchestrator transient failure — staying in INDEV: %s", task_id, _exit_summary)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during dev orchestrator."
        logger.info(f"[{AGENT_NAME}] Dev orchestrator for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Dev orchestrator raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Dev orchestrator for task '%s' failed.", task_id)
        # Stay in INDEV — an unexpected exception is not a design problem.
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
    from app.agent.pipeline_router import advance_stage
    from datetime import datetime
    import json as _json

    set_task_git_cwd(project_path, task_id=task_id)

    task = get_task(task_id)
    if not task:
        return

    planning_result_obj = get_planning_result(task_id)
    if not planning_result_obj:
        logger.warning("No planning result for task '%s' in conceptual review. Demoting to indev.", task_id)
        advance_stage(task_id, "fail", from_stage="conceptual_review")
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
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
        if result.get("outcome") == "needs_human":
            _exit_reason = "needs_human"
            _exit_summary = result.get("summary", "Reviewer escalated for human judgment.")
            advance_stage(task_id, "pass", from_stage="conceptual_review")
            from app.database import create_inbox_message as _create_inbox_cr
            _create_inbox_cr(
                subject=f"Human review needed: {(task.title or task_id)[:60]}",
                source_type="needs_human",
                task_id=task_id,
                project_id=task.project,
                task_title=task.title,
                outcome="needs_human",
                data_json=__import__("json").dumps({"summary": _exit_summary}),
            )
            logger.info("Task '%s' escalated to HUMAN REVIEW by conceptual reviewer.", task_id)
        elif result.get("outcome") == "passed":
            advance_stage(task_id, "pass", from_stage="conceptual_review")
            logger.info("Task '%s' advanced to OPTIMIZATION via scheduler.", task_id)
        else:
            advance_stage(task_id, "fail", from_stage="conceptual_review")
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
        advance_stage(task_id, "fail", from_stage="conceptual_review")
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


def _run_optimization_task(task_id: str, llm_base_url: str, llm_model: str,
                            max_context: int | None = None,
                            llm_id: int | None = None,
                            budget_id: int | None = None,
                            project_path: str | None = None) -> None:
    """Run optimization pipeline for an OPTIMIZATION task; advance to security on pass."""
    from app.agent.optimization import run_optimization_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task
    from app.database import create_agent_session, close_agent_session
    from app.agent.pipeline_router import advance_stage

    set_task_git_cwd(project_path, task_id=task_id)

    task = get_task(task_id)
    if not task:
        return

    _session_id = create_agent_session(
        task_id=task_id, agent_type="optimization",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    if _session_id is not None:
        register_db_session(task_id, _session_id)
    _exit_reason = "error"
    _exit_summary = ""
    _prompt = _compl = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        # Pre-flight PIP gate — runs at optimization stage entry
        if not _run_pip_preflight_and_gate(task_id, "optimization", llm_id, budget_id, project_path, loop):
            _exit_reason = "pip_blocked"
            _exit_summary = "PIP pre-flight gate blocked optimization entry."
            return  # card stays in optimization; resolution jobs dispatched

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
        _exit_reason = opt_result.get("outcome", "error")
        _exit_summary = opt_result.get("improvement_summary", "")
        _prompt = opt_result.get("total_prompt_tokens", 0)
        _compl = opt_result.get("total_completion_tokens", 0)
        logger.info("[optimization] Task '%s' via scheduler: %s", task_id, opt_result.get("outcome"))

        advance_stage(task_id, "pass", from_stage="optimization")
        logger.info("[optimization] Task '%s' advanced to SECURITY via scheduler.", task_id)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during optimization."
        logger.info(f"[{AGENT_NAME}] Optimization for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Exception during optimization pipeline."
        logger.exception(f"[{AGENT_NAME}] Optimization for task '%s' failed.", task_id)
        advance_stage(task_id, "fail", from_stage="optimization")
        _record_demotion_inline(task_id, "optimization", "indev", "Exception in optimization")
    finally:
        close_agent_session(_session_id, _exit_reason, _exit_summary,
                            prompt_tokens=_prompt, completion_tokens=_compl)
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
    from app.agent.pipeline_router import advance_stage

    set_task_git_cwd(project_path, task_id=task_id)

    task = get_task(task_id)
    if not task:
        return

    _session_id = create_agent_session(
        task_id=task_id, agent_type="security",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    if _session_id is not None:
        register_db_session(task_id, _session_id)
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
            advance_stage(task_id, "pass", from_stage="security")
            logger.info("[security] Task '%s' advanced to FINAL REVIEW via scheduler.", task_id)
        else:
            # Demotion target is determined by the reviewer (variable: "indev" or "optimization").
            # Use update_task directly since advance_stage has a single fail edge that may
            # not match the reviewer's chosen target.
            demotion = sec_result.get("demotion_target", "indev")
            update_task(task_id, type=demotion, stage_key=demotion)
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
        advance_stage(task_id, "fail", from_stage="security")
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



def _run_final_review_task(task_id: str, llm_base_url: str, llm_model: str,
                            max_context: int | None = None,
                            llm_id: int | None = None,
                            budget_id: int | None = None,
                            project_path: str | None = None) -> None:
    """Run the final review pipeline for a FINAL_REVIEW task; advance to human_review (manual) on pass."""
    from app.agent.final_review import run_final_review_pipeline
    from app.agent.tools import set_task_git_cwd
    from app.database import get_task, update_task, append_task_history
    from app.database import (
        create_transition_vote, create_transition_result,
        create_agent_session, close_agent_session,
    )
    from app.agent.pipeline_router import advance_stage

    set_task_git_cwd(project_path, task_id=task_id)

    task = get_task(task_id)
    if not task:
        return

    _session_id = create_agent_session(
        task_id=task_id, agent_type="final_review",
        llm_id=llm_id, budget_id=budget_id, scheduler_reason="scheduler",
    )
    if _session_id is not None:
        register_db_session(task_id, _session_id)
    _exit_reason = "error"
    _exit_summary = ""
    _prompt_tokens = _completion_tokens = 0

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        if not _run_pip_preflight_and_gate(task_id, "final_review", llm_id, budget_id, project_path, loop):
            _exit_reason = "pip_blocked"
            _exit_summary = "PIP pre-flight gate blocked final_review entry."
            return

        result = loop.run_until_complete(
            run_final_review_pipeline(
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
            transition="final_review",
            outcome=result.get("outcome", "unknown"),
            vote_summary=result,
            total_prompt_tokens=_prompt_tokens,
            total_completion_tokens=_completion_tokens,
        )

        if result.get("outcome") == "needs_human":
            _exit_reason = "needs_human"
            _exit_summary = result.get("summary", "Reviewer escalated for human judgment.")
            advance_stage(task_id, "pass", from_stage="final_review")
            from app.database import create_inbox_message as _create_inbox_fr
            _create_inbox_fr(
                subject=f"Human review needed: {(task.title or task_id)[:60]}",
                source_type="needs_human",
                task_id=task_id,
                project_id=task.project,
                task_title=task.title,
                outcome="needs_human",
                data_json=__import__("json").dumps({"summary": _exit_summary}),
            )
            logger.info("[final_review] Task '%s' escalated to HUMAN REVIEW by reviewer.", task_id)

        elif result.get("outcome") == "passed":
            from app.agent.merge import execute_merge
            from app.database import get_project_path as _get_project_path
            # Always use real project root for merge — project_path here is the
            # worktree path; git checkout base_branch inside a worktree fails.
            real_pp = (_get_project_path(task.project) if task.project else None) or project_path

            merge_test = execute_merge(
                task_id, project_path=real_pp, dry_run=True,
                llm_id=llm_id, budget_id=budget_id,
            )
            if merge_test.status == "virtual_passed":
                _exit_summary = "Final AI review passed. Virtual merge SUCCEEDED. Ready for human review."
                append_task_history(task_id, "ready_for_review", message=_exit_summary)
                advance_stage(task_id, "pass", from_stage="final_review")
                logger.info("[final_review] Task '%s' passed. Advanced to HUMAN REVIEW.", task_id)
            elif merge_test.status in ("conflict", "test_failure"):
                _exit_summary = f"Final AI review passed, but virtual merge {merge_test.status.upper()}. Demoting to indev."
                append_task_history(
                    task_id, "merge_test_failed",
                    message=f"{_exit_summary}\n\n{merge_test.error_detail or ''}",
                )
                advance_stage(task_id, "fail", from_stage="final_review")
                _record_demotion_inline(task_id, "final_review", "indev", _exit_summary)
                logger.warning("[final_review] Task '%s' virtual merge %s. Demoted to indev.", task_id, merge_test.status)
            else:
                # "error" = infrastructure failure; code review passed, advance anyway
                _exit_summary = f"Final AI review passed, but virtual merge FAILED: {merge_test.status}."
                append_task_history(
                    task_id, "merge_test_failed",
                    message=f"{_exit_summary} Detail: {merge_test.error_detail}",
                )
                advance_stage(task_id, "pass", from_stage="final_review")
                logger.warning("[final_review] Task '%s' virtual merge infrastructure error (%s). Advanced to HUMAN REVIEW with warning.", task_id, merge_test.status)
        else:
            # Demotion target is determined by the reviewer (variable).
            # Use update_task directly since advance_stage has a single fail edge.
            demotion = result.get("demotion_target", "indev")
            update_task(task_id, type=demotion, stage_key=demotion)
            _record_demotion_inline(task_id, "final_review", demotion, result.get("summary", ""))
            logger.warning("[final_review] Task '%s' demoted to %s via scheduler.", task_id, demotion)
    except ShutdownError:
        _exit_reason = "shutdown"
        _exit_summary = "Server shutdown during final review."
        logger.info(f"[{AGENT_NAME}] Final review for task '%s' aborted due to server shutdown.", task_id)
    except Exception:
        _exit_reason = "error"
        _exit_summary = "Final review raised an unexpected exception."
        logger.exception(f"[{AGENT_NAME}] Final review for task '%s' failed.", task_id)
        advance_stage(task_id, "fail", from_stage="final_review")
        _record_demotion_inline(task_id, "final_review", "indev", "Exception in final review")
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
    review_stages = {"conceptual_review", "optimization", "security", "human_review"}
    if from_stage in review_stages:
        logger.info("[pip] Triggering PIP generation for task '%s' demoted from '%s'.", task_id, from_stage)
        # We're in a daemon thread, but we can still use the loop if one exists
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(generate_pip(task_id, from_stage, reason))
        except RuntimeError:
            asyncio.run(generate_pip(task_id, from_stage, reason))




# ---------------------------------------------------------------------------
# Pipeline dispatch handler registrations (Phase 2: Scheduler Decoupling)
#
# Each _run_* function is registered into pipeline_router so that _run_task()
# can dispatch without a per-stage if/elif block.  New stage types added in
# future phases register here; no code changes needed in _run_task().
# ---------------------------------------------------------------------------

from app.agent.pipeline_router import register_handler as _register_stage_handler
import sys as _sys

# ---------------------------------------------------------------------------
# Phase 9 — Card Factory dispatch helpers
# ---------------------------------------------------------------------------

def _dispatch_factory_triggers(allowed_llm_id: "int | None") -> None:
    """Fire predecessor_complete and cron factory triggers on each tick."""
    try:
        from app.database import get_llm as _get_llm
        llm = _get_llm(allowed_llm_id) if allowed_llm_id else None
        llm_base_url = f"http://{llm.address}:{llm.port}/v1" if llm else "http://localhost:8008/v1"
        llm_model = llm.model if llm else "local"
        max_context = llm.max_context if llm else None
        llm_id = llm.id if llm else None

        from app.agent.card_factory import check_predecessor_triggers, check_cron_triggers
        check_predecessor_triggers(
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=None,
        )
        check_cron_triggers(
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=None,
        )
    except Exception:
        logger.exception("[scheduler] _dispatch_factory_triggers error")


def _run_factory_node(
    task_id: str,
    llm_base_url: str,
    llm_model: str,
    max_context: "int | None" = None,
    llm_id: "int | None" = None,
    budget_id: "int | None" = None,
    project_path: "str | None" = None,
) -> None:
    """Scheduler handler for factory_node stage type.

    A task whose stage_key is 'factory_node' (or any stage with agent_type
    'factory_node') is dispatched here.  The factory reads its configuration
    from the pipeline_stage.config JSON and creates sub-cards, then advances
    the triggering task to the next stage (single_pass gate).
    """
    from app.database import get_task, get_stage_by_key, get_default_template
    from app.agent.card_factory import run_factory
    from app.agent.pipeline_router import advance_stage

    task = get_task(task_id)
    if not task:
        return

    # Resolve pipeline stage
    stage = None
    stage_key = task.stage_key or task.type or ""
    template_id = None
    if hasattr(task, "project_ref") and task.project_ref:
        template_id = getattr(task.project_ref, "pipeline_template_id", None)
    if template_id is None:
        tmpl = get_default_template()
        if tmpl:
            template_id = tmpl.id
    if template_id:
        stage = get_stage_by_key(template_id, stage_key)

    if stage is None:
        logger.warning("[factory] No pipeline stage found for task %s stage_key=%r", task_id, stage_key)
        return

    project_id = task.project_id
    if project_id is None:
        logger.warning("[factory] Task %s has no project_id", task_id)
        return

    try:
        run_factory(
            stage.id,
            project_id,
            "pipeline",
            trigger_card_id=task_id,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            max_context=max_context,
            llm_id=llm_id,
            budget_id=budget_id,
        )
    except Exception:
        logger.exception("[factory] run_factory failed for task %s", task_id)

    # Advance the triggering card to the next stage (single_pass)
    advance_stage(task_id, "pass", from_stage=stage_key)

_SCHEDULER_MODULE = _sys.modules[__name__]


def _make_late_handler(fn_name: str) -> Callable:
    """Return a handler that looks up fn_name on this module at call time.

    This ensures that patch("app.agent.scheduler.<fn_name>") in tests correctly
    intercepts the call, because the lookup happens via getattr at dispatch time
    rather than at registration time.
    """
    def _handler(*args, **kw):
        return getattr(_SCHEDULER_MODULE, fn_name)(*args, **kw)
    return _handler


_register_stage_handler("idea",              _make_late_handler("_run_intake"))
_register_stage_handler("planning",          _make_late_handler("_run_planning_task"))
_register_stage_handler("indev",             _make_late_handler("_run_dev_orchestrator_task"))
_register_stage_handler("conceptual_review", _make_late_handler("_run_conceptual_review_task"))
_register_stage_handler("optimization",      _make_late_handler("_run_optimization_task"))
_register_stage_handler("security",          _make_late_handler("_run_security_task"))
_register_stage_handler("final_review",      _make_late_handler("_run_final_review_task"))
_register_stage_handler("factory_node",      _make_late_handler("_run_factory_node"))

# ---------------------------------------------------------------------------
# Agent-type executor registrations (generic pipeline nodes)
# ---------------------------------------------------------------------------

from app.agent.stage_executors import (  # noqa: E402
    _run_circuit_breaker,
    _run_voting_panel,
    _run_fan_out_judge,
    _run_reflection_agent,
)
from app.agent.pipeline_router import register_agent_type_executor as _reg_executor  # noqa: E402

_reg_executor("circuit_breaker",   _run_circuit_breaker)
_reg_executor("voting_panel",      _run_voting_panel)
_reg_executor("fan_out_judge",     _run_fan_out_judge)
_reg_executor("reflection_agent",  _run_reflection_agent)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _recover_hung_sessions() -> None:
    """Kill sessions that have been alive but LLM-idle for _HUNG_SESSION_IDLE_SECONDS.

    The scheduler thread cannot directly interrupt a blocking tool call, but
    calling kill_session() poisons the session UUID so the next call_llm()
    raises SessionKilledError.  Removing the task from _active_sessions
    immediately frees the LLM slot for re-dispatch on the same tick.
    The old thread closes the DB agent_sessions row when it finally exits.
    """
    import datetime as _dt_mod
    from app.database.session import SessionLocal as _SL
    from app.database.models import BudgetEntry as _BE, Task as _T
    from app.agent.llm_client import kill_session as _kill_session
    from app.agent.worktree import teardown_task_worktree as _teardown_worktree

    now_mono = time.time()
    now_utc = _dt_mod.datetime.now(_dt_mod.timezone.utc)

    with _active_sessions_lock:
        snapshot = [
            (tid, _session_started_at.get(tid, now_mono),
             _session_llm_ids.get(tid), _session_ids.get(tid),
             _session_types.get(tid))
            for tid, thread in _active_sessions.items()
            if thread.is_alive()
            and now_mono - _session_started_at.get(tid, now_mono) >= _HUNG_SESSION_MIN_AGE_SECONDS
        ]

    if not snapshot:
        return

    try:
        db = _SL()
        try:
            for tid, start, llm_id, sess_uuid, stype in snapshot:
                latest = (
                    db.query(_BE)
                    .filter(_BE.task_id == tid)
                    .order_by(_BE.created_at.desc())
                    .first()
                )
                # Convert session start (monotonic) to a wall-clock UTC datetime so
                # we can clamp idle baseline to max(last_budget_entry, session_start).
                session_started_utc = now_utc - _dt_mod.timedelta(seconds=(now_mono - start))
                
                if latest is None:
                    idle_secs = now_mono - start
                else:
                    last_at = latest.created_at
                    if isinstance(last_at, str):
                        try:
                            # Handle both " " and "T" separators, and ensure UTC
                            ts_str = last_at.replace(" ", "T").replace("Z", "+00:00")
                            last_at = _dt_mod.datetime.fromisoformat(ts_str)
                            if last_at.tzinfo is None:
                                last_at = last_at.replace(tzinfo=_dt_mod.timezone.utc)
                        except ValueError:
                            last_at = session_started_utc
                    
                    if last_at.tzinfo is None:
                        last_at = last_at.replace(tzinfo=_dt_mod.timezone.utc)
                    
                    # Baseline is the most recent of (session start, last LLM call)
                    baseline = max(last_at, session_started_utc)
                    idle_secs = (now_utc - baseline).total_seconds()

                if idle_secs < _HUNG_SESSION_IDLE_SECONDS:
                    continue

                logger.warning(
                    "[scheduler] Hung session for task '%s' (idle %.0f min, type=%s) — "
                    "killing and freeing slot for re-dispatch.",
                    tid, idle_secs / 60, stype,
                )

                if sess_uuid:
                    try:
                        _kill_session(sess_uuid)
                    except Exception:
                        pass

                # Force worktree removal for hung tasks to clear potential file/git locks
                # that cause re-dispatch loops in Windows environments.
                try:
                    task = db.query(_T).get(tid)
                    if task and task.project_ref and task.project_ref.path:
                        _teardown_worktree(tid, task.project_ref.path)
                except Exception as exc:
                    logger.error("[scheduler] Failed to teardown worktree for hung task '%s': %s", tid, exc)

                with _active_sessions_lock:
                    _active_sessions.pop(tid, None)
                    _session_llm_ids.pop(tid, None)
                    _session_types.pop(tid, None)
                    _session_started_at.pop(tid, None)
                    _session_titles.pop(tid, None)
                    _session_ids.pop(tid, None)

                if llm_id is not None:
                    with _llm_counts_lock:
                        _llm_session_counts[llm_id] = max(
                            0, _llm_session_counts[llm_id] - 1
                        )
        finally:
            db.close()
    except Exception:
        logger.exception("[scheduler] _recover_hung_sessions error")


def _cleanup_finished() -> None:
    """Remove sessions whose threads have completed and re-sync capacity counts."""
    dead_db_session_ids: list[int] = []
    with _active_sessions_lock:
        finished = [tid for tid, t in _active_sessions.items() if not t.is_alive()]
        for tid in finished:
            db_sid = _active_db_session_ids.pop(tid, None)
            if db_sid is not None:
                dead_db_session_ids.append(db_sid)
            del _active_sessions[tid]
            _session_llm_ids.pop(tid, None)
            _session_titles.pop(tid, None)
            _session_types.pop(tid, None)
            _session_started_at.pop(tid, None)

    # Immediately close DB sessions for threads that exited without their finally block.
    # close_agent_session is idempotent — already-closed sessions are a no-op.
    if dead_db_session_ids:
        try:
            from app.database import close_agent_session
            for db_sid in dead_db_session_ids:
                close_agent_session(db_sid, "thread_exited",
                                    "Thread exited without closing session")
        except Exception:
            logger.exception("[scheduler] Failed to close dead-thread DB sessions.")

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

    # Reconcile DB state — close any open agent_sessions whose session PK is not
    # in the currently-alive set.  Using session ID (not task ID) means stale
    # predecessor sessions (e.g. 50 prior planning retries for the same task)
    # are cleaned up even while the task thread is still running.
    with _active_sessions_lock:
        _alive_db_session_ids = set(_active_db_session_ids.values())
    try:
        from app.database import close_zombie_sessions_by_session_id, create_inbox_message, get_task
        closed_ids = close_zombie_sessions_by_session_id(exclude_ids=_alive_db_session_ids)
        if closed_ids:
            logger.info("[scheduler] Closed zombie DB session(s) for tasks: %s", closed_ids)
            for tid in closed_ids:
                task = get_task(tid)
                create_inbox_message(
                    subject=f"Zombie session closed: {(task.title if task else tid)[:60]}",
                    source_type="zombie_session",
                    task_id=tid,
                    project_id=task.project if task else None,
                    task_title=task.title if task else None,
                    outcome="zombie_recovered",
                    data_json=__import__("json").dumps({"task_id": tid, "reason": "thread no longer alive"}),
                )
    except Exception:
        logger.exception("[scheduler] Failed to reconcile zombie DB sessions.")


def cancel_task_sessions(task_ids: list[str]) -> None:
    """Kill active scheduler sessions for a list of soft-deleted task IDs.

    Kills the LLM session so the in-flight call_llm() raises SessionKilledError
    (a ShutdownError subclass), removes the task from _active_sessions to free
    the slot immediately, and calls request_stop() for any running MaestroLoop
    (indev) tasks.
    """
    from app.agent.llm_client import kill_session as _kill_session
    from app.agent.loop import request_stop

    cancelled: list[tuple[str, int | None]] = []  # (task_id, llm_id)

    with _active_sessions_lock:
        for task_id in task_ids:
            thread = _active_sessions.get(task_id)
            if thread is None or not thread.is_alive():
                continue

            session_id = _session_ids.get(task_id)
            llm_id = _session_llm_ids.get(task_id)
            task_type = _session_types.get(task_id)

            if session_id:
                _kill_session(session_id)

            del _active_sessions[task_id]
            _session_llm_ids.pop(task_id, None)
            _session_types.pop(task_id, None)
            _session_started_at.pop(task_id, None)
            _session_titles.pop(task_id, None)
            _session_ids.pop(task_id, None)

            cancelled.append((task_id, llm_id, task_type))
            logger.info(
                "[scheduler] Cancelled session for deleted task '%s' (type=%s).",
                task_id, task_type,
            )

    if not cancelled:
        return

    # Free LLM capacity slots immediately.
    with _llm_counts_lock:
        for _, llm_id, _ in cancelled:
            if llm_id is not None:
                _llm_session_counts[llm_id] = max(0, _llm_session_counts[llm_id] - 1)

    # For indev tasks, also cancel the asyncio task so the loop doesn't wait
    # until its next LLM call to notice it was killed.
    for task_id, _, task_type in cancelled:
        if task_type == "indev":
            request_stop(task_id)


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
        get_pending_episodic_summary_jobs,
        update_episodic_summary_job,
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
                    project_id=job.project,
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

    # --- Episodic summary jobs — reset orphaned 'running' rows ---
    try:
        from app.database.session import SessionLocal
        from app.database.models import EpisodicSummaryJob as _ESJ
        with SessionLocal() as db:
            orphaned = db.query(_ESJ).filter(_ESJ.status == "running").all()  # ORM query — no text() needed
        for job in orphaned:
            session_key = f"episodic-summary-{job.id}"
            with _active_sessions_lock:
                is_alive = (
                    session_key in _active_sessions
                    and _active_sessions[session_key].is_alive()
                )
            if not is_alive:
                logger.warning(
                    "[rescue] episodic_summary job %d stuck in 'running' — resetting to pending.",
                    job.id,
                )
                update_episodic_summary_job(job.id, status="pending", completed_at=None)
    except Exception as exc:
        logger.debug("[rescue] episodic_summary rescue check: %s", exc)


# ---------------------------------------------------------------------------
# Episodic summary job dispatcher + runner (Gap 7)
# ---------------------------------------------------------------------------

_EPISODIC_SUMMARY_RETRY_COOLDOWN: float = 300.0   # 5 min cooldown for failed jobs
_EPISODIC_SUMMARY_MAX_TURNS: int = 3              # not really turns; just for LLM calls
_SUMMARY_PROMPT = (
    "Summarise what this agent session attempted, what worked, and what failed. "
    "Be specific about approaches and outcomes. Write 2-4 sentences only. "
    "Focus on information that would help a future agent avoid the same mistakes "
    "or recognise promising directions."
)


def _dispatch_episodic_summary_jobs(
    allowed_llm_id: "int | None",
    node_active_counts: "dict[int, int]",
    node_session_counts: "dict[int, int]",
    node_obj_cache: "dict[int, Any]",
    llm_node_cache: "dict[int, int | None]",
) -> "int | None":
    """Dispatch pending episodic summary jobs (tier 2 — maintenance).
    Returns the (possibly updated) allowed_llm_id.
    """
    from app.agent.config import EPISODIC_MEMORY_ENABLED
    if not EPISODIC_MEMORY_ENABLED:
        return allowed_llm_id

    from app.database import get_pending_episodic_summary_jobs, update_episodic_summary_job, get_llm

    pending = get_pending_episodic_summary_jobs(limit=5)
    for job in pending:
        if not job.llm_id:
            continue

        job_key = f"episodic-summary-{job.id}"
        with _active_sessions_lock:
            if job_key in _active_sessions and _active_sessions[job_key].is_alive():
                continue

        llm = get_llm(job.llm_id)
        if not llm:
            continue

        if allowed_llm_id is not None and llm.id != allowed_llm_id:
            continue
        if allowed_llm_id is None:
            allowed_llm_id = llm.id

        if not _check_and_reserve_slot(
            llm, node_active_counts, node_session_counts, node_obj_cache, llm_node_cache,
            label=job_key,
        ):
            continue

        update_episodic_summary_job(job.id, status="running")

        thread = threading.Thread(
            target=_run_episodic_summary_job,
            args=(job, llm),
            daemon=True,
            name=f"maestro-episodic-summary-{job.id}",
        )
        with _active_sessions_lock:
            _active_sessions[job_key] = thread
            _session_llm_ids[job_key] = llm.id
            _session_titles[job_key] = f"Episodic Summary: task {job.task_id}"
        thread.start()

    return allowed_llm_id


def _run_episodic_summary_job(job: Any, llm: Any) -> None:
    """Worker: generate a 2-4 sentence session summary and store it as an episode."""
    import asyncio as _asyncio
    import json as _json
    from app.database import update_episodic_summary_job, get_task as _get_task, get_budget_entries
    from app.agent.llm_client import call_llm
    from app.agent.episodic_memory import insert_episode
    import app.agent.config as _cfg

    job_key = f"episodic-summary-{job.id}"
    try:
        task = _get_task(job.task_id)
        if not task:
            update_episodic_summary_job(job.id, status="failed")
            return

        # Fetch recent budget entries (up to 30 LLM turns)
        entries = get_budget_entries(task_id=str(job.task_id), limit=30)

        if not entries:
            update_episodic_summary_job(job.id, status="completed")
            return

        # Build a condensed history text for the summarisation prompt
        lines = []
        for i, e in enumerate(entries):
            preview = ""
            try:
                resp = _json.loads(e.response_data or "{}")
                choices = resp.get("choices") or []
                if choices:
                    preview = (
                        (choices[0].get("message") or {}).get("content") or ""
                    )[:300]
            except Exception:
                pass
            agent = e.agent_name or "?"
            lines.append(f"[turn {i+1}] agent={agent}: {preview}")

        history_text = "\n".join(lines)

        messages = [
            {
                "role": "system",
                "content": "You are a concise technical analyst. Summarise agent sessions in 2-4 sentences.",
            },
            {
                "role": "user",
                "content": (
                    f"Task: {task.title}\n"
                    f"Final status: {job.final_status}\n\n"
                    f"Recent session history (last {len(entries)} turns):\n{history_text}\n\n"
                    f"{_SUMMARY_PROMPT}"
                ),
            },
        ]

        loop = _asyncio.new_event_loop()
        try:
            response = loop.run_until_complete(
                call_llm(
                    messages,
                    base_url=llm.base_url,
                    model=llm.model,
                    tools=None,
                    task_id=job.task_id,
                    llm_id=job.llm_id,
                    budget_id=job.budget_id,
                    agent_name="episodic_summary",
                )
            )
        finally:
            loop.close()

        summary_text = ""
        if response and response.get("choices"):
            summary_text = (
                response["choices"][0].get("message", {}).get("content", "") or ""
            ).strip()

        if summary_text and task.project_id is not None:
            insert_episode(
                project_id=task.project_id,
                task_id=job.task_id,
                episode_type="session_summary",
                content=f"Task '{task.title}' ({job.final_status}): {summary_text}",
                metadata={
                    "task_title": task.title,
                    "final_status": job.final_status,
                    "job_id": job.id,
                },
                settings=_cfg,
            )

        update_episodic_summary_job(job.id, status="completed")

    except Exception as exc:
        logger.error("[episodic_summary] job %d failed: %s", job.id, exc)
        update_episodic_summary_job(job.id, status="failed")
    finally:
        job_key = f"episodic-summary-{job.id}"
        with _active_sessions_lock:
            _active_sessions.pop(job_key, None)
            lid = _session_llm_ids.pop(job_key, None)
        if lid is not None:
            with _llm_counts_lock:
                _llm_session_counts[lid] = max(0, _llm_session_counts.get(lid, 0) - 1)


# ---------------------------------------------------------------------------
# Nightly episodic memory cleanup
# ---------------------------------------------------------------------------

_last_episodic_cleanup: float = 0.0
_EPISODIC_CLEANUP_INTERVAL: float = 86400.0  # 24 hours


def _run_episodic_cleanup() -> None:
    """Delete expired episodic_memory rows (expires_at < now). Runs once per day."""
    global _last_episodic_cleanup
    from app.agent.config import EPISODIC_MEMORY_ENABLED
    if not EPISODIC_MEMORY_ENABLED:
        return

    now = time.time()
    if now - _last_episodic_cleanup < _EPISODIC_CLEANUP_INTERVAL:
        return
    _last_episodic_cleanup = now

    try:
        from app.database.session import SessionLocal
        from sqlalchemy import text as _sa_text
        with SessionLocal() as db:
            result = db.execute(_sa_text("DELETE FROM episodic_memory WHERE expires_at < now()"))
            db.commit()
            deleted = result.rowcount if hasattr(result, "rowcount") else "?"
            if deleted and deleted != "?" and int(deleted) > 0:
                logger.info("[episodic_cleanup] Deleted %s expired episode(s).", deleted)
    except Exception as exc:
        logger.warning("[episodic_cleanup] cleanup failed: %s", exc)


def _task_to_mini_dict(task: Any) -> dict:
    """Minimal dict for DAGResolver - avoids importing task_to_dict."""
    return {
        "id": task.id,
        "type": task.type,
        "position": task.position,
        "prerequisites": task.prerequisites or [],
        "parent_task_id": getattr(task, "parent_task_id", None),
        "last_progress_at": getattr(task, "last_progress_at", None),
        "is_starred": bool(getattr(task, "is_starred", False)),
        "owner": getattr(task, "owner", "user") or "user",
        "subdivision_generation": getattr(task, "subdivision_generation", 0) or 0,
    }
