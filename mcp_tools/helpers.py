"""Shared utilities for MCP tool implementations."""

import json
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "kanban.db"

DISPATCHABLE_TYPES = {
    "planning", "indev", "conceptual_review", "optimization",
    "security", "human_review", "subdividing", "pip_resolution",
}


def get_conn() -> sqlite3.Connection:
    """Read-only SQLite connection to kanban.db."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_rw_conn() -> sqlite3.Connection:
    """Read-write SQLite connection — use only in action tools."""
    conn = sqlite3.connect(str(DB_PATH), timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def extract_response_fields(response_data: str) -> dict:
    """
    Extract key fields from a raw LLM response_data JSON blob.
    Returns finish_reason, content_preview, reasoning_preview.
    """
    try:
        data = json.loads(response_data or "{}")
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message", {})
        content = (msg.get("content") or "").strip()
        reasoning = (msg.get("reasoning_content") or "").strip()
        return {
            "finish_reason": choice.get("finish_reason", ""),
            "content_preview": content[:400] if content else "",
            "reasoning_preview": reasoning[:200] if reasoning else "",
            "has_tool_calls": bool(msg.get("tool_calls")),
        }
    except Exception:
        return {"finish_reason": "", "content_preview": "", "reasoning_preview": "", "has_tool_calls": False}


def parse_gate_checks(vote_summary: str) -> list:
    """Extract gate_checks array from a transition_result vote_summary blob."""
    try:
        data = json.loads(vote_summary or "{}")
        return data.get("checks", [])
    except Exception:
        return []


def parse_json_field(value: str | None) -> object:
    """Parse a JSON column that may be None, empty, or a JSON string."""
    if not value:
        return None
    try:
        return json.loads(value)
    except Exception:
        return value
