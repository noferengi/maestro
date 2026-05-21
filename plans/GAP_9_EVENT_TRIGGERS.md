# Gap 9 — Event-driven triggers (world hooks, reactive dispatch)

**Status:** Complete ✓  
**Effort:** Medium  
**Priority:** Medium — required for Maestro to respond to the world rather than just its own state

---

## Problem

The scheduler operates on a fixed tick cycle. Maestro fires when stall ticks accumulate.
Everything is poll-based and internally driven. The system cannot react to external
events: a git push, a failing CI run, a new arXiv paper matching a watched query, a
file appearing in a watched directory, a webhook from GitHub.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Event sources (first implementation)** | HTTP webhook receiver (`POST /api/events/inbound`), file system watcher (`watchdog` library), scheduled API poll (fetch URL, fire if content changed). |
| **Registration** | `register_watch()` tool — Maestro creates watches during sessions. No human UI for watch management; humans inspect via MCP tools. Watches stored in `watched_events` DB table. |
| **Dispatch** | Event fires a Maestro autopilot tick with the payload injected as context. Maestro runs synchronously — the event dispatcher blocks until Maestro's tick completes. Maestro decides what to create: IDEA cards, research jobs, documents, nothing. Research job results feed back into Maestro's context inline (same call-stack model as Gap 8). |
| **Deduplication** | Three mechanisms, all per-watch, with source-appropriate defaults: (1) cooldown window — suppress re-fire for N seconds after a firing; (2) content hash — only fire if payload differs from last firing; (3) max fires + expiry — self-deactivate after N firings or a date. All three stored as `fire_config` JSON on the watch record. |

---

## Implementation plan

### Phase 1 — `watched_events` table

**Migration** (`NNNN_watched_events.py`):

```sql
CREATE TABLE watched_events (
    id             SERIAL PRIMARY KEY,
    project_id     INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    event_type     TEXT    NOT NULL
                       CHECK (event_type IN ('webhook', 'file_watch', 'api_poll')),
    label          TEXT    NOT NULL,     -- human-readable name for this watch
    source_config  JSONB   NOT NULL,     -- event-type-specific config (URL, path, etc.)
    fire_config    JSONB   NOT NULL DEFAULT '{}',
                   -- { cooldown_seconds: int, use_content_hash: bool,
                   --   max_fires: int|null, expires_at: timestamp|null }
    status         TEXT    NOT NULL DEFAULT 'active'
                       CHECK (status IN ('active', 'paused', 'expired')),
    last_fired_at  TIMESTAMPTZ NULL,
    last_payload_hash TEXT NULL,         -- for content hash dedup
    fire_count     INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_by_session TEXT NULL         -- session_id that called register_watch
);

CREATE INDEX ON watched_events (project_id, status, event_type);
```

**`app/database/crud_events.py`** — new CRUD module:
- `create_watch(project_id, event_type, label, source_config, fire_config, session_id, db)`
- `list_watches(project_id=None, status='active', db)` — used by the event poller and MCP tools
- `record_firing(watch_id, payload_hash, db)` — updates `last_fired_at`, `fire_count`; auto-expires if `max_fires` reached
- `should_fire(watch, payload_hash, db) -> bool` — evaluates all three dedup rules:
  ```python
  def should_fire(watch, payload_hash, db) -> bool:
      cfg = watch.fire_config
      now = datetime.utcnow()

      # Expiry
      if cfg.get("expires_at") and now > cfg["expires_at"]:
          update_watch_status(watch.id, "expired", db)
          return False

      # Max fires
      if cfg.get("max_fires") and watch.fire_count >= cfg["max_fires"]:
          update_watch_status(watch.id, "expired", db)
          return False

      # Cooldown window
      cooldown = cfg.get("cooldown_seconds", 60)
      if watch.last_fired_at and (now - watch.last_fired_at).seconds < cooldown:
          return False

      # Content hash (API poll default)
      if cfg.get("use_content_hash") and payload_hash == watch.last_payload_hash:
          return False

      return True
  ```

---

### Phase 2 — Event dispatcher

**`app/agent/event_dispatcher.py`** — new file:

```python
class EventDispatcher:
    """
    Called when any event source determines that a watch should fire.
    Runs the Maestro autopilot tick synchronously and blocks until it completes.
    """

    def dispatch(self, watch_id: int, payload: str, db, settings) -> dict:
        watch = get_watch(watch_id, db)
        payload_hash = hashlib.sha256(payload.encode()).hexdigest()

        if not should_fire(watch, payload_hash, db):
            return {"fired": False, "reason": "dedup suppressed"}

        record_firing(watch_id, payload_hash, db)

        # Build event context for Maestro's tick
        event_context = (
            f"[EVENT: {watch.event_type} | watch={watch.label}]\n"
            f"{payload}"
        )

        # Synchronous Maestro tick — blocks until complete.
        # Maestro may create cards, run research (inline via ask_agent pattern), write documents.
        result = autopilot_tick(
            project_id=watch.project_id,
            db=db,
            settings=settings,
            event_context=event_context,
        )

        return {"fired": True, "result": result}
```

