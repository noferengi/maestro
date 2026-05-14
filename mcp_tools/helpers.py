"""Shared utilities for MCP tool implementations."""

import json
from sqlalchemy import text

# Re-use the application's engine — shares the connection pool and dialect
# (SQLite with WAL mode, or PostgreSQL with MVCC) rather than opening an
# independent raw sqlite3 connection that causes exclusive-lock contention.
from app.database.session import engine

DISPATCHABLE_TYPES = {
    "planning", "indev", "conceptual_review", "optimization",
    "security", "human_review", "subdividing", "pip_resolution",
}


# ---------------------------------------------------------------------------
# Compatibility shims — keep callers unchanged
# ---------------------------------------------------------------------------

class _Row(dict):
    """
    Dict subclass that also supports integer-index access, matching the
    sqlite3.Row API used throughout the MCP diagnostic code.
    """
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class _Result:
    """Wraps a SQLAlchemy CursorResult with a sqlite3-compatible interface."""

    def __init__(self, cursor_result):
        self._cr = cursor_result
        # Capture rowcount immediately — the cursor may be consumed after fetchall/fetchone.
        self.rowcount = cursor_result.rowcount

    def fetchone(self):
        row = self._cr.mappings().fetchone()
        return _Row(row) if row is not None else None

    def fetchall(self):
        return [_Row(r) for r in self._cr.mappings().fetchall()]

    def __iter__(self):
        for r in self._cr.mappings():
            yield _Row(r)


class _Conn:
    """
    Wraps an SQLAlchemy Connection to present the API used throughout the MCP
    tool code: ?-style positional params, dict-like row access, close/commit.

    Both get_conn() and get_rw_conn() return one of these.  For read paths,
    close() rolls back the implicit (empty) transaction — safe and fast.
    For write paths, call commit() before close() to persist the changes.

    Also supports the sqlite3.Cursor pattern (execute then fetchone/fetchall on
    the same object) so scripts like inspect_cards.py can use it as a drop-in
    cursor replacement.
    """

    def __init__(self, sa_conn):
        self._conn = sa_conn
        self._last_res: _Result | None = None

    def execute(self, sql: str, params=None) -> "_Result":
        """Execute *sql* with optional positional params (?-style)."""
        if params:
            # Convert ?-style positional params to :p0 :p1 ... named params
            # required by SQLAlchemy text().  Works for lists and tuples.
            parts = sql.split("?")
            named: dict = {}
            new_sql = parts[0]
            for i, part in enumerate(parts[1:]):
                named[f"p{i}"] = params[i]
                new_sql += f":p{i}" + part
            sql, params = new_sql, named
        self._last_res = _Result(self._conn.execute(text(sql), params or {}))
        return self._last_res

    def fetchone(self):
        """Return one row from the last execute() — sqlite3.Cursor-compatible."""
        return self._last_res.fetchone() if self._last_res else None

    def fetchall(self):
        """Return all rows from the last execute() — sqlite3.Cursor-compatible."""
        return self._last_res.fetchall() if self._last_res else []

    def commit(self):
        self._conn.commit()

    def close(self):
        self._conn.close()


def get_conn() -> _Conn:
    """
    Read connection to the application database.

    Uses the shared SQLAlchemy engine (SQLite+WAL or PostgreSQL) so reads
    from MCP tools are never serialised behind the scheduler's write lock.
    On PostgreSQL each connection gets its own MVCC snapshot — reads and
    writes are fully parallel with no lock contention.
    """
    return _Conn(engine.connect())


def get_rw_conn() -> _Conn:
    """
    Read-write connection — use only in tools that must write to the DB.

    Call commit() to persist changes, then close() to release the connection
    back to the pool.
    """
    return _Conn(engine.connect())


def _date_ago(n: int, unit: str) -> str:
    """
    Return a SQL literal for 'N units ago', compatible with both SQLite and
    PostgreSQL.  Embed directly into a query string (not a bind param) since
    both dialects express this as a function call, not a value.

      _date_ago(1, 'day')   → "datetime('now', '-1 day')"  (SQLite)
                            → "NOW() - INTERVAL '1 day'"   (PostgreSQL)
    """
    if engine.dialect.name == "postgresql":
        return f"NOW() - INTERVAL '{n} {unit}'"
    return f"datetime('now', '-{n} {unit}')"


# ---------------------------------------------------------------------------
# Shared helpers (unchanged)
# ---------------------------------------------------------------------------

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
