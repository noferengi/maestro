#!/usr/bin/env python
"""
scripts/tail_task.py — live stream of LLM calls for a running task.

Usage:
    venv/Scripts/python.exe scripts/tail_task.py <task_id> [poll_seconds]

Each LLM turn prints one line to stdout as soon as it lands in the DB.
Designed to be fed into the Claude Code Monitor tool for live watching:

    Monitor(command="venv/Scripts/python.exe scripts/tail_task.py task-1779408180.714483")

Output format (one line per LLM call):
    HH:MM:SS [finish_reason] ctx=NN gen=NNN  tool_names | reasoning snippet...
"""

import sys
import os
import time
import json

_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from app.database.session import engine
from sqlalchemy import text


def _query_new_entries(conn, task_id: str, since_id: int) -> list[dict]:
    result = conn.execute(
        text(
            "SELECT id, agent_name, generation_cost, prompt_message_count, "
            "       response_data, created_at "
            "FROM budget_entries "
            "WHERE task_id = :tid AND id > :sid "
            "ORDER BY id ASC LIMIT 50"
        ),
        {"tid": task_id, "sid": since_id},
    )
    return [dict(r._mapping) for r in result]


def _extract(row: dict) -> dict:
    try:
        data = json.loads(row["response_data"] or "{}")
    except Exception:
        data = {}
    choice = (data.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    finish = choice.get("finish_reason") or "?"
    reasoning = (msg.get("reasoning_content") or "").strip().replace("\n", " ")
    content = (msg.get("content") or "").strip().replace("\n", " ")
    tool_calls = msg.get("tool_calls") or []
    tool_names = [tc.get("function", {}).get("name", "?") for tc in tool_calls if isinstance(tc, dict)]
    return {
        "finish": finish,
        "reasoning": reasoning[:120] if reasoning else "",
        "content": content[:120] if content else "",
        "tools": tool_names,
    }


def _task_active(conn, task_id: str) -> bool:
    row = conn.execute(
        text("SELECT id FROM agent_sessions WHERE task_id = :tid AND ended_at IS NULL LIMIT 1"),
        {"tid": task_id},
    ).fetchone()
    return row is not None


def _task_stage(conn, task_id: str) -> str:
    row = conn.execute(
        text("SELECT type FROM tasks WHERE id = :tid"),
        {"tid": task_id},
    ).fetchone()
    return row[0] if row else "?"


def main():
    if len(sys.argv) < 2:
        print("Usage: tail_task.py <task_id> [poll_seconds]", file=sys.stderr)
        sys.exit(1)

    task_id = sys.argv[1]
    poll = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0

    since_id = 0
    consecutive_idle = 0

    print(f"[tail_task] watching {task_id} (poll={poll}s) — Ctrl-C to stop", flush=True)

    while True:
        try:
            with engine.connect() as raw_conn:
                active = _task_active(raw_conn, task_id)
                stage = _task_stage(raw_conn, task_id)
                rows = _query_new_entries(raw_conn, task_id, since_id)

            if rows:
                consecutive_idle = 0
                for r in rows:
                    since_id = r["id"]
                    x = _extract(r)
                    ts = str(r["created_at"])[11:19]  # HH:MM:SS
                    ctx = r["prompt_message_count"] or 0
                    gen = r["generation_cost"] or 0
                    tools_str = " + ".join(x["tools"]) if x["tools"] else ""
                    snippet = x["reasoning"] or x["content"] or "(no text)"

                    if tools_str:
                        line = f"{ts} [{x['finish']}] ctx={ctx} gen={gen}  {tools_str} | {snippet[:80]}"
                    else:
                        line = f"{ts} [{x['finish']}] ctx={ctx} gen={gen}  {snippet[:100]}"
                    print(line, flush=True)
            else:
                consecutive_idle += 1
                # Print a heartbeat every ~30s of silence so the monitor knows we're alive
                if consecutive_idle % 10 == 0:
                    status = "ACTIVE" if active else "IDLE"
                    print(f"[tail_task] {status} stage={stage} since_id={since_id} — waiting...", flush=True)

            if not active and consecutive_idle > 5:
                print(f"[tail_task] session ended (stage={stage}), exiting.", flush=True)
                break

        except KeyboardInterrupt:
            print("[tail_task] interrupted.", flush=True)
            break
        except Exception as exc:
            print(f"[tail_task] ERROR: {exc}", flush=True)

        time.sleep(poll)


if __name__ == "__main__":
    main()