**`autopilot_tick` extension** (from Gap 2) — add `event_context: str | None = None` parameter.
When present, the event context is prepended to the Maestro assessment prompt:
```
[INCOMING EVENT]
{event_context}

React to this event. Decide what to create, investigate, or record.
Use your full toolkit: create IDEA cards, run research, write to the document store.
```

---

### Phase 3 — HTTP webhook receiver

**`app/main.py`** — new route:

```python
@app.post("/api/events/inbound/{watch_id}")
async def inbound_webhook(watch_id: int, request: Request, db=Depends(get_db)):
    watch = get_watch(watch_id, db)
    if not watch or watch.status != "active":
        raise HTTPException(404, "Watch not found or inactive")

    # Validate shared secret if configured
    secret = watch.source_config.get("secret")
    if secret:
        sig = request.headers.get("X-Hub-Signature-256", "")
        body = await request.body()
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            raise HTTPException(403, "Invalid signature")

    payload = (await request.body()).decode("utf-8", errors="replace")[:16384]  # cap at 16 KB
    result = EventDispatcher().dispatch(watch_id, payload, db, get_settings())
    return result
```

`source_config` for webhook watches:
```json
{
  "secret": "optional-shared-secret-for-HMAC-validation"
}
```

Default `fire_config` for webhooks: `{"cooldown_seconds": 60, "use_content_hash": false}`.

---

### Phase 4 — File system watcher

**`app/agent/file_watcher.py`** — new file. Uses the `watchdog` library:

```python
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

class MaestroFileWatcher:
    def __init__(self, db_factory, settings):
        self.observer = Observer()
        self.db_factory = db_factory
        self.settings = settings

    def start(self):
        """Called from app lifespan on startup. Loads all active file_watch watches."""
        with self.db_factory() as db:
            watches = list_watches(event_type='file_watch', db=db)
        for watch in watches:
            self._add_watch(watch)
        self.observer.start()

    def _add_watch(self, watch):
        path = watch.source_config["path"]
        handler = _WatchHandler(watch.id, self.db_factory, self.settings)
        self.observer.schedule(handler, path, recursive=watch.source_config.get("recursive", False))

class _WatchHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory:
            return
        payload = f"File created: {event.src_path}"
        with self.db_factory() as db:
            EventDispatcher().dispatch(self.watch_id, payload, db, self.settings)

    def on_modified(self, event):
        if event.is_directory:
            return
        payload = f"File modified: {event.src_path}"
        with self.db_factory() as db:
            EventDispatcher().dispatch(self.watch_id, payload, db, self.settings)
```

`source_config` for file_watch:
```json
{
  "path": "C:/Users/mdm16/Documents/Inbox",
  "recursive": false,
  "events": ["created", "modified"]
}
```

Default `fire_config`: `{"cooldown_seconds": 5, "use_content_hash": false}`.

The observer is registered in `app/main.py` `lifespan` alongside the scheduler startup.
When a new file_watch watch is registered via `register_watch()`, a signal is sent to add
the new watch to the running observer without restart.

---

### Phase 5 — Scheduled API poll

**`app/agent/api_poller.py`** — new file. Runs as part of the scheduler tick loop:

```python
def poll_due_watches(db, settings):
    """Called every scheduler tick. Checks api_poll watches whose next_poll_at <= now()."""
    due = db.execute(
        "SELECT * FROM watched_events WHERE event_type = 'api_poll' "
        "AND status = 'active' AND (last_fired_at IS NULL OR "
        "last_fired_at + (source_config->>'poll_interval_seconds')::int * interval '1 second' <= now())"
    ).fetchall()

    for watch in due:
        try:
            response = httpx.get(
                watch.source_config["url"],
                timeout=watch.source_config.get("timeout_seconds", 30),
                headers=watch.source_config.get("headers", {}),
            )
            payload = response.text[:16384]  # cap at 16 KB
            EventDispatcher().dispatch(watch.id, payload, db, settings)
        except Exception as e:
            log_watch_error(watch.id, str(e), db)
```

`source_config` for api_poll:
```json
{
  "url": "https://export.arxiv.org/api/query?search_query=ti:twin+primes&max_results=5",
  "poll_interval_seconds": 3600,
  "timeout_seconds": 30,
  "headers": {}
}
```

Default `fire_config`: `{"cooldown_seconds": 0, "use_content_hash": true}` — content hash
is the natural dedup for polling (only fire when results actually change).

---

### Phase 6 — `register_watch` tool

