"""
app/agent/file_watcher.py
--------------------------
MaestroFileWatcher — monitors directories for file-system events and fires
EventDispatcher.dispatch() when a watched file is created or modified.

Uses the `watchdog` library.  One Observer instance is shared across all
active file_watch watches.  New watches can be added at runtime without
restarting the server.
"""
from __future__ import annotations

import logging
import threading

logger = logging.getLogger(__name__)

# Module-level singleton — set by lifespan startup in main.py
_file_watcher: "MaestroFileWatcher | None" = None
_watcher_lock = threading.Lock()


def get_file_watcher() -> "MaestroFileWatcher | None":
    return _file_watcher


def _set_file_watcher(watcher: "MaestroFileWatcher | None") -> None:
    global _file_watcher
    with _watcher_lock:
        _file_watcher = watcher


def _make_handler(watch_id: int):
    """Return a FileSystemEventHandler bound to watch_id."""
    from watchdog.events import FileSystemEventHandler

    class _Handler(FileSystemEventHandler):
        def on_created(self, event):
            if not event.is_directory:
                _dispatch(watch_id, f"File created: {event.src_path}")

        def on_modified(self, event):
            if not event.is_directory:
                _dispatch(watch_id, f"File modified: {event.src_path}")

    return _Handler()


def _dispatch(watch_id: int, payload: str) -> None:
    """Fire EventDispatcher in the calling watchdog thread (already a daemon thread)."""
    try:
        from app.agent.event_dispatcher import EventDispatcher
        result = EventDispatcher().dispatch(watch_id, payload)
        logger.debug("[FileWatcher] watch %d dispatch result: %s", watch_id, result)
    except Exception as exc:
        logger.error("[FileWatcher] watch %d dispatch error: %s", watch_id, exc)


class MaestroFileWatcher:
    """Wraps a watchdog Observer and manages Maestro watch handlers."""

    def __init__(self) -> None:
        from watchdog.observers import Observer
        self._observer = Observer()
        self._started = False

    def start(self) -> None:
        """
        Load all active file_watch records from the DB, schedule handlers,
        and start the underlying watchdog Observer.

        Called once from app lifespan after the scheduler starts.
        """
        from app.database.crud_events import list_watches

        watches = list_watches(event_type="file_watch", status="active")
        for watch in watches:
            self._schedule_watch(watch)

        self._observer.start()
        self._started = True
        logger.info("[FileWatcher] Started with %d file_watch(es).", len(watches))

    def add_watch(self, watch) -> None:
        """Add a newly created file_watch to the running Observer."""
        if not self._started:
            logger.warning("[FileWatcher] add_watch called before start() — ignoring.")
            return
        self._schedule_watch(watch)
        logger.info("[FileWatcher] Added watch %d: %s", watch.id, watch.source_config.get("path"))

    def stop(self) -> None:
        if self._started:
            self._observer.stop()
            self._observer.join()
            self._started = False
            logger.info("[FileWatcher] Stopped.")

    def _schedule_watch(self, watch) -> None:
        path = watch.source_config.get("path")
        if not path:
            logger.warning("[FileWatcher] watch %d has no 'path' in source_config — skipped.", watch.id)
            return

        import os
        if not os.path.exists(path):
            logger.warning("[FileWatcher] watch %d path %r does not exist — skipped.", watch.id, path)
            return

        recursive = watch.source_config.get("recursive", False)
        handler = _make_handler(watch.id)
        try:
            self._observer.schedule(handler, path, recursive=recursive)
        except Exception as exc:
            logger.error("[FileWatcher] Could not schedule watch %d on %r: %s", watch.id, path, exc)
