"""
app/agent/event_dispatcher.py
------------------------------
EventDispatcher — called when any event source determines a watch should fire.

Runs a MaestroAgent synchronously for the watch's project with the event
payload injected as context.  The caller (webhook route / api_poller / file
watcher) blocks until the Maestro tick completes.
"""
from __future__ import annotations

import asyncio
import logging

from app.database.crud_events import (
    get_watch,
    should_fire,
    record_firing,
    log_watch_error,
    payload_hash as compute_hash,
)

logger = logging.getLogger(__name__)


class EventDispatcher:
    def dispatch(self, watch_id: int, payload: str) -> dict:
        """
        Gate → record → run Maestro tick.  Returns a dict describing the outcome.
        Always synchronous — intended to be called from a thread, never from an
        async context (use asyncio.run internally).
        """
        watch = get_watch(watch_id)
        if not watch or watch.status != "active":
            return {"fired": False, "reason": "watch inactive or not found"}

        p_hash = compute_hash(payload)

        if not should_fire(watch, p_hash):
            return {"fired": False, "reason": "dedup suppressed"}

        record_firing(watch_id, p_hash)

        event_context = (
            f"[EVENT: {watch.event_type} | watch={watch.label}]\n{payload}"
        )

        try:
            result = run_event_maestro_tick(watch.project_id, event_context)
        except Exception as exc:
            logger.exception("[EventDispatcher] tick failed for watch %d: %s", watch_id, exc)
            log_watch_error(watch_id, str(exc))
            return {"fired": True, "result": None, "error": str(exc)}

        return {"fired": True, "result": result}


def run_event_maestro_tick(project_id: int, event_context: str) -> dict:
    """
    Instantiate and run a MaestroAgent synchronously for the given project,
    injecting event_context so the agent knows what triggered this tick.
    """
    from app.database.crud_projects import get_project_by_id
    from app.database.crud_infra import get_llm
    from app.agent.maestro import MaestroAgent

    project = get_project_by_id(project_id)
    if not project:
        raise ValueError(f"Project {project_id} not found")
    if not project.llm_id or not project.budget_id:
        raise ValueError(
            f"Project {project_id!r} has no LLM or budget configured — cannot fire event tick"
        )

    llm = get_llm(project.llm_id)
    if not llm:
        raise ValueError(f"LLM {project.llm_id} not found for project {project_id!r}")

    agent = MaestroAgent(
        project_name=project.name,
        project_path=project.path,
        llm_id=project.llm_id,
        budget_id=project.budget_id,
        llm_base_url=f"http://{llm.address}:{llm.port}/v1",
        llm_model=llm.model,
        event_context=event_context,
    )

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        result = loop.run_until_complete(agent.run())
    finally:
        loop.close()

    return result