**`app/agent/tools.py`** — register:

```python
"register_watch": {
    "fn": handle_register_watch,
    "schema": {
        "name": "register_watch",
        "description": (
            "Register an event watch that will trigger a Maestro autopilot tick "
            "when an external event occurs. Supported types: webhook, file_watch, api_poll."
        ),
        "parameters": {
            "event_type":    {"type": "string", "enum": ["webhook", "file_watch", "api_poll"]},
            "label":         {"type": "string", "description": "Human-readable name for this watch."},
            "source_config": {"type": "object", "description": "Event-source configuration (path, url, etc.)."},
            "fire_config":   {"type": "object", "description": "Dedup config: cooldown_seconds, use_content_hash, max_fires, expires_at."},
        },
        "required": ["event_type", "label", "source_config"]
    }
}
```

`handle_register_watch` creates the DB record, then:
- For `file_watch`: sends a signal to `MaestroFileWatcher` to add the new path to the running observer.
- For `webhook`: returns the inbound URL (`/api/events/inbound/{watch_id}`) so the agent can record it.
- For `api_poll`: the next scheduler tick will pick it up automatically.

A complementary `list_watches(project=None)` tool lets agents inspect active watches.

---

### Phase 7 — Failure handling

- **API poll failures**: Logged to `watch_error_log` table (watch_id, error, created_at). After 3 consecutive failures, watch status set to `paused`. MCP tool `get_watch_errors(watch_id)` surfaces them. Human (or Maestro) must re-activate.
- **Webhook invalid signatures**: Return 403, no firing, no error log (expected from mis-configured senders).
- **File watcher path not found**: Observer logs a warning at startup; watch is skipped but not deleted. If the path appears later, a server restart will pick it up.
- **Dispatcher crash during Maestro tick**: Exception caught, logged, `last_fired_at` is still updated (prevents re-firing into a broken state). Error surfaced in project health.

```sql
CREATE TABLE watch_error_log (
    id         SERIAL PRIMARY KEY,
    watch_id   INTEGER NOT NULL REFERENCES watched_events(id) ON DELETE CASCADE,
    error      TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

---

### Phase 8 — Tests

1. **Unit** — `should_fire`: cooldown suppression; content hash match → no fire; hash mismatch → fire; max_fires reached → expires; expiry date passed → expires.
2. **Unit** — `record_firing`: `fire_count` increments; `last_payload_hash` updated; `last_fired_at` updated.
3. **Unit** — webhook route: valid HMAC → dispatches; invalid HMAC → 403; inactive watch → 404.
4. **Unit** — `api_poller.poll_due_watches`: due watches trigger dispatch; not-yet-due watches skipped.
5. **Unit** — `register_watch` for webhook: returns inbound URL in result.
6. **Integration** — webhook fires → `EventDispatcher.dispatch` → `autopilot_tick` called with event context.
7. **Integration** — api_poll content hash dedup: same response on second poll → no second firing.
8. **Integration** — max_fires: watch fires N times then auto-expires.

---

## Files touched (expected)

| File | Change |
|---|---|
| `app/migrations/versions/NNNN_watched_events.py` | `watched_events` + `watch_error_log` tables |
| `app/database/crud_events.py` | **New file** — watch CRUD + `should_fire` + `record_firing` |
| `app/agent/event_dispatcher.py` | **New file** — `EventDispatcher.dispatch` |
| `app/agent/file_watcher.py` | **New file** — `MaestroFileWatcher` + watchdog handler |
| `app/agent/api_poller.py` | **New file** — `poll_due_watches` |
| `app/agent/maestro_loop.py` | `autopilot_tick` gains `event_context` parameter |
| `app/agent/tools.py` | Register `register_watch` and `list_watches` |
| `app/main.py` | `POST /api/events/inbound/{watch_id}` route; start `MaestroFileWatcher` in lifespan |
| `app/tests/test_event_triggers.py` | **New file** — all tests for this gap |

---

## Acceptance criteria

- [x] A POST to `/api/events/inbound/{watch_id}` with a valid (or no) HMAC signature fires an `autopilot_tick` for the watch's project with the payload as context.
- [x] A file_watch watch fires when a file is created or modified in the watched path; cooldown suppresses storm events.
- [x] An api_poll watch fires only when the fetched content differs from the last firing (content hash dedup).
- [x] All three dedup mechanisms work independently and in combination per watch.
- [x] After `max_fires` firings or `expires_at` passes, the watch auto-expires and stops firing.
- [x] After 3 consecutive api_poll failures, the watch is paused and the error is visible via MCP tools.
- [x] `register_watch()` tool creates the watch, returns the inbound URL for webhook type, and activates file/poll sources without requiring a server restart.
- [x] All new code passes existing test suite with no regressions (951 tests, 0 failures).
