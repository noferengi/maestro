"""
app/agent/api_poller.py
------------------------
poll_due_watches() — called at the end of each scheduler tick.

Checks api_poll watches whose poll interval has elapsed, fetches the URL,
and fires EventDispatcher.dispatch() if the content changed (or always, if
use_content_hash is False).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import httpx

logger = logging.getLogger(__name__)


def poll_due_watches() -> None:
    """
    Scan all active api_poll watches and fire those whose interval has elapsed.
    Called synchronously from the scheduler tick — must not block for long.
    Any per-watch error is caught and logged without crashing the tick.
    """
    import httpx
    from app.database.crud_events import list_watches, log_watch_error
    from app.agent.event_dispatcher import EventDispatcher

    watches = list_watches(event_type="api_poll", status="active")
    for watch in watches:
        if not _is_due(watch):
            continue
        try:
            cfg = watch.source_config or {}
            resp = httpx.get(  # type: ignore[attr-defined]
                cfg["url"],
                timeout=cfg.get("timeout_seconds", 30),
                headers=cfg.get("headers", {}),
            )
            payload = resp.text[:16384]
            result = EventDispatcher().dispatch(watch.id, payload)
            logger.debug("[ApiPoller] watch %d fired: %s", watch.id, result)
        except KeyError:
            log_watch_error(watch.id, "source_config missing 'url'")
        except Exception as exc:
            logger.warning("[ApiPoller] watch %d error: %s", watch.id, exc)
            log_watch_error(watch.id, str(exc))


def _is_due(watch) -> bool:
    interval = (watch.source_config or {}).get("poll_interval_seconds", 3600)
    if watch.last_fired_at is None:
        return True
    last = watch.last_fired_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) >= last + timedelta(seconds=interval)
