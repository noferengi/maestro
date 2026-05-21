"""
app/database/crud_events.py
---------------------------
CRUD + dedup logic for the watched_events and watch_error_log tables (GAP 9).
"""
from __future__ import annotations

import hashlib
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from .session import SessionLocal
from .models import WatchedEvent, WatchErrorLog

logger = logging.getLogger(__name__)

_PAUSE_AFTER_CONSECUTIVE_ERRORS = 3


# ---------------------------------------------------------------------------
# Watch CRUD
# ---------------------------------------------------------------------------

def create_watch(
    project_id: int,
    event_type: str,
    label: str,
    source_config: dict,
    fire_config: dict | None = None,
    session_id: str | None = None,
) -> WatchedEvent | None:
    db = SessionLocal()
    try:
        watch = WatchedEvent(
            project_id=project_id,
            event_type=event_type,
            label=label,
            source_config=source_config,
            fire_config=fire_config or {},
            created_by_session=session_id,
        )
        db.add(watch)
        db.commit()
        db.refresh(watch)
        return watch
    except Exception as exc:
        db.rollback()
        logger.error("create_watch failed: %s", exc)
        return None
    finally:
        db.close()


def get_watch(watch_id: int) -> WatchedEvent | None:
    db = SessionLocal()
    try:
        return db.query(WatchedEvent).filter(WatchedEvent.id == watch_id).first()
    except Exception:
        return None
    finally:
        db.close()


def list_watches(
    project_id: int | None = None,
    event_type: str | None = None,
    status: str = "active",
) -> list[WatchedEvent]:
    db = SessionLocal()
    try:
        q = db.query(WatchedEvent)
        if project_id is not None:
            q = q.filter(WatchedEvent.project_id == project_id)
        if event_type is not None:
            q = q.filter(WatchedEvent.event_type == event_type)
        if status is not None:
            q = q.filter(WatchedEvent.status == status)
        return q.order_by(WatchedEvent.id).all()
    except Exception:
        return []
    finally:
        db.close()


def update_watch_status(watch_id: int, status: str) -> None:
    db = SessionLocal()
    try:
        db.query(WatchedEvent).filter(WatchedEvent.id == watch_id).update(
            {"status": status}, synchronize_session=False
        )
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("update_watch_status(%d, %r) failed: %s", watch_id, status, exc)
    finally:
        db.close()


def record_firing(watch_id: int, payload_hash: str | None) -> None:
    """Update fire metadata after a watch fires. Auto-expires on max_fires reached."""
    db = SessionLocal()
    try:
        watch = db.query(WatchedEvent).filter(WatchedEvent.id == watch_id).first()
        if not watch:
            return
        watch.last_fired_at = datetime.now(timezone.utc)
        watch.last_payload_hash = payload_hash
        watch.fire_count = (watch.fire_count or 0) + 1

        max_fires = watch.fire_config.get("max_fires") if watch.fire_config else None
        if max_fires and watch.fire_count >= max_fires:
            watch.status = "expired"
            logger.info("watch %d auto-expired after %d fires", watch_id, watch.fire_count)

        db.commit()
    except Exception as exc:
        db.rollback()
        logger.warning("record_firing(%d) failed: %s", watch_id, exc)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Dedup gate
# ---------------------------------------------------------------------------

def should_fire(watch: WatchedEvent, payload_hash: str | None) -> bool:
    """
    Evaluate all three dedup rules.  Returns True only if the watch should fire.
    Mutates watch status to 'expired' in the DB when limits are crossed.
    """
    cfg: dict[str, Any] = watch.fire_config or {}
    now = datetime.now(timezone.utc)

    # 1. Expiry date
    expires_at = cfg.get("expires_at")
    if expires_at:
        try:
            exp_dt = datetime.fromisoformat(str(expires_at))
            if exp_dt.tzinfo is None:
                exp_dt = exp_dt.replace(tzinfo=timezone.utc)
            if now >= exp_dt:
                update_watch_status(watch.id, "expired")
                return False
        except (ValueError, TypeError):
            pass

    # 2. Max fires (DB is authoritative; watch object may be stale after record_firing)
    max_fires = cfg.get("max_fires")
    if max_fires is not None and (watch.fire_count or 0) >= max_fires:
        update_watch_status(watch.id, "expired")
        return False

    # 3. Cooldown window
    cooldown = cfg.get("cooldown_seconds", 60)
    if watch.last_fired_at and cooldown > 0:
        last = watch.last_fired_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        if (now - last).total_seconds() < cooldown:
            return False

    # 4. Content hash — only suppress if hash matches AND feature is on
    if cfg.get("use_content_hash") and payload_hash is not None:
        if payload_hash == watch.last_payload_hash:
            return False

    return True


# ---------------------------------------------------------------------------
# Error logging
# ---------------------------------------------------------------------------

def log_watch_error(watch_id: int, error: str) -> None:
    """Record an error and pause the watch after 3 consecutive failures."""
    db = SessionLocal()
    try:
        entry = WatchErrorLog(watch_id=watch_id, error=error[:4096])
        db.add(entry)
        db.commit()

        consecutive = _get_consecutive_error_count_db(watch_id, db)
        if consecutive >= _PAUSE_AFTER_CONSECUTIVE_ERRORS:
            db.query(WatchedEvent).filter(WatchedEvent.id == watch_id).update(
                {"status": "paused"}, synchronize_session=False
            )
            db.commit()
            logger.warning(
                "watch %d paused after %d consecutive errors", watch_id, consecutive
            )
    except Exception as exc:
        try:
            db.rollback()
        except Exception:
            pass
        logger.error("log_watch_error(%d) failed: %s", watch_id, exc)
    finally:
        db.close()


def get_watch_errors(watch_id: int, limit: int = 20) -> list[WatchErrorLog]:
    db = SessionLocal()
    try:
        return (
            db.query(WatchErrorLog)
            .filter(WatchErrorLog.watch_id == watch_id)
            .order_by(WatchErrorLog.created_at.desc())
            .limit(limit)
            .all()
        )
    except Exception:
        return []
    finally:
        db.close()


def get_consecutive_error_count(watch_id: int) -> int:
    db = SessionLocal()
    try:
        return _get_consecutive_error_count_db(watch_id, db)
    finally:
        db.close()


def _get_consecutive_error_count_db(watch_id: int, db) -> int:
    """Count errors since the last successful firing (last_fired_at)."""
    watch = db.query(WatchedEvent).filter(WatchedEvent.id == watch_id).first()
    if not watch:
        return 0
    q = db.query(WatchErrorLog).filter(WatchErrorLog.watch_id == watch_id)
    if watch.last_fired_at:
        last = watch.last_fired_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        q = q.filter(WatchErrorLog.created_at > last)
    return q.count()


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def payload_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode()).hexdigest()
