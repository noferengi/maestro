"""
app/agent/stream_broker.py
--------------------------
Thread-safe ring-buffer token broker for live LLM stream visibility.

The LLM client calls publish() for every content token chunk as it arrives
from the upstream LLM server.  SSE consumers in main.py poll
get_tokens_since() to forward those chunks to browser clients.

Architecture notes
------------------
- All public functions are safe to call from any thread or asyncio context.
- The lock is a plain threading.Lock (not asyncio) so agent worker threads
  (which run their own event loops) can publish without cross-loop issues.
- Ring buffer per task: last RING_SIZE chunks are kept so a reconnecting
  browser client can catch up without needing a full replay.
- task_id registration is implicit: publish() creates the buffer on first use.
"""
from __future__ import annotations

import threading
import time

RING_SIZE = 400   # token chunks kept per task

_lock = threading.Lock()
_buffers: dict[str, list[dict]] = {}   # task_id -> list of event dicts
_sequence: dict[str, int] = {}         # task_id -> monotonic counter
_meta: dict[str, dict] = {}            # task_id -> last-activity metadata


def publish(
    task_id: str,
    text: str,
    *,
    agent_name: str = "",
    session_id: str = "",
    turn_type: str = "content",
) -> None:
    """Publish a token chunk for task_id.

    turn_type values:
      "content"      — regular streamed text token
      "tool_invoked" — tool call started (text = tool name list JSON)
      "turn_end"     — LLM generation complete for this turn
    """
    if not task_id:
        return
    now = time.time()
    with _lock:
        if task_id not in _buffers:
            _buffers[task_id] = []
            _sequence[task_id] = 0
        seq = _sequence[task_id] + 1
        _sequence[task_id] = seq
        _buffers[task_id].append({
            "seq": seq,
            "text": text,
            "agent_name": agent_name,
            "session_id": session_id,
            "turn_type": turn_type,
            "ts": now,
        })
        if len(_buffers[task_id]) > RING_SIZE:
            del _buffers[task_id][:-RING_SIZE]
        _meta[task_id] = {
            "agent_name": agent_name,
            "session_id": session_id,
            "last_activity": now,
        }


def get_tokens_since(task_id: str, since_seq: int) -> list[dict]:
    """Return all buffered events with seq > since_seq (thread-safe snapshot)."""
    with _lock:
        buf = _buffers.get(task_id)
        if not buf:
            return []
        return [t for t in buf if t["seq"] > since_seq]


def get_current_seq(task_id: str) -> int:
    with _lock:
        return _sequence.get(task_id, 0)


def get_meta(task_id: str) -> dict:
    with _lock:
        return dict(_meta.get(task_id, {}))


def last_activity_age(task_id: str) -> float:
    """Seconds since the last publish() for this task (float('inf') if never)."""
    with _lock:
        m = _meta.get(task_id)
        if not m:
            return float("inf")
        return time.time() - m["last_activity"]
