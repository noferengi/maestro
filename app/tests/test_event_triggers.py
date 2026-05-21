"""
Tests for GAP 9 — Event-driven triggers.

Covers:
  - should_fire dedup logic (unit)
  - record_firing state updates (unit)
  - webhook route: HMAC validation, 404 on inactive watch (unit)
  - poll_due_watches interval check (unit)
  - register_watch tool returns inbound_url for webhook type (unit)
  - EventDispatcher → run_event_maestro_tick called with event_context (integration)
  - api_poll content-hash dedup suppresses second firing (integration)
  - max_fires auto-expires a watch (integration)
  - 3 consecutive api_poll failures pause a watch (integration)
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import app.database as db_mod
from app.database.crud_events import (
    create_watch,
    get_watch,
    list_watches,
    record_firing,
    should_fire,
    update_watch_status,
    log_watch_error,
    get_consecutive_error_count,
    payload_hash,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_project() -> int:
    """Insert a minimal project row and return its id."""
    import database as db
    proj = db.upsert_project("test-events-project")
    return proj.id


def _make_watch(project_id: int, event_type: str = "webhook", fire_config: dict | None = None):
    return create_watch(
        project_id=project_id,
        event_type=event_type,
        label="test-watch",
        source_config={"secret": None},
        fire_config=fire_config or {},
    )


# ---------------------------------------------------------------------------
# Unit: should_fire
# ---------------------------------------------------------------------------

class TestShouldFire:
    def test_fires_first_time(self):
        project_id = _make_project()
        watch = _make_watch(project_id)
        assert should_fire(watch, "hash1") is True

    def test_cooldown_suppresses(self):
        project_id = _make_project()
        watch = _make_watch(project_id, fire_config={"cooldown_seconds": 3600})
        # Simulate a recent firing
        record_firing(watch.id, "h1")
        watch = get_watch(watch.id)   # reload to get updated last_fired_at
        assert should_fire(watch, "h2") is False

    def test_content_hash_match_suppresses(self):
        project_id = _make_project()
        watch = _make_watch(
            project_id, fire_config={"cooldown_seconds": 0, "use_content_hash": True}
        )
        record_firing(watch.id, "samehash")
        watch = get_watch(watch.id)
        assert should_fire(watch, "samehash") is False

    def test_content_hash_mismatch_fires(self):
        project_id = _make_project()
        watch = _make_watch(
            project_id, fire_config={"cooldown_seconds": 0, "use_content_hash": True}
        )
        record_firing(watch.id, "hash_a")
        watch = get_watch(watch.id)
        assert should_fire(watch, "hash_b") is True

    def test_max_fires_reached_expires(self):
        project_id = _make_project()
        watch = _make_watch(project_id, fire_config={"max_fires": 2, "cooldown_seconds": 0})
        record_firing(watch.id, "h1")
        record_firing(watch.id, "h2")
        watch = get_watch(watch.id)
        assert should_fire(watch, "h3") is False
        # Status should now be expired
        refreshed = get_watch(watch.id)
        assert refreshed.status == "expired"

    def test_expires_at_passed_expires(self):
        project_id = _make_project()
        past = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
        watch = _make_watch(
            project_id, fire_config={"expires_at": past, "cooldown_seconds": 0}
        )
        assert should_fire(watch, "h1") is False
        refreshed = get_watch(watch.id)
        assert refreshed.status == "expired"


# ---------------------------------------------------------------------------
# Unit: record_firing
# ---------------------------------------------------------------------------

class TestRecordFiring:
    def test_increments_fire_count(self):
        project_id = _make_project()
        watch = _make_watch(project_id)
        assert watch.fire_count == 0
        record_firing(watch.id, "h1")
        record_firing(watch.id, "h2")
        refreshed = get_watch(watch.id)
        assert refreshed.fire_count == 2

    def test_updates_last_payload_hash(self):
        project_id = _make_project()
        watch = _make_watch(project_id)
        record_firing(watch.id, "myhash")
        refreshed = get_watch(watch.id)
        assert refreshed.last_payload_hash == "myhash"

    def test_updates_last_fired_at(self):
        project_id = _make_project()
        watch = _make_watch(project_id)
        before = datetime.now(timezone.utc)
        record_firing(watch.id, "h")
        refreshed = get_watch(watch.id)
        assert refreshed.last_fired_at is not None
        last = refreshed.last_fired_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        assert last >= before

    def test_auto_expires_on_max_fires(self):
        project_id = _make_project()
        watch = _make_watch(project_id, fire_config={"max_fires": 1, "cooldown_seconds": 0})
        record_firing(watch.id, "h1")
        refreshed = get_watch(watch.id)
        assert refreshed.status == "expired"


# ---------------------------------------------------------------------------
# Unit: webhook route HMAC + 404
# ---------------------------------------------------------------------------

class TestWebhookRoute:
    @pytest.fixture(scope="class")
    def client(self):
        from app.main import app
        # Patch scheduler and file watcher to avoid blocking during lifespan startup
        with (
            patch("app.agent.scheduler.start_scheduler"),
            patch("app.agent.scheduler.stop_scheduler"),
            patch("app.agent.file_watcher.MaestroFileWatcher"),
        ):
            with TestClient(app, raise_server_exceptions=False) as c:
                yield c

    def test_inactive_watch_returns_404(self, client):
        project_id = _make_project()
        watch = _make_watch(project_id, event_type="webhook")
        update_watch_status(watch.id, "paused")
        resp = client.post(f"/api/events/inbound/{watch.id}", content=b"payload")
        assert resp.status_code == 404

    def test_missing_watch_returns_404(self, client):
        resp = client.post("/api/events/inbound/999999", content=b"payload")
        assert resp.status_code == 404

    def test_invalid_hmac_returns_403(self, client):
        project_id = _make_project()
        watch = _make_watch(
            project_id,
            event_type="webhook",
            fire_config={"cooldown_seconds": 0},
        )
        # Re-create with a secret
        import database as db
        from app.database.crud_events import create_watch as cw
        w2 = cw(
            project_id=project_id,
            event_type="webhook",
            label="secret-watch",
            source_config={"secret": "topsecret"},
            fire_config={"cooldown_seconds": 0},
        )
        resp = client.post(
            f"/api/events/inbound/{w2.id}",
            content=b"payload",
            headers={"X-Hub-Signature-256": "sha256=badsig"},
        )
        assert resp.status_code == 403

    def test_no_hmac_fires(self, client):
        """A watch without a secret accepts any POST."""
        project_id = _make_project()
        watch = _make_watch(project_id, event_type="webhook", fire_config={"cooldown_seconds": 0})

        with patch("app.agent.event_dispatcher.run_event_maestro_tick") as mock_tick:
            mock_tick.return_value = {"status": "no_action", "turns": 0}
            resp = client.post(f"/api/events/inbound/{watch.id}", content=b"hello")
        assert resp.status_code == 200
        data = resp.json()
        assert data["fired"] is True


# ---------------------------------------------------------------------------
# Unit: poll_due_watches interval check
# ---------------------------------------------------------------------------

class TestApiPoller:
    def test_due_watch_triggers_dispatch(self):
        project_id = _make_project()
        watch = create_watch(
            project_id=project_id,
            event_type="api_poll",
            label="poll-watch",
            source_config={"url": "http://example.com", "poll_interval_seconds": 3600},
            fire_config={"cooldown_seconds": 0, "use_content_hash": True},
        )
        # last_fired_at is None → always due

        mock_resp = MagicMock()
        mock_resp.text = "result"

        with (
            patch("httpx.get", return_value=mock_resp),
            patch("app.agent.event_dispatcher.EventDispatcher") as mock_ed,
        ):
            mock_ed.return_value.dispatch.return_value = {"fired": True}

            from app.agent.api_poller import poll_due_watches
            poll_due_watches()

        mock_ed.return_value.dispatch.assert_called_once_with(watch.id, "result")

    def test_not_yet_due_watch_is_skipped(self):
        project_id = _make_project()
        watch = create_watch(
            project_id=project_id,
            event_type="api_poll",
            label="poll-not-due",
            source_config={"url": "http://example.com", "poll_interval_seconds": 3600},
        )
        # Simulate recent firing
        record_firing(watch.id, "h")

        with (
            patch("httpx.get") as mock_get,
            patch("app.agent.event_dispatcher.EventDispatcher") as mock_ed,
        ):
            from app.agent.api_poller import poll_due_watches
            poll_due_watches()

        mock_ed.return_value.dispatch.assert_not_called()
        mock_get.assert_not_called()


# ---------------------------------------------------------------------------
# Unit: register_watch tool returns inbound_url for webhook
# ---------------------------------------------------------------------------

class TestRegisterWatchTool:
    def test_returns_inbound_url_for_webhook(self):
        project_id = _make_project()

        import database as db
        task = db.create_task(
            title="test task",
            description="desc",
            task_type="idea",
            project="test-events-project",
        )

        from app.agent.tools import handle_register_watch
        import json

        with patch("app.agent.file_watcher.get_file_watcher", return_value=None):
            result_str = handle_register_watch(
                event_type="webhook",
                label="my-hook",
                source_config={},
                task_id=task.id,
            )

        result = json.loads(result_str)
        assert "inbound_url" in result
        assert result["inbound_url"].startswith("/api/events/inbound/")

    def test_returns_message_for_api_poll(self):
        project_id = _make_project()

        import database as db
        task = db.create_task(
            title="test task 2",
            description="desc",
            task_type="idea",
            project="test-events-project",
        )

        from app.agent.tools import handle_register_watch
        import json

        result_str = handle_register_watch(
            event_type="api_poll",
            label="arxiv-poll",
            source_config={"url": "http://example.com", "poll_interval_seconds": 3600},
            task_id=task.id,
        )

        result = json.loads(result_str)
        assert "message" in result
        assert "watch_id" in result


# ---------------------------------------------------------------------------
# Integration: EventDispatcher → run_event_maestro_tick with event_context
# ---------------------------------------------------------------------------

class TestEventDispatcherIntegration:
    def test_dispatch_calls_tick_with_event_context(self):
        project_id = _make_project()

        # Give the project an LLM and budget (fake IDs — we mock the tick)
        import database as db
        proj = db.get_project("test-events-project")

        watch = create_watch(
            project_id=project_id,
            event_type="webhook",
            label="integration-watch",
            source_config={},
            fire_config={"cooldown_seconds": 0},
        )

        from app.agent.event_dispatcher import EventDispatcher

        with patch("app.agent.event_dispatcher.run_event_maestro_tick") as mock_tick:
            mock_tick.return_value = {"status": "no_action"}
            result = EventDispatcher().dispatch(watch.id, "git push detected")

        assert result["fired"] is True
        mock_tick.assert_called_once()
        context_arg = mock_tick.call_args[0][1]
        assert "integration-watch" in context_arg
        assert "git push detected" in context_arg

    def test_dedup_suppresses_same_hash(self):
        project_id = _make_project()
        watch = create_watch(
            project_id=project_id,
            event_type="api_poll",
            label="dedup-watch",
            source_config={},
            fire_config={"use_content_hash": True, "cooldown_seconds": 0},
        )

        from app.agent.event_dispatcher import EventDispatcher

        with patch("app.agent.event_dispatcher.run_event_maestro_tick") as mock_tick:
            mock_tick.return_value = {"status": "no_action"}
            r1 = EventDispatcher().dispatch(watch.id, "same content")
            r2 = EventDispatcher().dispatch(watch.id, "same content")

        assert r1["fired"] is True
        assert r2["fired"] is False
        assert r2["reason"] == "dedup suppressed"
        mock_tick.assert_called_once()


# ---------------------------------------------------------------------------
# Integration: max_fires auto-expires
# ---------------------------------------------------------------------------

class TestMaxFiresExpiry:
    def test_watch_expires_after_max_fires(self):
        project_id = _make_project()
        watch = create_watch(
            project_id=project_id,
            event_type="webhook",
            label="limited-watch",
            source_config={},
            fire_config={"max_fires": 2, "cooldown_seconds": 0},
        )

        from app.agent.event_dispatcher import EventDispatcher

        with patch("app.agent.event_dispatcher.run_event_maestro_tick") as mock_tick:
            mock_tick.return_value = {"status": "no_action"}
            EventDispatcher().dispatch(watch.id, "fire1")
            EventDispatcher().dispatch(watch.id, "fire2")
            r3 = EventDispatcher().dispatch(watch.id, "fire3")

        # Third dispatch should be suppressed because watch auto-expired
        assert r3["fired"] is False
        refreshed = get_watch(watch.id)
        assert refreshed.status == "expired"


# ---------------------------------------------------------------------------
# Integration: 3 consecutive api_poll failures pause watch
# ---------------------------------------------------------------------------

class TestApiPollFailurePause:
    def test_three_consecutive_failures_pause_watch(self):
        project_id = _make_project()
        watch = create_watch(
            project_id=project_id,
            event_type="api_poll",
            label="failing-poll",
            source_config={"url": "http://bad.example.com", "poll_interval_seconds": 0},
        )

        from app.database.crud_events import log_watch_error as lwe

        lwe(watch.id, "error 1")
        lwe(watch.id, "error 2")
        lwe(watch.id, "error 3")

        refreshed = get_watch(watch.id)
        assert refreshed.status == "paused"
