"""
Hugging Face JSONL exporter for the training data pipeline (GAP 11).

Converts qualified agent sessions to the HF conversational format:
  {"messages": [{"role": "user", "content": "..."}, ...]}

Tool calls are serialized as structured text within assistant turns:
  <tool_call>{"name": "read_file", "arguments": {...}}</tool_call>

Tool results appear as tool turns:
  <tool_response>...</tool_response>

System prompts are stripped entirely — the first turn is always the task
description injected as a user message.
"""

import hashlib
import json
import logging
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from app.database.session import SessionLocal
from app.database.models import BudgetEntry, Task, TrainingSessionScore
from app.database.crud_costs import reconstruct_messages_for_entry
from app.database.crud_training import (
    get_qualified_unexported_sessions,
    mark_sessions_exported,
    count_qualified_unexported,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message formatting
# ---------------------------------------------------------------------------

def _format_task_preamble(task: Task) -> str:
    """Build the injected user message that opens each training sequence."""
    parts = [f"Task: {task.title}"]
    if task.description:
        parts.append(task.description.strip())
    if task.acceptance_criteria:
        try:
            criteria = json.loads(task.acceptance_criteria)
            if isinstance(criteria, list) and criteria:
                parts.append("Acceptance criteria:\n" + "\n".join(f"- {c}" for c in criteria))
        except (json.JSONDecodeError, TypeError):
            parts.append(task.acceptance_criteria.strip())
    return "\n\n".join(parts)


def _format_assistant_turn(msg: dict) -> dict:
    """Serialize tool_calls within an assistant message into <tool_call> text blocks."""
    content = msg.get("content") or ""
    tool_calls = msg.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", tc)
        content += f"\n<tool_call>\n{json.dumps(fn, ensure_ascii=False, indent=2)}\n</tool_call>"
    return {"role": "assistant", "content": content.strip()}


def _format_tool_turn(msg: dict) -> dict:
    """Wrap a tool result message in <tool_response> tags."""
    raw = msg.get("content") or ""
    return {"role": "tool", "content": f"<tool_response>\n{raw}\n</tool_response>"}


# ---------------------------------------------------------------------------
# Session export
# ---------------------------------------------------------------------------

def export_session_to_hf(session_id: str, task: Task, db) -> dict | None:
    """
    Reconstruct the full message history for a session and format it as an
    HF conversational record. Returns None if the session is too short or
    reconstruction fails.
    """
    entries = (
        db.query(BudgetEntry)
        .filter(BudgetEntry.session_id == session_id)
        .order_by(BudgetEntry.id.asc())
        .all()
    )
    if not entries:
        return None

    messages: list[dict] = [
        {"role": "user", "content": _format_task_preamble(task)}
    ]

    seen_msg_count = 0
    for entry in entries:
        full = reconstruct_messages_for_entry(entry.id, db)
        # Only process the *new* messages added by this turn (delta slice)
        new_msgs = full[seen_msg_count:]
        seen_msg_count = len(full)

        for msg in new_msgs:
            role = msg.get("role", "")
            if role == "system":
                continue
            if role == "assistant":
                messages.append(_format_assistant_turn(msg))
            elif role == "tool":
                messages.append(_format_tool_turn(msg))
            elif role in ("user",):
                messages.append({"role": role, "content": msg.get("content") or ""})

    # Need at least: task preamble + 1 assistant + 1 more turn
    if len(messages) < 3:
        return None

    return {"messages": messages}


# ---------------------------------------------------------------------------
# Near-duplicate filtering
# ---------------------------------------------------------------------------

def build_session_fingerprint(task: Task) -> str:
    """SHA256 of the normalised task description — sessions with the same task are near-duplicates."""
    text = (task.description or task.title or "").strip().lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def deduplicate_sessions(
    sessions: list[TrainingSessionScore],
    tasks_by_id: dict[str, Task],
    max_per_fingerprint: int = 3,
) -> list[TrainingSessionScore]:
    """Keep only the top-N sessions per task fingerprint (ranked by score)."""
    by_fp: dict[str, list[TrainingSessionScore]] = defaultdict(list)
    for s in sessions:
        task = tasks_by_id.get(s.task_id)
        fp = build_session_fingerprint(task) if task else s.task_id
        by_fp[fp].append(s)

    result: list[TrainingSessionScore] = []
    for fp_sessions in by_fp.values():
        fp_sessions.sort(key=lambda s: s.score, reverse=True)
        result.extend(fp_sessions[:max_per_fingerprint])
    return result


# ---------------------------------------------------------------------------
# Export orchestrator
# ---------------------------------------------------------------------------

def run_export(
    export_dir: str = "data/training_exports",
    export_max: int = 1000,
    dedup_max: int = 3,
) -> tuple[str, int] | None:
    """
    Pull qualified unexported sessions, deduplicate, format as HF JSONL, write to disk.

    Returns (path, count) on success, or None if there are no qualifying records.
    """
    db = SessionLocal()
    try:
        sessions = get_qualified_unexported_sessions(db=db, limit=export_max)
        if not sessions:
            return None

        # Prefetch tasks for dedup fingerprinting
        task_ids = list({s.task_id for s in sessions})
        task_rows = db.query(Task).filter(Task.id.in_(task_ids)).all()
        tasks_by_id = {t.id: t for t in task_rows}

        sessions = deduplicate_sessions(sessions, tasks_by_id, max_per_fingerprint=dedup_max)

        records: list[dict] = []
        exported_ids: list[str] = []

        for s in sessions:
            task = tasks_by_id.get(s.task_id)
            if not task:
                continue
            record = export_session_to_hf(s.session_id, task, db)
            if record:
                records.append(record)
                exported_ids.append(s.session_id)

        if not records:
            return None

        ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        out_path = Path(export_dir) / f"training_{ts}.jsonl"
        out_path.parent.mkdir(parents=True, exist_ok=True)

        with open(out_path, "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")

        mark_sessions_exported(exported_ids, db=db)
        logger.info("Training export: %d sessions → %s", len(records), out_path)
        return str(out_path), len(records)

    except Exception as exc:
        logger.error("Training export failed: %s", exc)
        return None
    finally:
        db.close()
