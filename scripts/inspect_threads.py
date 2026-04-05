#!/usr/bin/env python
"""Inspect scheduler thread state and activity."""
import sys
import os
import threading
import time

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import scheduler internals
from app.agent.scheduler import (
    _active_sessions, _active_sessions_lock,
    _session_llm_ids, _llm_counts_lock, _llm_session_counts,
    _scheduler_thread, SCHEDULER_TICK_INTERVAL
)

def main():
    print("\n" + "="*70)
    print("  SCHEDULER THREAD INSPECTION")
    print("="*70)
    
    # 1. Scheduler thread state
    print("\n[1] Scheduler Thread:")
    if _scheduler_thread is not None:
        print(f"    PID: {_scheduler_thread.ident if _scheduler_thread.ident else 'N/A'}")
        print(f"    Alive: {_scheduler_thread.is_alive()}")
        print(f"    Daemon: {_scheduler_thread.daemon}")
        print(f"    Name: {_scheduler_thread.name}")
    else:
        print("    Scheduler thread not started")
    
    # 2. Active task sessions
    with _active_sessions_lock:
        print("\n[2] Active Task Sessions:")
        if not _active_sessions:
            print("    No active sessions")
        else:
            for tid, t in _active_sessions.items():
                alive = t.is_alive()
                daemon = t.daemon
                name = t.name
                print(f"    {tid}:")
                print(f"        alive={alive}, daemon={daemon}")
                print(f"        name={name}")
                if alive:
                    print(f"        is_alive()={alive}")
    
    # 3. Session LLM mapping
    print("\n[3] Session -> LLM Mapping:")
    with _active_sessions_lock:
        with _llm_counts_lock:
            active_keys = set(_active_sessions.keys())
            session_llm_ids = dict(_session_llm_ids)
            llm_counts = dict(_llm_session_counts)
    
    for key, llm_id in session_llm_ids.items():
        if key in active_keys:
            count = llm_counts.get(llm_id, 0)
            print(f"    {key} -> LLM {llm_id} (count={count})")
    
    # 4. LLM counts
    print("\n[4] LLM Session Counts:")
    with _llm_counts_lock:
        for llm_id, count in _llm_session_counts.items():
            print(f"    LLM {llm_id}: {count}")
    
    # 5. Thread stack traces for alive threads
    print("\n[5] Thread Stack Traces (alive threads only):")
    with _active_sessions_lock:
        for tid, t in _active_sessions.items():
            if t.is_alive():
                print(f"\n    Thread {tid} ({t.name}):")
                try:
                    for frame_summary in threading.current_thread().stack:
                        print(f"      {frame_summary}")
                except Exception as e:
                    print(f"      Error: {e}")
    
    # 6. Check if scheduler is stuck in a loop
    print("\n[6] Scheduler Loop Check:")
    print(f"    Expected tick interval: {SCHEDULER_TICK_INTERVAL}s")
    
    # Check if scheduler is actively running
    print("\n[7] Scheduler Activity:")
    print("    Check server logs for scheduler debug messages:")
    print("    - '[scheduler] tick: dispatching task X'")
    print("    - '[scheduler] One-LLM policy: pinned to LLM N'")
    print("    - '[scheduler] Task X advanced to PLANNING'")
    
    return _active_sessions, _session_llm_ids, _llm_session_counts

if __name__ == "__main__":
    main()
