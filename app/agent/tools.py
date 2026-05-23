"""
app/agent/tools.py
------------------
Safe tool definitions and implementations for the Maestro agent.

Every tool has:
  1. A Python implementation that enforces project-root containment and
     never performs hard deletes.
  2. An OpenAI-format JSON schema entry in TOOL_SCHEMAS.
  3. Registration in TOOL_REGISTRY keyed by the tool name.

The public entry point is dispatch_tool(name, arguments) -> str.
"""

from __future__ import annotations

import contextvars
import glob as _glob
import hashlib
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Internal imports
# ---------------------------------------------------------------------------
# We import config lazily inside functions where needed to avoid circular deps
# at module import time, but we do need a few constants up front.
from app.agent.config import (
    ARCHIVE_DIR,
    PROJECT_ROOT,
    SHELL_TIMEOUT_SECONDS,
    GIT_SAFETY_BRANCH_PREFIX,
    GIT_ALLOWED_BASE_BRANCHES,
    GIT_TIMEOUT_SECONDS,
    TOOL_MAX_SEARCH_RESULTS,
    TOOL_MAX_GIT_LOG_ENTRIES,
    TOOL_LISTING_EXCLUDED_DIRS,
    MAESTRO_GIT_ROOT,
    MAESTRO_CAPABILITIES,
    SELF_MODIFICATION_PROJECT,
    SELF_MOD_INTEGRATION_BRANCH,
    SELF_MOD_REVERT_VOTE_THRESHOLD,
)
from app.agent.self_modification_allowlist import ALLOWED_PATHS as _SELF_MOD_ALLOWED_PATHS
from app.agent.self_modification_allowlist import HARD_BLOCKED as _SELF_MOD_HARD_BLOCKED
from app.agent.llm_client import is_shutting_down, ShutdownError

# ---------------------------------------------------------------------------
# Per-task git working directory
# ---------------------------------------------------------------------------
# Using a ContextVar so parallel agent sessions (each in their own thread or
# asyncio task) each have an independent working directory. Never defaults to
# TheMaestro's own source tree - always requires explicit configuration per task.
_task_git_cwd: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_task_git_cwd", default=None
)

# When True, read tools reject absolute paths that resolve outside effective_root.
# Set only during the planning survey phase to prevent cross-project file reads.
_restrict_reads_to_root: contextvars.ContextVar[bool] = contextvars.ContextVar(
    "_restrict_reads_to_root", default=False
)

_task_project_name: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_task_project_name", default=None
)

# Current task_id and session_id — set by the scheduler alongside set_task_git_cwd
# so report_tool_bug can stamp reports without requiring the agent to know its own ID.
_task_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_task_id_ctx", default=None
)
_session_id_ctx: contextvars.ContextVar[int | None] = contextvars.ContextVar(
    "_session_id_ctx", default=None
)

# Current inter-agent ask depth — incremented by each nested ask_agent hop.
# Reset to 0 at the start of every root MaestroLoop session.
_ask_depth_ctx: contextvars.ContextVar[int] = contextvars.ContextVar(
    "_ask_depth_ctx", default=0
)

# ---------------------------------------------------------------------------
# Per-call output buffer — enables read_last_output slicing
# ---------------------------------------------------------------------------
# Maps task_id → last full (un-truncated) tool output, capped at 4 MiB.
# "_sync" is used for synchronous dispatch_tool calls (single-threaded callers).
_output_buffer: dict[str, str] = {}
_output_buffer_lock = threading.Lock()
_MAX_BUFFER_BYTES = 4 * 1024 * 1024  # 4 MiB per task

# Stores the raw output of the most recent test run so read_test_summary can parse it.
_last_test_output: contextvars.ContextVar[str] = contextvars.ContextVar(
    "_last_test_output", default=""
)


# ---------------------------------------------------------------------------
# consult_maestro per-session call counter
# ---------------------------------------------------------------------------
# Maps task_id → number of consult_maestro calls in the current session.
# Reset by reset_consult_count() at the start of each MaestroLoop run.
_consult_call_counts: dict[str, int] = {}
_consult_call_counts_lock = threading.Lock()


def reset_consult_count(task_id: str) -> None:
    """Reset the consult_maestro call counter for a task session."""
    with _consult_call_counts_lock:
        _consult_call_counts[task_id] = 0


def _increment_consult_count(task_id: str) -> int:
    """Increment and return the new consult call count for a task."""
    with _consult_call_counts_lock:
        count = _consult_call_counts.get(task_id, 0) + 1
        _consult_call_counts[task_id] = count
        return count


# ---------------------------------------------------------------------------
# Per-task file read-range tracking
# ---------------------------------------------------------------------------
# Maps normalised abs-path -> sorted, merged list of (start, end) inclusive
# line intervals already delivered to the LLM in this session.
# A path being present (even with an empty interval list) means read_file()
# has been called on it at least once.
#
# Maximum lines served per call - shared by both read_file.
_READ_FILE_MAX_LINES = 250

_prepped_files: contextvars.ContextVar[dict[str, list[tuple[int, int]]] | None] = (
    contextvars.ContextVar("_prepped_files", default=None)
)


def _get_prepped_files() -> dict[str, list[tuple[int, int]]]:
    d = _prepped_files.get()
    if d is None:
        d = {}
        _prepped_files.set(d)
    return d


def _mark_file_prepped(path: str) -> None:
    """Register a file as having had read_file() called (no source lines served yet)."""
    norm = os.path.normpath(os.path.realpath(path))
    _get_prepped_files().setdefault(norm, [])


def _is_file_prepped(path: str) -> bool:
    return os.path.normpath(os.path.realpath(path)) in _get_prepped_files()


def _invalidate_prepped_cache(path: str) -> None:
    """Reset served-range record for path so the next read_file returns fresh content.

    Sets the entry to an empty list (not removed) so _is_file_prepped still returns
    True — this causes read_file to skip the structural-summary phase and serve the
    new content immediately, which is what agents need after a write.
    """
    try:
        norm = os.path.normpath(os.path.realpath(path))
    except OSError:
        return
    _get_prepped_files()[norm] = []


def _record_served_range(norm_path: str, start: int, end: int) -> None:
    """Record that lines start..end (inclusive, 1-indexed) have been delivered."""
    intervals = _get_prepped_files().setdefault(norm_path, [])
    intervals.append((start, end))
    intervals.sort()
    merged: list[tuple[int, int]] = [intervals[0]]
    for s, e in intervals[1:]:
        if s <= merged[-1][1] + 1:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    _get_prepped_files()[norm_path] = merged


def _next_unserved_range(
    norm_path: str, req_start: int, req_end: int
) -> "tuple[int, int] | None":
    """Return (start, end) of the first ≤_READ_FILE_MAX_LINES unserved lines
    within [req_start, req_end], or None if the entire range is already served.
    """
    served = _get_prepped_files().get(norm_path, [])
    cursor = req_start
    for s, e in served:
        if cursor > req_end:
            break
        if cursor < s:
            # Gap before this served interval
            gap_end = min(s - 1, req_end, cursor + _READ_FILE_MAX_LINES - 1)
            return (cursor, gap_end)
        if cursor <= e:
            cursor = e + 1  # skip past served interval
    if cursor <= req_end:
        return (cursor, min(req_end, cursor + _READ_FILE_MAX_LINES - 1))
    return None


def _serve_file_lines(safe_path: str, start: int, end: int) -> str:
    """Read lines start..end from safe_path, record as served, return with header.

    Lines are 1-indexed inclusive.  Clamps to actual file length.
    """
    if _is_binary_path(safe_path):
        return f"ERROR: '{safe_path}' is a binary file and cannot be read as text."
    norm = os.path.normpath(os.path.realpath(safe_path))
    try:
        with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
            all_lines = fh.readlines()
    except OSError as exc:
        return f"ERROR reading '{safe_path}': {exc}"
    total = len(all_lines)
    start = max(1, min(start, total))
    end   = max(start, min(end, total))
    _record_served_range(norm, start, end)
    rows: list[str] = []
    has_trailing = False
    for i in range(start, end + 1):
        raw = all_lines[i - 1].rstrip('\r\n')  # strip line endings; keep trailing spaces/tabs
        stripped = raw.rstrip(' \t')
        tail = raw[len(stripped):]
        if tail:
            has_trailing = True
            n_sp = tail.count(' ')
            n_tab = tail.count('\t')
            parts = []
            if n_sp:
                parts.append(f"{n_sp}sp")
            if n_tab:
                parts.append(f"{n_tab}tab")
            rows.append(f"{i}: {stripped}  <trailing:{'+'.join(parts)}>")
        else:
            rows.append(f"{i}: {raw}")
    header = f"== FILE: {safe_path} (lines {start}-{end} of {total}) =="
    if has_trailing:
        header += (
            "\n(note: <trailing:Nsp> = N trailing spaces, <trailing:Ntab> = N trailing tabs"
            " - patch_file auto-repairs these; you do NOT need to include them in old_str)"
        )
    result = [header] + rows
    return "\n".join(result)


def _served_ranges_str(norm_path: str) -> str:
    served = _get_prepped_files().get(norm_path, [])
    return ", ".join(f"{s}-{e}" for s, e in served) if served else "none"


# Paths where git init has already been attempted this process lifetime.
# Prevents repeated init attempts if the first one fails.
_git_init_attempted: set[str] = set()


def ensure_git_repo(path: str) -> None:
    """If ``path`` has no .git directory, attempt ``git init`` once.

    Subsequent calls for the same path (including after failure) are no-ops.
    Logs the outcome but never raises - callers should proceed and let the
    subsequent git command surface any real error.
    """
    if os.path.exists(os.path.join(path, ".git")):
        return
    if path in _git_init_attempted:
        return
    _git_init_attempted.add(path)
    try:
        result = subprocess.run(
            ["git", "init", path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            logger.info("[git] Initialized new repository at %s", path)
        else:
            logger.warning(
                "[git] git init failed at %s: %s",
                path, (result.stdout + result.stderr).strip(),
            )
    except Exception as exc:
        logger.warning("[git] git init error at %s: %s", path, exc)


def set_task_git_cwd(
    path: str | None,
    task_id: str | None = None,
    session_id: int | None = None,
) -> None:
    """
    Set the git working directory for all git tool calls in the current context.

    Call this before dispatching any tools for a task, passing the filesystem
    path of the project the task belongs to. Pass None to clear the override
    (git tools will error rather than fall back to TheMaestro's own repo).

    Optionally pass task_id and session_id to stamp report_tool_bug reports.
    """
    _task_git_cwd.set(path)
    if task_id is not None:
        _task_id_ctx.set(task_id)
    if session_id is not None:
        _session_id_ctx.set(session_id)


def get_task_git_cwd() -> str | None:
    """Return the currently active task git working directory, or None."""
    return _task_git_cwd.get()


def set_task_context(task_id: str, session_id: int | None = None) -> None:
    """Set task_id and session_id for the current context (used by report_tool_bug)."""
    _task_id_ctx.set(task_id)
    _session_id_ctx.set(session_id)


# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Directory exclusions for listing tools
# ---------------------------------------------------------------------------
# Sourced from maestro.ini [tools] excluded_directories.
# Add entries there to extend - no code change required.
# The root .archive folder and .git are always excluded by absolute path
# regardless of this set.

LISTING_EXCLUDED_DIRS: set[str] = TOOL_LISTING_EXCLUDED_DIRS


def _effective_archive_dir() -> str:
    """Return the .archive directory for the currently active project root.

    Always project-local: each project keeps its own soft-delete tombstone at
    <project_root>/.archive rather than in Maestro's central directory.
    """
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    return os.path.join(effective_root, ".archive")


def _assert_safe_path(path: str) -> str:
    """
    Resolve path and ensure it doesn't touch protected system/metadata paths.

    Maestro allows agents to navigate the entire PC, but protects:
      1. .git internal directories (everywhere)
      2. The Maestro archive (.archive)

    Returns the absolute resolved path string.
    Raises ValueError if the path touches a protected location.
    """
    # Normalize mixed separators
    path = os.path.normpath(path)
    effective_root = _task_git_cwd.get() or PROJECT_ROOT

    if os.path.isabs(path):
        resolved = os.path.realpath(path)
        # Survey-phase restriction: reject absolute paths outside the project root so the
        # planning survey cannot read files from unrelated projects on the same machine.
        if _restrict_reads_to_root.get():
            root_real = os.path.realpath(effective_root)
            if not (resolved.startswith(root_real + os.sep) or resolved == root_real):
                raise ValueError(
                    f"Refusing to read outside project root {effective_root}; "
                    "use a relative path instead."
                )
    else:
        resolved = os.path.realpath(os.path.join(effective_root, path))

    # RC4 strict isolation: when a task root is set, block reads outside it
    task_root = _task_git_cwd.get()
    if task_root:
        task_root_real = os.path.realpath(task_root)
        if not (resolved == task_root_real or
                resolved.startswith(task_root_real + os.sep)):
            raise ValueError(
                f"Strict Isolation violation: cannot read '{path}' "
                f"outside task root '{task_root}'"
            )

    # Protect .git directories everywhere
    # Case-insensitive check on Windows for safety.
    norm_path = resolved.lower()
    if "\\.git\\" in norm_path or "/.git/" in norm_path or norm_path.endswith("\\.git") or norm_path.endswith("/.git"):
        logger.warning("Blocked access to git internal path: %s", resolved)
        raise ValueError(f"HARD REJECTION: '{path}' touches a .git directory. Access to git internals is blocked.")

    # Protect .archive directories (both Maestro's central archive and the effective
    # project's local archive).  Agents must never read inside these; they are
    # managed exclusively by archive_file / move_file / patch_file.
    for _archive_candidate in (ARCHIVE_DIR, _effective_archive_dir()):
        archive_abs = os.path.realpath(_archive_candidate).lower()
        if norm_path == archive_abs or norm_path.startswith(archive_abs + os.sep):
            logger.warning("Blocked access to archive directory: %s", resolved)
            raise ValueError(f"REJECTION: '{path}' is inside an archive directory. This folder is managed by the system.")

    return resolved


def _assert_archivable(path: str) -> str:
    """
    Extended safety check used exclusively by archive_file.

    Rules (in priority order):
      1. Path must NOT touch .git or anything inside it.
      2. Path must NOT be inside the root archive directory (ARCHIVE_DIR).

    Returns the resolved absolute path on success.
    Raises ValueError with a descriptive message on any violation.
    """
    safe = _assert_safe_path(path)
    archive_root = os.path.realpath(_effective_archive_dir())

    # Note: _assert_safe_path already protects .git and .archive internals.
    # We only need to check if the path IS the archive root itself here.
    if safe == archive_root:
        raise ValueError("HARD REJECTION: Cannot archive the archive root itself.")

    return safe


# Segments that are never valid write targets even when inside the project root.
# Dependency dirs, build artefacts, and VCS metadata are all off-limits for writes.
_WRITE_BLOCKED_SEGMENTS: frozenset[str] = frozenset({
    "venv", ".venv", "__pycache__", "node_modules",
    ".mypy_cache", ".pytest_cache", ".ruff_cache",
    "dist", "build", ".eggs", ".tox", "site-packages",
})


def _assert_on_allowlist(resolved: str) -> None:
    """Validate a resolved path against the self-modification allowlist."""
    # Normalize case for cross-platform comparison (critical on Windows).
    nc = os.path.normcase(resolved)
    hard_blocked_nc = {os.path.normcase(p) for p in _SELF_MOD_HARD_BLOCKED}
    allowed_nc = {os.path.normcase(p) for p in _SELF_MOD_ALLOWED_PATHS}
    if nc in hard_blocked_nc:
        raise ValueError(
            f"WRITE REJECTED: '{resolved}' is permanently off-limits for self-modification. "
            "This path cannot be modified by agents even with all toggles enabled."
        )
    if nc not in allowed_nc:
        raise ValueError(
            f"WRITE REJECTED: '{resolved}' is not on the self-modification allowlist. "
            "Add it to app/agent/self_modification_allowlist.py ALLOWED_PATHS to permit this write."
        )


def _assert_safe_write_path(path: str) -> str:
    """
    Safety check for all write operations (write_file, append_file).

    Layer 0 — inherited from _assert_safe_path (called first):
      - .git directories everywhere: HARD REJECTED.  Writing into a .git dir
        would corrupt the repository.
      - .archive directory: HARD REJECTED.  Maestro's soft-delete tombstone
        must not be written to directly.

    Additional write-specific layers:
      1. The resolved path must be inside the effective project root.
         Agents may READ anywhere on the PC, but writes are confined to the
         project they are working on.
      2. The path must not pass through any dependency/build/VCS segment
         (_WRITE_BLOCKED_SEGMENTS) — those dirs must never be overwritten.
      3. Gitignored paths are refused to prevent accidental writes to
         secrets, data files, or other protected content.

    Self-modification exemption: _maestro_self project with can_self_modify=true
    may write to the Maestro source tree, but only to allowlisted paths.

    Returns the resolved absolute path.
    Raises ValueError with a descriptive message on any violation.
    """
    resolved = _assert_safe_path(path)   # Layer 0: blocks .git + .archive
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    root = os.path.realpath(effective_root)

    # Self-modification exemption: _maestro_self project with can_self_modify enabled
    # may write to the Maestro source tree, but only to allowlisted paths.
    # Normcase comparison is required on Windows (MAESTRO_GIT_ROOT is normcased).
    if MAESTRO_GIT_ROOT and os.path.normcase(resolved).startswith(MAESTRO_GIT_ROOT + os.sep):
        project_name = _task_project_name.get() or ""
        if project_name == SELF_MODIFICATION_PROJECT and MAESTRO_CAPABILITIES.can_self_modify:
            _assert_on_allowlist(resolved)
            return resolved
        raise ValueError(
            f"WRITE REJECTED: '{path}' is inside the Maestro source tree. "
            f"Only the '{SELF_MODIFICATION_PROJECT}' project with can_self_modify=true "
            "may write here, and only to allowlisted paths."
        )

    # 1. Containment: writes must stay inside the project root
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise ValueError(
            f"WRITE REJECTED: '{path}' resolves to '{resolved}' which is outside "
            f"the project root '{root}'. Writes are restricted to the project directory."
        )

    # 2. Segment blocklist
    rel = os.path.relpath(resolved, root)
    for seg in rel.replace("\\", "/").split("/"):
        if seg in _WRITE_BLOCKED_SEGMENTS:
            raise ValueError(
                f"WRITE REJECTED: '{path}' passes through '{seg}' — "
                "dependency/build directories cannot be written to."
            )

    # 3. Gitignored paths — refuse writes (protect secrets, data, generated files)
    if _is_gitignored(resolved):
        raise ValueError(
            f"WRITE REJECTED: '{path}' is listed in .gitignore. "
            "Writing to gitignored paths is blocked to protect secrets and data files. "
            "If this is intentional, remove the path from .gitignore first."
        )

    return resolved


def _find_archived_copies(rel_path: str, archive_dir: str | None = None) -> list[str]:
    """
    Scan the project-local archive for all previously archived copies of rel_path.
    rel_path must be relative to the effective project root.
    Returns a list of absolute paths ordered most-recent first.
    """
    archive_root = os.path.realpath(archive_dir or _effective_archive_dir())
    if not os.path.isdir(archive_root):
        return []
    found: list[str] = []
    try:
        ts_dirs = sorted(os.listdir(archive_root), reverse=True)
    except OSError:
        return []
    for ts_dir in ts_dirs:
        candidate = os.path.join(archive_root, ts_dir, rel_path)
        if os.path.exists(candidate):
            found.append(candidate)
    return found




# ---------------------------------------------------------------------------
# Gitignore guard
# ---------------------------------------------------------------------------

def _is_gitignored(abs_path: str) -> bool:
    """Return True if abs_path is git-ignored. Uses centralized PathFilter."""
    from app.agent.path_filter import is_ignored
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    return is_ignored(abs_path, effective_root)


# ---------------------------------------------------------------------------
# Tool result safety guards
# ---------------------------------------------------------------------------

# Maximum characters allowed in a single tool result.  Anything larger is
# almost certainly a binary file, a huge directory listing, or a runaway
# recursive glob — all of which would overflow the LLM's context window.
_MAX_TOOL_RESULT_CHARS = 200_000


def _cap_tool_result(name: str, result: str, *, task_id: str = "_sync") -> str:
    """Truncate oversized tool results and store full output in the per-task buffer."""
    with _output_buffer_lock:
        _output_buffer[task_id] = result[:_MAX_BUFFER_BYTES]
    if len(result) <= _MAX_TOOL_RESULT_CHARS:
        return result
    truncated = result[:_MAX_TOOL_RESULT_CHARS]
    cut = truncated.rfind("\n")
    if cut > _MAX_TOOL_RESULT_CHARS // 2:
        truncated = truncated[:cut]
    total_lines = result.count("\n") + 1
    shown_lines = truncated.count("\n") + 1
    next_offset = shown_lines + 1
    logger.warning(
        "Tool '%s' returned %d chars — truncating to %d to prevent context overflow.",
        name, len(result), len(truncated),
    )
    footer = (
        f"\n\n[TRUNCATED]\n"
        f"total_chars={len(result):,}  shown_chars={len(truncated):,}\n"
        f"total_lines={total_lines:,}    shown_lines={shown_lines:,}\n"
        f"next_offset_lines={next_offset}\n"
        f'hint="Use read_last_output(offset={next_offset}, limit=500) or read_last_output(grep=\'pattern\')"'
    )
    return truncated + footer


def _is_binary_path(abs_path: str) -> bool:
    """Return True if the file appears to be binary (null bytes in first 512 bytes)."""
    try:
        with open(abs_path, "rb") as fh:
            return b"\x00" in fh.read(512)
    except OSError:
        return False


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def read_file(
    path: str,
    start: int | None = None,
    end: int | None = None,
    count: int | None = None,
) -> str:
    """Read a file's structure or source code. Capped at 250 lines per call.

    FIRST CALL (no range): Returns a structural summary (classes, functions, imports).
    SUBSEQUENT CALLS (no range): Serves the NEXT 250 unserved lines automatically.
    TARGETED READ: Provide 'start' (+ optionally 'end' or 'count') to read a specific range.

    Small files (≤ 25 lines) are always shown in full on the first call.
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    if _is_binary_path(safe_path):
        return (
            f"ERROR: '{path}' is a binary file (contains null bytes) and cannot be read as text. "
            "Binary files must be inspected with appropriate non-text tools."
        )

    norm = os.path.normpath(os.path.realpath(safe_path))
    from app.agent.project_snapshot import _count_file_lines, build_file_summary
    total = _count_file_lines(safe_path)

    # 1. Structural Summary Phase
    # If this is the first time seeing the file and NO specific range was requested,
    # show the summary (or the whole file if it is tiny).
    if not _is_file_prepped(safe_path) and start is None and end is None and count is None:
        _mark_file_prepped(safe_path)
        if total <= 25:
            result = _inline_small_file(safe_path)
            _record_served_range(norm, 1, total)
            return result
        return build_file_summary(safe_path)

    # 2. Source Reading Phase
    # Ensure file is marked as prepped if a range was requested immediately
    if not _is_file_prepped(safe_path):
        _mark_file_prepped(safe_path)

    if end is not None and count is not None:
        return "ERROR: provide 'end' OR 'count', not both."

    if start is None:
        # Default: first ≤250 unserved lines
        unserved = _next_unserved_range(norm, 1, total)
        if unserved is None:
            rel = os.path.relpath(safe_path, PROJECT_ROOT)
            return (
                f"ALREADY IN CONTEXT: all lines of '{rel}' have been served "
                f"(ranges: {_served_ranges_str(norm)}). "
                f"To re-read a range, call read_file('{rel}', start=N)."
            )
        req_start, req_end = unserved
    else:
        req_start = start
        if count is not None:
            req_end = start + count - 1
        elif end is not None:
            req_end = end
        else:
            req_end = start + _READ_FILE_MAX_LINES - 1

    # Clamp to actual file bounds
    req_start = max(1, min(req_start, total))
    req_end = max(req_start, min(req_end, total))

    # Capped at 250 lines per call
    if req_end - req_start >= _READ_FILE_MAX_LINES:
        req_end = req_start + _READ_FILE_MAX_LINES - 1

    # Check if this exact range is already served
    already_served = False
    served_intervals = _get_prepped_files().get(norm, [])
    for s, e in served_intervals:
        if s <= req_start and e >= req_end:
            already_served = True
            break

    result = _serve_file_lines(safe_path, req_start, req_end)
    if already_served:
        header = f"(NOTE: lines {req_start}-{req_end} were already in context; repeating per request)\n"
        return header + result

    return result


_SMALL_FILE_HEADER = "== FILE (full content): {path} =="


def _inline_small_file(abs_path: str) -> str:
    """Return raw content for tiny files (≤ 25 lines), with a header."""
    if _is_binary_path(abs_path):
        return f"ERROR: '{abs_path}' is a binary file and cannot be inlined as text."
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        header = f"== FILE (full content): {abs_path} =="
        return f"{header}\n{content}"
    except OSError as exc:
        return f"ERROR reading '{abs_path}': {exc}"


def write_file(path: str, content: str) -> str:
    """[WRITE — files] Overwrite a file with the given content. Auto-stages for git. If the file already exists, a pre-overwrite copy is archived to the project's .archive/. Path must be inside project root."""
    try:
        safe_path = _assert_safe_write_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if os.path.exists(safe_path) and _is_binary_path(safe_path):
        return f"ERROR: '{path}' is a binary file; refusing to overwrite with text content."

    archived_msg = ""
    if os.path.isfile(safe_path):
        effective_root = _task_git_cwd.get() or PROJECT_ROOT
        rel_path = os.path.relpath(os.path.realpath(safe_path), os.path.realpath(effective_root))
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        archive_dest = os.path.join(_effective_archive_dir(), timestamp, rel_path)
        try:
            os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
            shutil.copy2(safe_path, archive_dest)
            archived_msg = f" Pre-overwrite copy archived to '{archive_dest}'."
        except OSError as exc:
            return f"ERROR: could not archive pre-overwrite copy of '{path}': {exc}"

    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        _invalidate_prepped_cache(safe_path)
        rc, out, err = _git_run(["git", "add", safe_path])
        git_msg = " and staged for git."
        if rc != 0:
            git_msg = f" but STAGING FAILED: {err or out}"
            
        line_count = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
        status = f"OK: wrote {line_count} lines to '{path}'{git_msg}{archived_msg}"
        if line_count <= 250:
            status += f"\n\n== NEW CONTENT: {path} ==\n{content}"
            if content and not content.endswith('\n'):
                status += "\n"
            status += "== END =="
        else:
            # Show first and last 20 lines so the agent can verify both ends.
            lines = content.split('\n')
            head = '\n'.join(f"{i+1}: {l}" for i, l in enumerate(lines[:20]))
            tail_start = max(20, line_count - 20)
            tail = '\n'.join(f"{i+1}: {l}" for i, l in enumerate(lines[tail_start:], start=tail_start + 1))
            status += f"\n(file has {line_count} lines — showing first 20 and last 20)\n{head}\n...\n{tail}"
        return status
    except OSError as exc:
        return f"ERROR writing '{path}': {exc}"


def append_file(path: str, content: str) -> str:
    """[WRITE — files] Append text to the end of a file (creates it if absent). Auto-stages for git. Path must be inside project root."""
    try:
        safe_path = _assert_safe_write_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if os.path.exists(safe_path) and _is_binary_path(safe_path):
        return f"ERROR: '{path}' is a binary file; refusing to append text content."
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "a", encoding="utf-8") as fh:
            fh.write(content)
        _invalidate_prepped_cache(safe_path)
        _git_run(["git", "add", safe_path])
        line_count = content.count('\n') + (1 if content and not content.endswith('\n') else 0)
        status = f"OK: appended {line_count} lines to '{path}'."
        if line_count <= 250:
            status += f"\n\n== APPENDED CONTENT ==\n{content}"
            if content and not content.endswith('\n'):
                status += "\n"
            status += "== END =="
        else:
            preview = '\n'.join(f"{i+1}: {l}" for i, l in enumerate(content.split('\n')[:10]))
            status += f"\n(first 10 of {line_count} lines)\n{preview}\n..."
        return status
    except OSError as exc:
        return f"ERROR appending to '{path}': {exc}"


def _find_old_str_lax(text: str, old_str: str) -> "tuple[int, int] | None":
    """Line-by-line match ignoring trailing whitespace.

    Returns (char_start, char_end) of the matched block in *text*, or None if
    the block is not found exactly once (0 or 2+ matches → None).
    """
    text_lines = text.splitlines(keepends=True)
    old_lines = old_str.splitlines(keepends=True) or [old_str]
    n = len(old_lines)
    old_stripped = [l.rstrip() for l in old_lines]
    matches: list[int] = []
    for i in range(max(1, len(text_lines) - n + 1 + 1)):  # iterate all valid start positions
        if i + n > len(text_lines):
            break
        if [l.rstrip() for l in text_lines[i : i + n]] == old_stripped:
            matches.append(i)
    if len(matches) != 1:
        return None
    i = matches[0]
    char_start = sum(len(text_lines[j]) for j in range(i))
    char_end = char_start + sum(len(text_lines[i + j]) for j in range(n))
    return char_start, char_end


def _find_old_str_indent_lax(text: str, old_str: str) -> "tuple[int, int] | None":
    """Line-by-line match ignoring ALL leading/trailing whitespace per line.

    Last-resort fallback for indentation mismatches (wrong indent level, tabs vs
    spaces, mixed indent).  Returns (char_start, char_end) in *text*, or None if
    not found exactly once.  Rejects all-blank old_str to avoid spurious matches.
    """
    text_lines = text.splitlines(keepends=True)
    old_lines = old_str.splitlines(keepends=True) or [old_str]
    n = len(old_lines)
    old_stripped = [l.strip() for l in old_lines]
    if not any(old_stripped):
        return None
    matches: list[int] = []
    for i in range(max(1, len(text_lines) - n + 1)):
        if i + n > len(text_lines):
            break
        if [l.strip() for l in text_lines[i : i + n]] == old_stripped:
            matches.append(i)
    if len(matches) != 1:
        return None
    i = matches[0]
    char_start = sum(len(text_lines[j]) for j in range(i))
    char_end = char_start + sum(len(text_lines[i + j]) for j in range(n))
    return char_start, char_end


def _apply_patch_from_span(
    safe_path: str, path: str, original: str,
    char_start: int, char_end: int, new_str: str,
    repair_note: str = "",
) -> str:
    """Archive pre-patch copy, apply replacement at char_start:char_end, stage, return message."""
    start_line = original[:char_start].count("\n") + 1
    old_chunk = original[char_start:char_end]
    end_line = start_line + old_chunk.count("\n")
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    rel_path = os.path.relpath(os.path.realpath(safe_path), os.path.realpath(effective_root))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    archive_dest = os.path.join(_effective_archive_dir(), timestamp, rel_path)
    try:
        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        shutil.copy2(safe_path, archive_dest)
    except OSError as exc:
        return f"ERROR: could not archive pre-patch copy of '{path}': {exc}"
    patched = original[:char_start] + new_str + original[char_end:]
    try:
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(patched)
    except OSError as exc:
        return f"ERROR writing '{path}': {exc}"
    _invalidate_prepped_cache(safe_path)
    _git_run(["git", "add", safe_path])
    lines_changed = new_str.count("\n") - old_chunk.count("\n")
    new_end_line = start_line + new_str.count("\n")
    note = f" [{repair_note}]" if repair_note else ""
    msg = (
        f"OK: patched '{path}' (lines {start_line}-{end_line} -> {start_line}-{new_end_line}, "
        f"net {lines_changed:+d} lines){note}. Staged for git."
    )
    patched_lines = patched.splitlines()
    ctx_start = max(0, start_line - 2)
    ctx_end = min(len(patched_lines), new_end_line + 2)
    if ctx_end - ctx_start <= 250:
        section = '\n'.join(
            f"{ctx_start + i + 1}: {patched_lines[ctx_start + i]}" for i in range(ctx_end - ctx_start)
        )
        msg += f"\n\n== PATCHED SECTION (lines {ctx_start+1}-{ctx_end}) ==\n{section}\n== END =="
    else:
        msg += f"\n(section spans {ctx_end - ctx_start} lines — too large to show inline)"
    return msg


def _vis_ws(s: str) -> str:
    """Render non-obvious whitespace visibly (ASCII-safe) for diagnostics."""
    return s.replace('\r', '[CR]').replace('\t', '[TAB]')


def patch_file(path: str, old_str: str, new_str: str) -> str:
    """[WRITE — files] Replace an exact string in a file. old_str must appear exactly once.
    Auto-stages for git. Path must be inside project root.
    Use this instead of write_file when making targeted edits — avoids full-file rewrites.
    Auto-repairs trailing whitespace differences, extra blank lines around old_str, and
    indentation mismatches when an exact string match is not found.
    """
    try:
        safe_path = _assert_safe_write_path(path)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' does not exist. Use write_file to create new files."
    if _is_binary_path(safe_path):
        return f"ERROR: '{path}' is a binary file; cannot patch."
    try:
        with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
            original = fh.read()
    except OSError as exc:
        return f"ERROR reading '{path}': {exc}"

    # Normalize CRLF in old_str — Python text-mode reads already strip \r from the
    # file, so a mismatch is guaranteed if the LLM emitted \r\n in its JSON output.
    old_str = old_str.replace('\r\n', '\n').replace('\r', '\n')

    count = original.count(old_str)
    if count == 0:
        # --- Lax matching cascade (most to least specific) ---

        # Level 1: trailing whitespace — handles lines with extra trailing spaces/tabs.
        lax = _find_old_str_lax(original, old_str)
        if lax is not None:
            return _apply_patch_from_span(safe_path, path, original, lax[0], lax[1], new_str,
                                          "auto-repaired trailing whitespace")

        # Level 2: blank-line trimming — handles extra leading/trailing newlines in old_str.
        old_str_trimmed = old_str.strip('\n')
        if old_str_trimmed and old_str_trimmed != old_str:
            count_t = original.count(old_str_trimmed)
            if count_t == 1:
                off = original.index(old_str_trimmed)
                return _apply_patch_from_span(safe_path, path, original, off,
                                              off + len(old_str_trimmed), new_str,
                                              "auto-trimmed blank lines from old_str")
            if count_t == 0:
                lax2 = _find_old_str_lax(original, old_str_trimmed)
                if lax2 is not None:
                    return _apply_patch_from_span(safe_path, path, original, lax2[0], lax2[1], new_str,
                                                  "auto-trimmed blank lines + repaired trailing whitespace")
        else:
            old_str_trimmed = old_str  # keep reference for level 3

        # Level 3: indentation-lax — strips all leading/trailing whitespace per line.
        indent_lax = _find_old_str_indent_lax(original, old_str)
        if indent_lax is not None:
            return _apply_patch_from_span(safe_path, path, original, indent_lax[0], indent_lax[1], new_str,
                                          "auto-repaired indentation")
        if old_str_trimmed != old_str:
            indent_lax2 = _find_old_str_indent_lax(original, old_str_trimmed)
            if indent_lax2 is not None:
                return _apply_patch_from_span(safe_path, path, original, indent_lax2[0], indent_lax2[1], new_str,
                                              "auto-trimmed blank lines + repaired indentation")

        # Nothing matched — build diagnostic for the agent.
        msg = [f"ERROR: old_str not found in '{path}'."]
        import re
        def _canonical(s: str) -> str: return re.sub(r"\s+", "", s)
        if _canonical(old_str) in _canonical(original):
            msg.append("HINT: The text exists but whitespace/indentation does not match exactly.")
            old_has_tabs = "\t" in old_str
            file_has_tabs = "\t" in original
            if old_has_tabs != file_has_tabs:
                msg.append(
                    f"DIAGNOSTIC: Your old_str {'has' if old_has_tabs else 'lacks'} tabs, "
                    f"but the file {'has' if file_has_tabs else 'lacks'} them."
                )
            lines = original.splitlines()
            old_lines = old_str.splitlines()
            if old_lines:
                first_line_clean = old_lines[0].strip()
                for lineno, line in enumerate(lines, 1):
                    if first_line_clean and first_line_clean in line:
                        file_lead = len(line) - len(line.lstrip())
                        old_lead = len(old_lines[0]) - len(old_lines[0].lstrip())
                        msg.append(f"DIAGNOSTIC: Found similar text at line {lineno}:")
                        msg.append(f"  FILE ({file_lead} leading chars): '{_vis_ws(line)}'")
                        msg.append(f"  YOUR ({old_lead} leading chars): '{_vis_ws(old_lines[0])}'")
                        msg.append("(legend: ·=space →=tab ↵=carriage-return)")
                        if file_lead != old_lead:
                            msg.append(
                                f"  FIX: adjust leading whitespace in old_str "
                                f"from {old_lead} to {file_lead} characters."
                            )
                        break
        else:
            msg.append(
                "HINT: Text not found even after ignoring all whitespace. "
                "Call read_file() again to get the exact current content."
            )
        msg.append(
            "ACTION: re-read the affected lines with read_file(start=N, end=M), "
            "then copy the lines verbatim into old_str."
        )
        return "\n".join(msg)
    if count > 1:
        return (
            f"ERROR: old_str appears {count} times in '{path}' — patch is ambiguous. "
            "Extend old_str to include more surrounding context so it matches exactly once."
        )
    # Exact match — apply directly.
    char_offset = original.index(old_str)
    return _apply_patch_from_span(safe_path, path, original, char_offset,
                                  char_offset + len(old_str), new_str)


def _get_cached_summary_for_listing(abs_path: str) -> "str | None":
    """Sync DB lookup for a file's cached summary.

    Prefers short_summary (exactly 2 sentences, purpose-built for listings) and
    falls back to the first line of summary for rows that pre-date migration 0035.
    Truncates at 500 chars to preserve both sentences without cutting mid-sentence
    while still bounding output for pathologically long summaries.
    """
    try:
        from app.database import get_file_summary_by_path
        row = get_file_summary_by_path(abs_path)
        if row:
            text = (getattr(row, 'short_summary', None) or "").strip() \
                or (row.summary or "").split("\n")[0].strip()
            if text:
                return (text[:500] + "...") if len(text) > 500 else text
    except Exception as exc:
        logger.debug("summary lookup failed for %s: %s", abs_path, exc)
    return None


def list_directory(path: str = ".", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] List files and directories at the given path. head/tail/grep filter output. No state change.

    Maestro allows navigation of the entire PC. Files are shown with their
    cached summary if available. Entries are annotated but never hidden (except
    .archive which is a Maestro-internal tombstone):
      - .git/:       [PROTECTED - git internals; use git tools, no direct writes]
      - venv/, __pycache__/, etc.:
                     [AUTO-EXCLUDED - skipped by agent tools and summarization]
      - gitignored:  [GITIGNORED - excluded from auto-summarization; read_file access OK]
      - symlink escaping project: [PROTECTED - symlink escapes project]
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isdir(safe_path):
        return f"ERROR: '{path}' is not a directory."

    from app.agent.path_filter import get_ignored_paths
    from app.agent.project_snapshot import _is_symlink_escaping

    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    root_real = os.path.realpath(effective_root)
    archive_real = os.path.realpath(_effective_archive_dir())

    try:
        entries = sorted(os.listdir(safe_path))
    except OSError as exc:
        return f"ERROR: Cannot read directory '{path}': {exc}"

    # Batch gitignore check for all entries at once (single subprocess call)
    all_full = [os.path.join(safe_path, e) for e in entries]
    try:
        gitignored_set = get_ignored_paths(all_full, effective_root)
    except Exception:
        gitignored_set = set()

    lines: list[str] = []

    for entry, full in zip(entries, all_full):
        full_real = os.path.realpath(full)

        is_dir = os.path.isdir(full)
        kind = "DIR " if is_dir else "FILE"
        suffix = "/" if is_dir else ""

        # 1. .archive — shown as RESERVED; contents are not listable (_assert_safe_path
        #    blocks any path inside .archive so list_directory(".archive/...") will fail).
        if full_real == archive_real:
            lines.append(f"{kind}  {entry}/  [RESERVED - Maestro soft-delete archive; contents not accessible]")
            continue

        # 2. .git — shown but hard-protected; listing inside .git is also blocked by
        #    _assert_safe_path so list_directory(".git/objects") will fail.
        if entry.lower() == ".git" and is_dir:
            lines.append(f"{kind}  {entry}/  [PROTECTED - git internals; contents not accessible, use git tools]")
            continue

        # 3. Symlinks escaping the project root
        if _is_symlink_escaping(full, root_real):
            target = os.readlink(full) if os.path.islink(full) else "?"
            lines.append(f"{kind}  {entry}{suffix} -> {target}  [PROTECTED - symlink escapes project]")
            continue

        # 4. Built-in excluded dirs (venv, __pycache__, node_modules, etc.)
        #    Check by name — these are shown but flagged as auto-excluded.
        #    Agent CAN read inside them if truly needed, but tools skip them automatically.
        if is_dir and entry in TOOL_LISTING_EXCLUDED_DIRS:
            lines.append(f"{kind}  {entry}/  [AUTO-EXCLUDED - skipped by agent tools and summarization]")
            continue

        # Hidden files/dirs (except a few explicit allowances)
        if entry.startswith(".") and entry not in (".env.example", ".gitignore", ".geminiignore"):
            lines.append(f"{kind}  {entry}{suffix}  [AUTO-EXCLUDED - hidden file/dir]")
            continue

        # 5. Gitignored — shown but labelled so agent knows automatic processing is skipped.
        #    Direct read_file / search_files calls are still allowed on these paths.
        if full in gitignored_set:
            lines.append(
                f"{kind}  {entry}{suffix}  [GITIGNORED - excluded from auto-summarization; "
                "read_file/search_files access is allowed]"
            )
            continue

        # 6. Normal entries
        if is_dir:
            lines.append(f"{kind}  {entry}/")
        else:
            summary = _get_cached_summary_for_listing(full)
            if summary:
                lines.append(f"{kind}  {entry}  - {summary}")
            else:
                lines.append(f"{kind}  {entry}  - (SUMMARY NOT AVAILABLE)")

    return _slice_output("\n".join(lines) if lines else "(empty directory)", head=head, tail=tail, grep=grep)


def find_in_files(
    pattern: str,
    directory: str = ".",
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[READ] Ripgrep-style content search. Returns matches in 'file:line_number: content' format (up to 200 results). head/tail/grep filter output. No state change."""
    safe_dir = _assert_safe_path(directory)
    results: list[str] = []

    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"ERROR: Invalid regex pattern: {exc}"

    from app.agent.path_filter import walk_safe
    from app.agent.config import PROJECT_ROOT

    effective_root = _task_git_cwd.get() or PROJECT_ROOT

    for root, dirs, files in walk_safe(safe_dir):
        for fname in files:
            fpath = os.path.join(root, fname)
            if _is_binary_path(fpath):
                continue
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if compiled.search(line):
                            rel = os.path.relpath(fpath, safe_dir)
                            results.append(f"{rel}:{lineno}: {line.rstrip()}")
                            if len(results) >= TOOL_MAX_SEARCH_RESULTS:
                                results.append(f"... (truncated at {TOOL_MAX_SEARCH_RESULTS} results)")
                                raw = "\n".join(results)
                                return _slice_output(raw, head=head, tail=tail, grep=grep)
            except OSError:
                continue

    raw = "\n".join(results) if results else "No matches found."
    return _slice_output(raw, head=head, tail=tail, grep=grep)






def read_file_metadata(path: str) -> str:
    """[READ] File size, modification time, sha256, line count, and binary flag. No state change."""
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    try:
        stat = Path(safe_path).stat()
        byte_size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
        is_bin = _is_binary_path(safe_path)
        if is_bin:
            sha = "n/a (binary)"
            line_count = "n/a (binary)"
        else:
            with open(safe_path, "rb") as fh:
                data = fh.read()
            sha = hashlib.sha256(data).hexdigest()
            line_count = data.count(b"\n") + (1 if data else 0)
        return (
            f"path={safe_path}\n"
            f"size={byte_size} bytes\n"
            f"mtime={mtime}\n"
            f"sha256={sha}\n"
            f"lines={line_count}\n"
            f"binary={'yes' if is_bin else 'no'}"
        )
    except OSError as exc:
        return f"ERROR reading metadata for '{path}': {exc}"


def find_files(glob_pattern: str, directory: str = ".", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """
    Find files matching a glob pattern under directory.
    Returns one path per line (up to 200 results). head/tail/grep filter output.
    """
    safe_dir = _assert_safe_path(directory)
    full_pattern = os.path.join(safe_dir, "**", glob_pattern)
    matches = _glob.glob(full_pattern, recursive=True)
    _archive_real = os.path.realpath(_effective_archive_dir())
    # Filter out excluded directories and the root archive tree
    filtered = [
        m for m in matches
        if not any(part in LISTING_EXCLUDED_DIRS for part in m.split(os.sep))
        and not os.path.realpath(m).startswith(_archive_real + os.sep)
        and os.path.realpath(m) != _archive_real
    ]
    if not filtered:
        return "No files found matching the pattern."
    lines = [os.path.relpath(m, safe_dir) for m in sorted(filtered)[:TOOL_MAX_SEARCH_RESULTS]]
    return _slice_output("\n".join(lines), head=head, tail=tail, grep=grep)


# ---------------------------------------------------------------------------
# Safe archive (never hard-delete)
# ---------------------------------------------------------------------------

def write_archive(path: str, reason: str = "") -> str:
    """[WRITE — archive] Safely 'delete' a file by moving it to .archive/<timestamp>/. NEVER hard-deletes. Reversible: copy from the archive path shown in the return value.

    Safety guarantees:
    - NEVER calls shutil.rmtree, os.remove, os.unlink, or any destructive primitive.
    - HARD REJECTS paths inside .git - the repository must never be touched.
    - HARD REJECTS paths already inside ARCHIVE_DIR - cannot re-archive.
    - HARD REJECTS paths outside PROJECT_ROOT - no cross-project accidents.

    Undelete support:
    - If the target path does not exist but was previously archived, returns
      the archived location(s) and exact restore instructions.

    Returns the archive destination path and restore instructions on success.
    """
    try:
        safe_path = _assert_archivable(path)
    except ValueError as exc:
        return f"BLOCKED: {exc}"

    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    root_real = os.path.realpath(effective_root)
    rel_path = os.path.relpath(safe_path, root_real)
    local_archive_dir = _effective_archive_dir()

    if not os.path.exists(safe_path):
        # Check if this path was previously archived - emit undelete guide
        archived = _find_archived_copies(rel_path, local_archive_dir)
        if archived:
            lines = [
                f"ERROR: '{path}' does not exist - it was previously archived.",
                "",
                "Archived copies found (most recent first):",
            ]
            for loc in archived:
                lines.append(f"  {loc}")
            lines += [
                "",
                "To restore the most recent copy run:",
                f'  python -c "import shutil; shutil.copy(r\\"{archived[0]}\\", r\\"{safe_path}\\")"',
                "",
                "To restore a directory tree (if a folder was archived) run:",
                f'  python -c "import shutil; shutil.copytree(r\\"{archived[0]}\\", r\\"{safe_path}\\")"',
            ]
            return "\n".join(lines)
        return f"ERROR: '{path}' does not exist - nothing to archive."

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    dest = os.path.join(local_archive_dir, timestamp, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    try:
        shutil.move(safe_path, dest)
    except OSError as exc:
        logger.error("archive_file: failed to move %s -> %s: %s", safe_path, dest, exc)
        return f"ERROR: could not archive '{path}': {exc}"
    logger.info("Archived %s -> %s", safe_path, dest)

    if reason:
        reason_file = dest + "._reason.txt"
        with open(reason_file, "w", encoding="utf-8") as fh:
            fh.write(f"Archived at: {timestamp}\nReason: {reason}\n")

    # Create a DB record so the file can be found and restored via the API.
    archive_id_msg = ""
    task_id = _task_id_ctx.get()
    if task_id:
        try:
            from app.database import create_archived_file as _caf
            from app.database import get_project_path as _gpp
            project_name = _task_project_name.get()
            project_root = (_gpp(project_name) if project_name else None) or effective_root
            original_rel = os.path.relpath(safe_path, project_root)
            archive_rel = os.path.relpath(dest, project_root)
            record = _caf(task_id, original_rel, archive_rel)
            archive_id_msg = f"\narchive_id={record.id} (use POST /api/tasks/{task_id}/undelete to restore)"
        except Exception as exc:
            logger.warning("write_archive: failed to create DB record: %s", exc)

    return (
        f"OK: archived '{path}' -> '{dest}'.{archive_id_msg}\n"
        f"Restore by copying: shutil.copy(r'{dest}', r'{safe_path}')"
    )


def workspace_delete_file(path: str, reason: str = "") -> str:
    """[WRITE — safe delete] Move a file to .archive/ and create a recovery record. Returns archive_id."""
    from app.agent import workspace as _ws
    task_id = _task_id_ctx.get()
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    project_name = _task_project_name.get() or ""
    try:
        from app.database import get_project_path as _gpp
        project_root = (_gpp(project_name) if project_name else None) or effective_root
    except Exception:
        project_root = effective_root
    try:
        record = _ws.delete_file(
            task_id=task_id or "unknown",
            path=path,
            effective_root=effective_root,
            project_root=project_root,
        )
        return json.dumps({
            "archive_id": record.id,
            "archive_path": record.archive_path,
            "message": f"File archived (id={record.id}). Use POST /api/tasks/{task_id}/undelete to restore.",
        })
    except Exception as exc:
        return f"ERROR: {exc}"


def workspace_rename_file(src: str, dst: str) -> str:
    """[WRITE — rename] Rename src to dst within the current worktree. Fails if dst already exists."""
    from app.agent import workspace as _ws
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    try:
        _ws.rename_file(src=src, dst=dst, effective_root=effective_root)
        return json.dumps({"ok": True, "src": src, "dst": dst})
    except FileExistsError as exc:
        return f"ERROR: destination already exists — {exc}"
    except Exception as exc:
        return f"ERROR: {exc}"


def move_file(src: str, dst: str) -> str:
    """[WRITE — files] Move or rename a file within the project.
    If dst already exists, a copy is archived to .archive/ before being overwritten.
    Auto-stages both paths for git. Path must be inside project root.
    """
    try:
        safe_src = _assert_safe_write_path(src)
    except ValueError as exc:
        return f"ERROR: {exc}"
    if not os.path.isfile(safe_src):
        return f"ERROR: source '{src}' does not exist or is not a file."
    try:
        safe_dst = _assert_safe_write_path(dst)
    except ValueError as exc:
        return f"ERROR: {exc}"

    archived_msg = ""
    if os.path.exists(safe_dst):
        effective_root = _task_git_cwd.get() or PROJECT_ROOT
        rel_dst = os.path.relpath(os.path.realpath(safe_dst), os.path.realpath(effective_root))
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        archive_dest = os.path.join(_effective_archive_dir(), timestamp, rel_dst)
        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        try:
            shutil.copy2(safe_dst, archive_dest)
            archived_msg = f" (overwrote '{dst}', copy archived to '{archive_dest}')"
        except OSError as exc:
            return f"ERROR: could not archive existing '{dst}': {exc}"

    dst_dir = os.path.dirname(safe_dst)
    if dst_dir:
        os.makedirs(dst_dir, exist_ok=True)
    try:
        shutil.move(safe_src, safe_dst)
    except OSError as exc:
        return f"ERROR: move failed: {exc}"

    _invalidate_prepped_cache(safe_src)
    _invalidate_prepped_cache(safe_dst)
    _git_run(["git", "rm", "--cached", "--force", "-q", safe_src])
    _git_run(["git", "add", safe_dst])
    return f"OK: moved '{src}' -> '{dst}'{archived_msg}. Staged for git."


# ---------------------------------------------------------------------------
# Output slicing helper (shared by read_git_diff, read_git_log, find_in_files)
# ---------------------------------------------------------------------------

def _slice_output(
    text: str,
    *,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """Apply head/tail/grep/offset/limit filters to a multi-line string."""
    lines = text.splitlines()
    if offset is not None:
        lines = lines[offset:]
    if limit is not None:
        lines = lines[:limit]
    if grep:
        try:
            pat = re.compile(grep, re.IGNORECASE)
            lines = [l for l in lines if pat.search(l)]
        except re.error:
            lines = [l for l in lines if grep in l]
    if head is not None:
        lines = lines[:head]
    elif tail is not None:
        lines = lines[-tail:]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Git tools
# ---------------------------------------------------------------------------

def _is_inside_maestro_repo(path: str) -> bool:
    """
    Return True if *path* is inside TheMaestro's own git repository.

    Resolves the actual git root of *path* (handles symlinks, submodules,
    worktrees) and compares it to MAESTRO_GIT_ROOT.  Falls back to a simple
    prefix check if git is unavailable.
    """
    if not MAESTRO_GIT_ROOT:
        return False
    norm = os.path.normcase(os.path.normpath(path))
    # Fast prefix check first (avoids a subprocess for the common case)
    if norm == MAESTRO_GIT_ROOT or norm.startswith(MAESTRO_GIT_ROOT + os.sep):
        return True
    # Authoritative check: resolve the actual git root of the target path
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            target_root = os.path.normcase(os.path.normpath(result.stdout.strip()))
            return target_root == MAESTRO_GIT_ROOT
    except Exception:
        pass
    return False


def _git_run(args: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """
    Internal helper - run a git command, return (returncode, stdout, stderr).

    Working directory resolution order:
      1. Explicit ``cwd`` argument (rare - used by internal callers that already
         know the path, e.g. write_file staging).
      2. The per-task context set via ``set_task_git_cwd()``.
      3. Hard error - no fallback to TheMaestro's own repo.

    Hard safety rail: any git operation whose resolved working directory falls
    inside TheMaestro's own git repository is unconditionally blocked.  The
    agent may only operate on child project repositories.
    """
    effective_cwd = cwd or _task_git_cwd.get()
    if effective_cwd is None:
        return (
            1,
            "",
            "ERROR: No task git working directory configured. "
            "Call set_task_git_cwd(project_path) before using git tools.",
        )
    # --- SAFETY RAIL ---
    if _is_inside_maestro_repo(effective_cwd):
        return (
            1,
            "",
            "BLOCKED: Git operations inside TheMaestro's own repository are "
            "not permitted.  Configure a separate project path for this task "
            f"(attempted cwd: {effective_cwd}).",
        )
    ensure_git_repo(effective_cwd)
    try:
        result = subprocess.run(
            args,
            cwd=effective_cwd,
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
        rc = result.returncode
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()
        logger.debug("git %s rc=%d", args[1:], rc)
        if rc != 0:
            if stderr:
                logger.warning("git %s failed: %s", args[1:], stderr)
            else:
                # git outputs status messages (e.g. "nothing to commit") to stdout;
                # empty stderr with non-zero rc is a normal outcome for some commands.
                logger.debug("git %s exited %d (no stderr): %s", args[1:], rc, stdout[:200])
        return rc, stdout, stderr
    except Exception as exc:
        return 1, "", str(exc)


def read_git_status(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] Return the current git status of the project. head/tail/grep filter output. No state change."""
    rc, out, err = _git_run(["git", "status"])
    if rc != 0:
        return f"ERROR: git status failed: {err}"
    return _slice_output(out, head=head, tail=tail, grep=grep)


def read_git_diff(
    path: str | None = None,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[READ] Return git diff (staged + unstaged). Optionally scoped to a path. head/tail/grep filter output. No state change."""
    args = ["git", "diff", "HEAD"]
    if path:
        try:
            safe_path = _assert_safe_path(path)
            args.append(safe_path)
        except ValueError as exc:
            return f"BLOCKED: {exc}"
    rc, out, err = _git_run(args)
    if rc != 0:
        return f"ERROR: git diff failed: {err}"
    result = out or "(no changes)"
    return _slice_output(result, head=head, tail=tail, grep=grep)


def read_git_log(
    path: str | None = None,
    max_count: int = 20,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[READ] Return recent git log entries. Optionally scoped to a file. head/tail/grep filter output. No state change."""
    max_count = min(max(1, max_count), TOOL_MAX_GIT_LOG_ENTRIES)
    args = ["git", "log", f"--max-count={max_count}",
            "--format=%h %ai %an | %s"]
    if path:
        try:
            safe_path = _assert_safe_path(path)
            args.append("--")
            args.append(safe_path)
        except ValueError as exc:
            return f"BLOCKED: {exc}"
    rc, out, err = _git_run(args)
    if rc != 0:
        return f"ERROR: git log failed: {err}"
    result = out or "(no log entries)"
    return _slice_output(result, head=head, tail=tail, grep=grep)


def read_git_blame(path: str, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] Show git blame for a file (last-modified info per line). head/tail/grep filter output. No state change."""
    try:
        safe_path = _assert_safe_path(path)
    except ValueError as exc:
        return f"BLOCKED: {exc}"
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    rc, out, err = _git_run(["git", "blame", safe_path])
    if rc != 0:
        return f"ERROR: git blame failed: {err}"
    return _slice_output(out or "(no blame output)", head=head, tail=tail, grep=grep)


def read_git_show(ref: str, path: str | None = None, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] Show a file at a git ref, or commit details (message + diffstat). head/tail/grep filter output. No state change."""
    if not re.match(r'^[A-Za-z0-9\-_./~^@]+$', ref):
        return "ERROR: ref contains invalid characters. Only alphanumeric, -, _, ., /, ~, ^, @ are allowed."
    if path:
        try:
            safe_path = _assert_safe_path(path)
        except ValueError as exc:
            return f"BLOCKED: {exc}"
        working_dir = _task_git_cwd.get() or PROJECT_ROOT
        rel_path = os.path.relpath(safe_path, working_dir).replace("\\", "/")
        args = ["git", "show", f"{ref}:{rel_path}"]
    else:
        args = ["git", "show", ref, "--stat"]
    rc, out, err = _git_run(args)
    if rc != 0:
        return f"ERROR: git show failed: {err}"
    return _slice_output(out or "(no output)", head=head, tail=tail, grep=grep)


def write_git_branch(branch_name: str) -> str:
    """[WRITE — git] Create and checkout a new branch. Branch name must start with 'maestro/task-'. Reversible only by switching branches."""
    if not branch_name.startswith(GIT_SAFETY_BRANCH_PREFIX):
        return (
            f"ERROR: Branch name must start with '{GIT_SAFETY_BRANCH_PREFIX}'. "
            f"Got '{branch_name}'."
        )
    rc, out, err = _git_run(["git", "checkout", "-b", branch_name])
    if rc != 0:
        if "already exists" in err:
            rc2, cur, _ = _git_run(["git", "rev-parse", "--abbrev-ref", "HEAD"])
            if rc2 == 0 and cur.strip() == branch_name:
                return f"OK: branch '{branch_name}' already exists and is checked out."
        return f"ERROR: could not create branch '{branch_name}': {err}"
    return f"OK: created and checked out branch '{branch_name}'."


def write_git_commit(message: str) -> str:
    """[WRITE — git] Stage all changes (including untracked files) and create a commit. Permanent record — reversible only via a revert commit."""
    _git_run(["git", "add", "-A"])
    rc, out, err = _git_run(["git", "commit", "-m", message])
    if rc != 0:
        combined = (err + "\n" + out).strip()
        if "nothing to commit" in combined:
            return "OK: nothing to commit - working tree clean."
        return f"ERROR: git commit failed: {combined}"
    return f"OK: committed.\n{out}"


def write_git_checkout(branch: str) -> str:
    """[WRITE — git] Checkout a branch. Only maestro/task-* branches are permitted — agents never leave their task branch."""
    if not branch.startswith(GIT_SAFETY_BRANCH_PREFIX):
        logger.warning("Blocked git checkout to disallowed branch: %s", branch)
        return (
            f"ERROR: Checkout of '{branch}' is not permitted. "
            f"Only 'maestro/task-*' branches are allowed. "
            "Agents in worktrees must stay on their assigned branch."
        )
    rc, out, err = _git_run(["git", "checkout", branch])
    if rc != 0:
        return f"ERROR: git checkout '{branch}' failed: {err}"
    return f"OK: checked out '{branch}'."


def write_git_restore(path: str) -> str:
    """[WRITE — git] Restore a tracked file to its HEAD state, DISCARDING all local changes. Irreversible for unsaved work."""
    try:
        safe_path = _assert_safe_path(path)
    except ValueError as exc:
        return f"BLOCKED: {exc}"
    rc, out, err = _git_run(["git", "restore", safe_path])
    if rc != 0:
        return f"ERROR: git restore '{path}' failed: {err or out}"
    return f"OK: restored '{path}' to HEAD state."




def consult_maestro(question: str) -> str:
    """
    Escalate a question to the Maestro orchestrator and receive an answer inline.
    Use this when you are stuck, facing an architectural ambiguity, or repeatedly
    failing a test and need a higher-level perspective.

    Execution continues after you receive the answer — this tool is non-terminal.
    A per-session call cap applies; when exceeded the tool returns a hard-stop message.
    This tool requires async dispatch; calling it via the synchronous path returns an error.
    """
    return (
        "ERROR: consult_maestro requires async dispatch.  "
        "Use async_dispatch_tool() instead of dispatch_tool()."
    )


def report_tool_bug(
tool_name: str, trying_to: str, expected: str, actual: str) -> str:
    """[DIAGNOSTIC] Report a tool malfunction to the Maestro bug tracker.

    Use this when a tool behaves in a way that prevents you from making progress —
    wrong output, stale content, unexpected error, missing capability, etc.
    After filing the report, try an alternative approach or call submit_work.

    tool_name:  name of the misbehaving tool (e.g. 'patch_file', 'read_file')
    trying_to:  what you were attempting (e.g. 'replace the retry logic in llm_client.py')
    expected:   what the tool should have done
    actual:     what it actually did or returned (paste the error or describe the bad output)
    """
    from app.database import create_tool_bug_report
    task_id = _task_id_ctx.get() or "unknown"
    session_id = _session_id_ctx.get()
    report_id = create_tool_bug_report(
        task_id=task_id,
        tool_name=tool_name,
        trying_to=trying_to,
        expected=expected,
        actual=actual,
        session_id=session_id,
    )
    if report_id is None:
        return "WARNING: bug report could not be saved (DB error), but noted internally."
    return (
        f"Bug report #{report_id} filed for tool '{tool_name}'. "
        "A human operator will review it. Please try an alternative approach or submit_work."
    )


def submit_work(signal: str, summary: str, payload: dict | None = None, previous: bool = False) -> str:
    """[FINISH] The ONLY way to complete a task. Signals that your work is done.

    signal: 'ACCEPTED' (task complete), 'REVERT_TO_DESIGN' (task impossible/needs re-plan),
            'SUBDIVIDE' (needs further breakdown), 'PLAN_UPDATED' (correction complete).
    summary: A concise final report of work done or justification for the signal.
    payload: Optional dictionary for agent-specific data (e.g., test results, sub-task lists).
    """
    # This tool is a 'terminal' tool. It doesn't perform I/O, it returns a
    # special marker that the loop orchestrator (MaestroLoop) intercepts.
    import json

    # Detect suspicious payload patterns that suggest the agent tried to output
    # raw JSON instead of using structured tool-call parameters.
    if payload:
        for key in ("raw_json", "json_response", "json_output", "raw_response"):
            if key in payload:
                logger.warning(
                    "submit_work called with suspicious payload key '%s' — "
                    "this suggests the agent emitted raw JSON text instead of using "
                    "structured tool-call parameters. Prefer passing data as native dict values.",
                    key,
                )
                break

    return json.dumps({
        "__maestro_terminal__": True,
        "signal": signal,
        "summary": summary,
        "payload": payload or {}
    })


def cleanup_ghost_worktrees(project_path: str | None = None) -> dict:
    """[INFRA] Scan all projects for orphaned/ghost git worktrees and locked directories."""
    from mcp_tools.actions import cleanup_ghost_worktrees as _cleanup
    return _cleanup(project_path)


def restart_server() -> str:
    """[INFRA] Trigger a hot-restart of the Maestro server."""
    from mcp_tools.actions import restart_server as _restart
    return _restart()


# ---------------------------------------------------------------------------
# Task / Kanban tools
# ---------------------------------------------------------------------------

def _import_db():
    """Lazy import of database functions to avoid circular import at load time."""
    # The database module lives at app/database.py - add app dir to path if needed
    app_dir = os.path.join(PROJECT_ROOT, "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import database as _db  # noqa: PLC0415
    return _db


# ---------------------------------------------------------------------------
# Planning tools (pure computation - no I/O, safe for any agent)
# ---------------------------------------------------------------------------

def write_arch_doc(title: str, components: list, relationships: list) -> str:
    """[WRITE — db] Write a structured markdown architecture document to .maestro/architecture.md. Returns a short stub."""
    lines = [f"# Architecture: {title}", ""]

    if components:
        lines.append("## Components")
        for comp in components:
            if isinstance(comp, dict):
                name = comp.get("name", "Unnamed")
                desc = comp.get("description", "")
                tech = comp.get("technology", "")
                lines.append(f"### {name}")
                if desc:
                    lines.append(f"{desc}")
                if tech:
                    lines.append(f"- **Technology:** {tech}")
                lines.append("")
            else:
                lines.append(f"- {comp}")
        lines.append("")

    if relationships:
        lines.append("## Relationships")
        for rel in relationships:
            if isinstance(rel, dict):
                src = rel.get("from", "?")
                dst = rel.get("to", "?")
                label = rel.get("label", "uses")
                lines.append(f"- **{src}** --{label}--> **{dst}**")
            else:
                lines.append(f"- {rel}")
        lines.append("")

    content = "\n".join(lines)
    dest = ".maestro/architecture.md"
    try:
        safe_path = _assert_safe_path(dest)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        _git_run(["git", "add", safe_path])
        return f"OK: architecture doc '{title}' saved to '{dest}' ({len(components)} components, {len(relationships)} relationships)."
    except Exception as exc:
        return f"ERROR saving architecture doc: {exc}"


def write_mermaid(diagram_type: str, definition: str) -> str:
    """[WRITE — db] Validate and write a Mermaid diagram to .maestro/diagrams/{type}.md. Valid types: flowchart, sequence, class, er, gantt, state, pie."""
    type_map = {
        "flowchart": "flowchart",
        "flow": "flowchart",
        "sequence": "sequenceDiagram",
        "class": "classDiagram",
        "er": "erDiagram",
        "gantt": "gantt",
        "statediagram": "stateDiagram-v2",
        "state": "stateDiagram-v2",
        "pie": "pie",
    }
    normalized = type_map.get(diagram_type.lower())
    if not normalized:
        return (
            f"ERROR: Invalid diagram type '{diagram_type}'. "
            f"Valid types: {sorted(type_map.keys())}"
        )

    stripped = definition.strip()
    if any(stripped.startswith(d) for d in type_map.values()):
        markup = f"```mermaid\n{stripped}\n```"
    else:
        markup = f"```mermaid\n{normalized}\n{stripped}\n```"

    dest = f".maestro/diagrams/{diagram_type.lower()}.md"
    try:
        safe_path = _assert_safe_path(dest)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(markup)
        _git_run(["git", "add", safe_path])
        line_count = markup.count("\n") + 1
        return f"OK: {normalized} diagram saved to '{dest}' ({line_count} lines)."
    except Exception as exc:
        return f"ERROR saving mermaid diagram: {exc}"


def write_interface_contract(component_name: str, provides: list, consumes: list) -> str:
    """[WRITE — db] Write an API interface contract to .maestro/contracts/{name}.json. Returns a short stub."""
    import json as _json

    contract = {
        "component": component_name,
        "provides": [],
        "consumes": [],
    }

    for item in (provides or []):
        if isinstance(item, dict):
            contract["provides"].append(item)
        else:
            contract["provides"].append({"name": str(item), "type": "unknown"})

    for item in (consumes or []):
        if isinstance(item, dict):
            contract["consumes"].append(item)
        else:
            contract["consumes"].append({"name": str(item), "type": "unknown"})

    content = _json.dumps(contract, indent=2)
    safe_name = re.sub(r"[^\w\-]", "_", component_name)
    dest = f".maestro/contracts/{safe_name}.json"
    try:
        safe_path = _assert_safe_path(dest)
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        _git_run(["git", "add", safe_path])
        return (
            f"OK: interface contract for '{component_name}' saved to '{dest}' "
            f"({len(contract['provides'])} provides, {len(contract['consumes'])} consumes)."
        )
    except Exception as exc:
        return f"ERROR saving interface contract: {exc}"


def write_benchmark(task_id: str, parent_task_id: str, benchmark_type: str, metrics: str) -> str:
    """[WRITE — db] Record a before/after profiling benchmark. benchmark_type: 'before'|'after'. metrics: JSON string with test_duration_ms, memory_peak_mb, complexity_score etc."""
    import json as _json
    if benchmark_type not in ("before", "after"):
        return "ERROR: benchmark_type must be 'before' or 'after'."
    try:
        metrics_dict = _json.loads(metrics) if isinstance(metrics, str) else metrics
    except _json.JSONDecodeError as exc:
        return f"ERROR: metrics must be valid JSON: {exc}"
    try:
        db = _import_db()
        db.create_optimization_benchmark(
            task_id=task_id,
            parent_task_id=parent_task_id,
            benchmark_type=benchmark_type,
            metrics=_json.dumps(metrics_dict),
        )
        return f"OK: Benchmark '{benchmark_type}' recorded for task '{task_id}' (parent: '{parent_task_id}')."
    except Exception as exc:
        return f"ERROR recording benchmark: {exc}"


def web_search(query: str, count: int = 5) -> str:
    """
    Execute a web search.  Supports DuckDuckGo (free) or Brave Search (requires API key).
    Provider is selected via [llm] search_provider in maestro.ini (default: duckduckgo).

    Returns a JSON string of results with titles, URLs, and snippets.
    Uses SearchCache to avoid redundant API calls for identical queries.
    """
    if is_shutting_down():
        raise ShutdownError("Server is shutting down")

    import json as _json
    from datetime import datetime, timedelta, timezone
    from app.database import get_search_cache, create_search_cache, get_last_search_time
    from app.agent.config import BRAVE_API_KEY, TAVILY_API_KEY, SEARCH_PROVIDER

    # 1. Check local cache first
    q = query.strip()
    provider = SEARCH_PROVIDER.lower()
    cached = get_search_cache(q, provider=provider)
    if cached:
        logger.info("Search Cache HIT for query: '%s' (provider: %s)", q, provider)
        return cached.result_json

    # 2. Rate limit check (only if we are about to make a REAL API call)
    last_search = get_last_search_time()
    if last_search:
        # DB stores naive UTC, convert to timezone-aware UTC for comparison
        if last_search.tzinfo is None:
            last_search = last_search.replace(tzinfo=timezone.utc)
        
        now = datetime.now(timezone.utc)
        diff = now - last_search
        limit_minutes = 30
        if diff < timedelta(minutes=limit_minutes):
            wait_remaining = timedelta(minutes=limit_minutes) - diff
            wait_secs = int(wait_remaining.total_seconds())
            wait_mins = wait_secs // 60
            wait_remainder_secs = wait_secs % 60
            return (
                f"ERROR: Rate limit exceeded for search provider '{provider}'. "
                f"To respect our 1000 queries/month budget, we only allow one search every {limit_minutes} minutes. "
                f"Please wait {wait_mins}m {wait_remainder_secs}s or use cached results. "
                "Try refining your query or checking if a similar search was already done."
            )

    # 3. Cache miss - call the selected search provider
    search_results = []

    try:
        if provider == "duckduckgo":
            logger.info("Search Cache MISS for query: '%s' - calling DuckDuckGo", q)
            search_results = _ddg_search(q, count)
        elif provider == "brave":
            if not BRAVE_API_KEY:
                return "ERROR: BRAVE_API_KEY not set but search_provider='brave'. Web search is unavailable."
            logger.info("Search Cache MISS for query: '%s' - calling Brave Search API", q)
            search_results = _brave_search(q, count, BRAVE_API_KEY)
        elif provider == "tavily":
            if not TAVILY_API_KEY:
                return "ERROR: TAVILY_API_KEY not set but search_provider='tavily'. Web search is unavailable."
            logger.info("Search Cache MISS for query: '%s' - calling Tavily Search API", q)
            search_results = _tavily_search(q, count, TAVILY_API_KEY)
        else:
            return f"ERROR: Unknown search_provider '{provider}'. Supported: duckduckgo, brave, tavily."

        final_json = _json.dumps({"query": q, "provider": provider, "results": search_results}, indent=2)

        # 3. Persist to cache for next time
        create_search_cache(q, final_json)

        return final_json
    except ImportError as e:
        lib = {
            "duckduckgo": "duckduckgo-search",
            "brave": "brave",
            "tavily": "tavily-python"
        }.get(provider, provider)
        return f"ERROR: '{lib}' python library not installed. Run 'pip install {lib}' to enable web search. (ImportError: {e})"
    except Exception as exc:
        return f"ERROR: {provider.capitalize()} search failed: {exc}"


def _ddg_search(query: str, count: int) -> list[dict]:
    """Internal helper: execute search via duckduckgo_search library."""
    from duckduckgo_search import DDGS
    
    results = []
    # DDGS.text() is the standard search method in duckduckgo_search 6.x/7.x
    with DDGS() as ddgs:
        ddgs_gen = ddgs.text(query, max_results=min(count, 15))
        for r in ddgs_gen:
            results.append({
                "title": r.get("title", "No Title"),
                "url": r.get("href", "No URL"),
                "description": r.get("body", "No Description"),
            })
    return results


def _brave_search(query: str, count: int, api_key: str) -> list[dict]:
    """Internal helper: execute search via Brave Search API."""
    from brave import Brave
    brave = Brave(api_key=api_key)
    # The brave-search python library might have a different API, 
    # but we follow the established pattern.
    raw_results = brave.search(q=query, count=min(count, 10))
    # Standardize to our format
    results = []
    for r in raw_results:
        results.append({
            "title": r.get("title", "No Title"),
            "url": r.get("url", "No URL"),
            "description": r.get("description", "No Description"),
        })
    return results


def _tavily_search(query: str, count: int, api_key: str) -> list[dict]:
    """Internal helper: execute search via Tavily Search API."""
    from tavily import TavilyClient
    client = TavilyClient(api_key=api_key)
    response = client.search(query=query, max_results=min(count, 15))
    
    results = []
    for r in response.get("results", []):
        results.append({
            "title": r.get("title", "No Title"),
            "url": r.get("url", "No URL"),
            "description": r.get("content", "No Description"),
        })
    return results

    search_results = []
    if hasattr(results, 'web') and hasattr(results.web, 'results'):
        for r in results.web.results:
            search_results.append({
                "title": getattr(r, 'title', 'No Title'),
                "url": getattr(r, 'url', 'No URL'),
                "description": getattr(r, 'description', 'No Description'),
            })
    return search_results


def web_fetch(url: str) -> str:
    """
    Fetch the content of a URL and return a text-only summary.
    Strips HTML tags, scripts, and styles.
    """
    if is_shutting_down():
        raise ShutdownError("Server is shutting down")

    import httpx
    from bs4 import BeautifulSoup

    try:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        }
        with httpx.Client(follow_redirects=True, timeout=15.0) as client:
            resp = client.get(url, headers=headers)
            resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")

        # Remove noise
        for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
            tag.decompose()

        # Get text
        text = soup.get_text(separator="\n")

        # Clean up whitespace
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        clean_text = "\n".join(lines)

        return f"== CONTENT FROM: {url} ==\n\n{clean_text}"
    except Exception as exc:
        return f"ERROR fetching URL '{url}': {exc}"


def spawn_research_agent(question: str, context: str = "") -> str:
    """
    Placeholder for synchronous dispatch - the actual async version is in
    async_dispatch_tool(). When called synchronously, returns an error
    directing the caller to use the async path.
    """
    return (
        "ERROR: spawn_research_agent requires async dispatch. "
        "Use async_dispatch_tool() instead of dispatch_tool()."
    )


def launch_research_agent(question: str, context: str = "") -> str:
    """
    Placeholder for synchronous dispatch.  The real implementation lives in
    async_dispatch_tool() where it can park the caller's LLM slot and await
    the scheduler-dispatched research job.
    """
    return (
        "ERROR: launch_research_agent requires async dispatch. "
        "Use async_dispatch_tool() instead of dispatch_tool()."
    )


def get_task(task_id: str) -> str:
    """Fetch a Kanban task by ID and return it as a JSON string."""
    import json
    try:
        db = _import_db()
        task = db.get_task(task_id)
        if task is None:
            return f"ERROR: Task '{task_id}' not found."
        result = {
            "id": task.id,
            "title": task.title,
            "type": task.type,
            "description": task.description,
            "owner": task.owner,
            "tags": task.tags,
            "prerequisites": getattr(task, "prerequisites", []) or [],
            "history": task.history,
            "position": task.position,
            "created_at": task.created_at.isoformat() if task.created_at else None,
            "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        }

        # Enhance with planning result if available
        try:
            from app.database import get_latest_planning_result
            planning = get_latest_planning_result(task_id)
            if planning:
                result["planning"] = {
                    "file_manifest": json.loads(planning.file_manifest) if planning.file_manifest else [],
                    "implementation_steps": json.loads(planning.implementation_steps) if planning.implementation_steps else [],
                    "interface_contracts": json.loads(planning.interface_contracts) if planning.interface_contracts else [],
                    "test_strategy": json.loads(planning.test_strategy) if planning.test_strategy else "",
                }
        except Exception as e:
            logger.warning("[tools] failed to fetch planning result for task %s: %s", task_id, e)

        return json.dumps(result, indent=2)
    except Exception as exc:
        return f"ERROR fetching task '{task_id}': {exc}"


def list_tasks(project: str, column: str | None = None) -> str:
    """Return task summaries for a project, optionally filtered by column/type."""
    import json
    try:
        db = _import_db()
        tasks = db.get_tasks_by_project(project)
        if column:
            tasks = [t for t in tasks if t.type == column]
        if not tasks:
            msg = f"No tasks found for project '{project}'"
            if column:
                msg += f" in column '{column}'"
            return msg + "."
        summaries = []
        for t in tasks:
            desc = (t.description or "")
            if len(desc) > 100:
                desc = desc[:97] + "..."
            summaries.append({
                "id": t.id,
                "title": t.title,
                "type": t.type,
                "description": desc,
                "owner": t.owner,
                "tags": t.tags,
                "prerequisites": getattr(t, "prerequisites", []) or [],
            })
        return json.dumps(summaries, indent=2)
    except Exception as exc:
        return f"ERROR listing tasks: {exc}"


def write_task_status(task_id: str, new_status: str) -> str:
    """[WRITE — db] Advance a task through the Kanban pipeline. Valid statuses: PENDING, ACTIVE, VERIFYING, REJECTED."""
    STATUS_TO_TYPE = {
        "PENDING": "planning",
        "ACTIVE": "indev",
        "VERIFYING": "conceptual_review",
        # "ACCEPTED" is intentionally absent — routes through review pipeline via submit_work
        "REJECTED": "planning",
    }
    if new_status == "ACCEPTED":
        return (
            "ERROR: 'ACCEPTED' is not valid here. "
            "To signal task completion, call the submit_work tool with signal='ACCEPTED'. "
            "This routes through the full review pipeline (conceptual_review → optimization → "
            "security → final_review) before the task can reach completed."
        )
    if new_status not in STATUS_TO_TYPE:
        return (
            f"ERROR: '{new_status}' is not a valid status. "
            f"Choose from: {list(STATUS_TO_TYPE.keys())}"
        )
    try:
        db = _import_db()
        task = db.update_task(task_id, type=STATUS_TO_TYPE[new_status])
        if task is None:
            return f"ERROR: Task '{task_id}' not found or update failed."
        return f"OK: Task '{task_id}' status updated to '{new_status}' (column: {STATUS_TO_TYPE[new_status]})."
    except Exception as exc:
        return f"ERROR updating task status: {exc}"


def write_task_history(task_id: str, entry: str) -> str:
    """[WRITE — db] Append a proof-of-work entry to a task's history log."""
    import json as _json
    try:
        db = _import_db()
        task = db.get_task(task_id)
        if task is None:
            return f"ERROR: Task '{task_id}' not found."
        history = list(task.history or [])
        history.append({
            "entry": entry,
            "timestamp": datetime.now().isoformat(),
            "source": "maestro-agent",
        })
        updated = db.update_task(task_id, history=history)
        if updated is None:
            return f"ERROR: Failed to update history for task '{task_id}'."
        return f"OK: history entry appended to task '{task_id}'."
    except Exception as exc:
        return f"ERROR appending history: {exc}"


def batch_create_cards(
    cards: list,
    new_parent: "dict | None" = None,
    archive_origin: bool = False,
) -> str:
    """[WRITE — db] Create multiple new task cards in the current task's project.

    Each card is created at its specified entry_stage.  If new_parent is
    provided, a parent card is created first and the new cards are parented
    under it.  If archive_origin is True, the current task is demoted to type
    'archive' after the cards are created.

    Returns JSON: {"created_ids": [...], "parent_id": "..." | null}
    """
    import json as _json
    task_id = _task_id_ctx.get()
    if not task_id:
        return "ERROR: batch_create_cards requires an active task context"

    try:
        db = _import_db()
        task = db.get_task(task_id)
        if not task:
            return f"ERROR: task {task_id!r} not found"
        project_name = task.project or "TheMaestro"
        llm_id = task.llm_id
        budget_id = task.budget_id

        parent_id = None
        if new_parent and isinstance(new_parent, dict):
            p_title = new_parent.get("title", "Parent")
            p_desc = new_parent.get("description", "")
            p_task = db.create_task(
                title=p_title,
                task_type="idea",
                description=p_desc,
                owner="system",
                llm_id=llm_id,
                budget_id=budget_id,
                project=project_name,
            )
            if p_task:
                parent_id = p_task.id

        created_ids: list[str] = []
        actual_id_map: dict[str, str] = {}  # "sub-{i}" → real task ID

        for i, card in enumerate(cards or []):
            if not isinstance(card, dict):
                continue
            entry_stage = card.get("entry_stage") or "idea"
            prereqs = [
                actual_id_map[p]
                for p in (card.get("prereq_ids") or [])
                if p in actual_id_map
            ]
            t = db.create_task(
                title=card.get("title", "Untitled"),
                task_type=entry_stage,
                description=card.get("description", ""),
                owner="system",
                tags=card.get("tags") or [],
                llm_id=llm_id,
                budget_id=budget_id,
                prerequisites=prereqs,
                project=project_name,
                position=i,
                stage_key=entry_stage,
            )
            if t:
                # Re-fetch to set parent_task_id (create_task doesn't accept it)
                db.update_task(
                    t.id,
                    parent_task_id=parent_id or task_id,
                )
                actual_id_map[f"sub-{i}"] = t.id
                created_ids.append(t.id)

        if archive_origin:
            db.update_task(task_id, type="archive", stage_key="archive")

        return _json.dumps({"created_ids": created_ids, "parent_id": parent_id})
    except Exception as exc:
        return f"ERROR in batch_create_cards: {exc}"


def write_plan_fields(result_id: int, fields_json: str) -> str:
    """[WRITE — db] Patch specific fields on a planning_results row. Allowed: design_rationale, interface_contracts, dependency_graph, file_manifest, test_strategy, implementation_steps."""
    import json as _json
    ALLOWED = {"design_rationale", "interface_contracts", "dependency_graph", "file_manifest",
               "test_strategy", "implementation_steps"}
    try:
        fields = _json.loads(fields_json) if isinstance(fields_json, str) else fields_json
        if not isinstance(fields, dict):
            return "ERROR: fields_json must be a JSON object mapping field names to values."
        invalid = set(fields.keys()) - ALLOWED
        if invalid:
            return (
                f"ERROR: Invalid field(s): {sorted(invalid)}. "
                f"Allowed: {sorted(ALLOWED)}"
            )
        if not fields:
            return "ERROR: fields_json is empty — nothing to update."
        _JSON_REQUIRED = {
            "implementation_steps", "file_manifest", "dependency_graph",
            "interface_contracts", "test_strategy",
        }
        serialized = {}
        for k, v in fields.items():
            if isinstance(v, str):
                if k in _JSON_REQUIRED:
                    try:
                        _json.loads(v)
                    except _json.JSONDecodeError as exc:
                        return (
                            f"ERROR: field '{k}' must be a JSON array or object, "
                            f"not plain text (got: {repr(v[:80])}). "
                            f"Pass a list/dict and the tool will encode it. Parse error: {exc}"
                        )
                serialized[k] = v
            else:
                serialized[k] = _json.dumps(v)
        from app.database import update_planning_result
        from app.database.session import SessionLocal as _SL
        db = _SL()
        try:
            result = update_planning_result(db, result_id, **serialized)
            if result is None:
                return f"ERROR: Planning result id={result_id} not found."
            return f"Updated fields: {sorted(serialized.keys())}"
        finally:
            db.close()
    except Exception as exc:
        return f"ERROR: {exc}"


# ---------------------------------------------------------------------------
# Survey tools
# ---------------------------------------------------------------------------

def _get_survey_orchestrator():
    from app.agent.survey_orchestrator import SurveyOrchestrator
    return SurveyOrchestrator()


def get_project_summary(project: str | None = None) -> str:
    """Return the top-level project health summary if it exists and is fresh."""
    p_name = project or _task_project_name.get()
    if not p_name:
        return "ERROR: No project name provided or configured in context."
    try:
        so = _get_survey_orchestrator()
        summary = so.get_project_summary(p_name)
        if not summary:
            return f"No fresh project summary found for '{p_name}'. A survey may be in progress."
        return summary
    except Exception as exc:
        return f"ERROR fetching project summary: {exc}"


def get_directory_summary(rel_dir: str, project: str | None = None) -> str:
    """Return the summary for a specific directory within the project."""
    p_name = project or _task_project_name.get()
    if not p_name:
        return "ERROR: No project name provided or configured in context."
    try:
        from app.database import get_scope_summary
        summary = get_scope_summary(p_name, "directory", rel_dir)
        if not summary:
            return f"No summary found for directory '{rel_dir}' in project '{p_name}'."
        return f"Summary for {rel_dir}:\n{summary.summary}"
    except Exception as exc:
        return f"ERROR fetching directory summary: {exc}"


def get_module_summary(module_name: str, project: str | None = None) -> str:
    """Return the summary for a logical module within the project."""
    p_name = project or _task_project_name.get()
    if not p_name:
        return "ERROR: No project name provided or configured in context."
    try:
        from app.database import get_scope_summary
        summary = get_scope_summary(p_name, "module", module_name)
        if not summary:
            return f"No summary found for module '{module_name}' in project '{p_name}'."
        return f"Summary for module {module_name}:\n{summary.summary}"
    except Exception as exc:
        return f"ERROR fetching module summary: {exc}"


def list_scope_summaries(project: str | None = None, scope_type: str | None = None) -> str:
    """List all available scope summaries (type, key, short_summary, freshness) for a project."""
    p_name = project or _task_project_name.get()
    if not p_name:
        return "ERROR: No project name provided or configured in context."
    import json
    try:
        from app.database import list_scope_summaries as db_list_scopes
        scopes = db_list_scopes(p_name, scope_type=scope_type)
        if not scopes:
            return f"No scope summaries found for project '{p_name}'."
        
        results = []
        for s in scopes:
            results.append({
                "type": s.scope_type,
                "key": s.scope_key,
                "short_summary": s.short_summary,
                "staleness": s.staleness_state,
                "updated_at": s.updated_at.isoformat() if s.updated_at else None
            })
        return json.dumps(results, indent=2)
    except Exception as exc:
        return f"ERROR listing scope summaries: {exc}"


# ---------------------------------------------------------------------------
# Named shell tool implementations (one tool per operation)
# ---------------------------------------------------------------------------
# Each named tool hardcodes a specific command and delegates to the
# appropriate internal runner. The LLM never guesses what's allowed —
# the tool name IS the command.
# ---------------------------------------------------------------------------

def _make_py_cmd(base: str, cwd: str) -> str:
    """Rewrite 'python' in base to the project venv's Python interpreter."""
    from app.agent.worktree import venv_python as _vp
    py = _vp(cwd)
    if py != "python":
        return re.sub(r"^python\b", py.replace("\\", "/"), base)
    return base


def _venv_python(project_cwd: str) -> str:
    """Return the venv python executable for the project, falling back to 'python'."""
    for candidate in (
        os.path.join(project_cwd, "venv", "Scripts", "python.exe"),
        os.path.join(project_cwd, "venv", "bin", "python"),
        os.path.join(project_cwd, ".venv", "Scripts", "python.exe"),
        os.path.join(project_cwd, ".venv", "bin", "python"),
    ):
        if os.path.isfile(candidate):
            return candidate
    return "python"


# --- Tool flag allowlists ---
# Tokens the LLM passes that are NOT in these sets are logged and silently dropped.

_SHELL_METACHAR_RE = re.compile(r'[;|&><`$\n\r\x00()\{\}\\]')

# Pip version specifiers legitimately use < and > (e.g. "requests>=2.28,<3").
# Since run_deps_pip always calls pip with shell=False, these chars are safe.
_PIP_ARGS_METACHAR_RE = re.compile(r'[;|&`$\n\r\x00()\{\}\\]')

_PYTEST_FLAGS = frozenset({
    "-x", "--exitfirst",
    "-v", "--verbose", "-q", "--quiet", "--no-header", "--no-summary",
    "-s", "--capture=no", "--capture=fd", "--capture=sys", "--capture=tee-sys",
    "--tb=short", "--tb=long", "--tb=no", "--tb=line", "--tb=native", "--tb=auto",
    "--co", "--collect-only",
    "--last-failed", "--lf", "--failed-first", "--ff", "--new-first", "--nf",
    "--cache-clear", "--cache-show",
    "-p no:warnings", "-p no:cacheprovider",
    "--pdb",
    "--assert=plain", "--assert=rewrite",
    "--strict-markers", "--strict-config",
    "--no-cov", "--cov-report=term", "--cov-report=term-missing",
    "--durations=0", "--durations=10", "--durations=20",
    "--log-cli-level=DEBUG", "--log-cli-level=INFO", "--log-cli-level=WARNING",
    "--import-mode=importlib", "--import-mode=prepend", "--import-mode=append",
    "--runxfail",
    "--override-ini=xfail_strict=true", "--override-ini=xfail_strict=false",
})
_PYTEST_VALUE_FLAGS = frozenset({
    "-k", "-m", "--ignore", "-n", "--timeout", "--cov",
    "--rootdir", "--junit-xml", "--log-file",
})

_MYPY_FLAGS = frozenset({
    "--strict", "--ignore-missing-imports",
    "--no-error-summary", "--pretty", "--show-error-codes", "--show-error-context",
    "--warn-return-any", "--warn-unused-ignores", "--warn-unused-configs",
    "--warn-redundant-casts",
    "--disallow-untyped-defs", "--disallow-incomplete-defs",
    "--disallow-untyped-calls", "--disallow-any-generics",
    "--check-untyped-defs", "--no-check-untyped-defs",
    "--follow-imports=silent", "--follow-imports=skip", "--follow-imports=normal",
    "--no-site-packages", "--no-namespace-packages",
    "-q", "--quiet", "--no-color-output",
    "--show-column-numbers", "--show-absolute-path",
})
_MYPY_VALUE_FLAGS = frozenset({
    "--exclude", "--python-version", "--platform",
    "--config-file", "--package", "--module",
})

_RUFF_FLAGS = frozenset({
    "--fix", "--no-fix", "--unsafe-fixes",
    "-q", "--quiet", "--no-cache", "--statistics",
    "--output-format=text", "--output-format=json", "--output-format=grouped",
    "--show-fixes", "--show-source",
    "--exit-zero", "--exit-non-zero-on-fix",
    "--respect-gitignore", "--no-respect-gitignore",
    "--isolated",
})
_RUFF_VALUE_FLAGS = frozenset({
    "--select", "--ignore", "--extend-select",
    "--extend-ignore", "--per-file-ignores",
    "--config", "--line-length", "--target-version",
})

_CARGO_TEST_FLAGS = frozenset({
    "--verbose", "-v", "-q", "--quiet",
    "--release", "--all-features", "--no-default-features",
    "--lib", "--bins", "--examples", "--tests", "--benches", "--all-targets",
    "--nocapture", "--no-fail-fast",
    "--color=auto", "--color=always", "--color=never",
})
_CARGO_TEST_VALUE_FLAGS = frozenset({
    "--features", "--package", "-p",
    "--manifest-path", "--target", "--jobs", "-j",
})

_GO_TEST_FLAGS = frozenset({
    "-v", "-race", "-count=1", "-count=10",
    "-short", "-failfast",
    "-cover", "-covermode=set", "-covermode=count", "-covermode=atomic",
    "-json",
})
_GO_TEST_VALUE_FLAGS = frozenset({
    "-run", "-bench", "-benchtime", "-timeout",
    "-cpu", "-parallel", "-count", "-coverprofile",
})

_CARGO_BUILD_FLAGS = frozenset({
    "--verbose", "-v", "-q", "--quiet",
    "--release", "--all-features", "--no-default-features",
    "--lib", "--bins", "--examples",
    "--color=auto", "--color=always", "--color=never",
    "--frozen", "--locked",
})
_CARGO_BUILD_VALUE_FLAGS = frozenset({
    "--features", "--package", "-p",
    "--manifest-path", "--target", "--jobs", "-j",
})

_TSC_FLAGS = frozenset({
    "--noEmit", "--strict", "--declaration",
    "--sourceMap", "--watch",
    "--incremental", "--composite",
    "--pretty", "--noEmitOnError",
    "--listFiles", "--diagnostics",
})
_TSC_VALUE_FLAGS = frozenset({
    "--target", "--module", "--moduleResolution",
    "--lib", "--outDir", "--rootDir", "--project", "-p",
})

_MAKE_TARGET_RE = re.compile(r'^[a-zA-Z0-9_.\-/]+$')
_NPM_SCRIPT_RE = re.compile(r'^[a-zA-Z0-9_\-:.]+$')
_MVN_GOAL_RE = re.compile(r'^[a-zA-Z0-9_:.\-]+$')

# --- End allowlists ---


def _validate_flags(
    flags: str,
    tool_name: str,
    allowlist: frozenset,
    value_flags: frozenset = frozenset(),
    task_id: str | None = None,
) -> tuple[list, list]:
    """
    Split flags string and return (safe_flags, rejected_flags).

    safe_flags: only allowlisted tokens, safe to pass to subprocess.
    rejected_flags: tokens that were dropped (logged + returned so callers
    can surface the reason to the LLM agent).
    """
    if not flags or not flags.strip():
        return [], []
    try:
        tokens = shlex.split(flags)
    except ValueError as exc:
        logger.warning(
            "[security] Tool '%s' (task=%s): shlex.split failed on flags=%r — rejected entirely. Error: %s",
            tool_name, task_id, flags[:200], exc,
        )
        return [], [repr(flags[:200])]

    result: list = []
    rejected: list = []
    skip_next = False

    for i, token in enumerate(tokens):
        if skip_next:
            if _SHELL_METACHAR_RE.search(token):
                rejected.append(f"{token!r} (value for {tokens[i-1]!r} contains metachar)")
                result.pop()
            elif len(token) > 256:
                rejected.append(f"{token!r} (value too long: {len(token)} chars)")
                result.pop()
            else:
                result.append(token)
            skip_next = False
            continue

        flag_key = token.split("=")[0] if "=" in token else token

        if token in allowlist or flag_key in allowlist:
            result.append(token)
        elif flag_key in value_flags:
            result.append(token)
            skip_next = True
        else:
            rejected.append(repr(token))

    if rejected:
        logger.warning(
            "[security] Tool '%s' (task=%s) rejected %d flag token(s): %s",
            tool_name, task_id, len(rejected), ", ".join(rejected),
        )
    return result, rejected


def _validate_tool_path(path: str, tool_name: str, task_id: str | None = None) -> str | None:
    """
    Validate a path argument from a run_* tool.
    Returns the path if safe, or None if rejected (with a log warning).
    """
    if not path or not path.strip():
        return "."
    if _SHELL_METACHAR_RE.search(path):
        logger.warning(
            "[security] Tool '%s' (task=%s) rejected path=%r: contains shell metacharacters",
            tool_name, task_id, path[:200],
        )
        return None
    norm = os.path.normpath(path)
    if os.path.isabs(path) or os.path.isabs(norm) or norm.startswith(".."):
        logger.warning(
            "[security] Tool '%s' (task=%s) rejected path=%r: absolute or traversal path",
            tool_name, task_id, path[:200],
        )
        return None
    if len(path) > 512:
        logger.warning(
            "[security] Tool '%s' (task=%s) rejected path: too long (%d chars)",
            tool_name, task_id, len(path),
        )
        return None
    return path


# --- Testing / linting ---

def run_test_pytest(
    path: str = ".",
    flags: str = "",
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[RUN — sandbox] Run pytest. path: test file, dir, or space-separated list of test files/dirs (default '.'). flags: pytest option flags only (e.g. '-x -v'). Per-test timeout injected automatically. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."

    # Support space-separated list of test paths (e.g. path="tests/test_a.py tests/test_b.py")
    raw_paths = path.split() if path.strip() else ["."]
    safe_paths: list[str] = []
    rejected_paths: list[str] = []
    for _p in raw_paths:
        _validated = _validate_tool_path(_p, "run_test_pytest")
        if _validated:
            safe_paths.append(_validated)
        else:
            rejected_paths.append(repr(_p))
    if not safe_paths:
        safe_paths = ["."]

    safe_flags, _rejected_flags = _validate_flags(flags, "run_test_pytest", _PYTEST_FLAGS, _PYTEST_VALUE_FLAGS, _task_id_ctx.get())
    _rejected = _rejected_flags + rejected_paths
    _rejection_prefix = ""
    if _rejected:
        _rejection_prefix = (
            f"[SECURITY] {len(_rejected)} flag(s) blocked by security policy and removed: "
            f"{', '.join(_rejected)}.\n"
            "NOTE: A per-test timeout is automatically injected — do not pass "
            "-p no:timeout, -o timeout=0, --override-ini=addopts=, or similar "
            "timeout-disabling flags. Use --timeout=N if a longer timeout is needed.\n"
            "NOTE: To run multiple test files, pass them space-separated in the path argument, "
            "e.g. path='tests/test_a.py tests/test_b.py'.\n\n"
        )
    args = [_venv_python(cwd), "-m", "pytest"] + safe_paths + safe_flags
    # Inject per-test timeout unless the project config already sets one.
    has_timeout = any("--timeout" in f for f in safe_flags)
    if not has_timeout:
        _has_timeout_config = False
        for _cfg in ("pytest.ini", "pyproject.toml", "setup.cfg", "tox.ini"):
            _cfg_path = os.path.join(cwd, _cfg)
            if os.path.isfile(_cfg_path):
                try:
                    with open(_cfg_path) as _f:
                        if "timeout" in _f.read():
                            _has_timeout_config = True
                            break
                except OSError:
                    pass
        if not _has_timeout_config:
            injected = max(60, SHELL_TIMEOUT_SECONDS)
            args.append(f"--timeout={injected}")
    timeout_msg = (
        f"ERROR: Command timed out after {SHELL_TIMEOUT_SECONDS}s. "
        "This may indicate a hang, infinite loop, or high computational complexity."
    )
    rc, result = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, timeout_msg)
    _last_test_output.set(result)
    return _rejection_prefix + _slice_output(result, head=head, tail=tail, grep=grep)


def run_check_mypy(path: str, flags: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run mypy type-checker. path: file or package. flags: extra mypy flags. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_path = _validate_tool_path(path, "run_check_mypy") or "."
    safe_flags, _ = _validate_flags(flags, "run_check_mypy", _MYPY_FLAGS, _MYPY_VALUE_FLAGS)
    args = [_venv_python(cwd), "-m", "mypy", safe_path] + safe_flags
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: mypy timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_check_ruff(path: str = ".", flags: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run ruff linter. path: file or dir (default '.'). flags: extra ruff flags. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_path = _validate_tool_path(path, "run_check_ruff") or "."
    safe_flags, _ = _validate_flags(flags, "run_check_ruff", _RUFF_FLAGS, _RUFF_VALUE_FLAGS)
    args = [_venv_python(cwd), "-m", "ruff", "check", safe_path] + safe_flags
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: ruff timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_check_black(path: str = ".", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Check formatting with black (read-only — does not modify files). path: file or dir (default '.'). head/tail/grep filter output."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_path = _validate_tool_path(path, "run_check_black") or "."
    args = [_venv_python(cwd), "-m", "black", "--check", safe_path]
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: black timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_test_unittest(module: str = "", pattern: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run Python unittest. module: dotted module name (e.g. 'tests.test_foo'). pattern: file pattern for discover (e.g. 'test_*.py'). head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    args = [_venv_python(cwd), "-m", "unittest"]
    if module:
        if re.match(r'^[\w.]+$', module):
            args.append(module)
        else:
            logger.warning("[security] run_test_unittest rejected module=%r", module)
            return f"[security] unittest module {module!r} rejected: must be a dotted identifier"
    elif pattern:
        if _SHELL_METACHAR_RE.search(pattern) or len(pattern) > 128:
            logger.warning("[security] run_test_unittest rejected pattern=%r", pattern)
            return f"[security] unittest pattern {pattern!r} rejected"
        args += ["discover", "-p", pattern]
    else:
        args.append("discover")
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: unittest timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_test_npm(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run npm test in the task's project directory. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    rc, out = _run_tool_subprocess(["npm", "test"], cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: npm test timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_test_cargo(args: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run cargo test. args: extra cargo test flags. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_flags, _ = _validate_flags(args, "run_test_cargo", _CARGO_TEST_FLAGS, _CARGO_TEST_VALUE_FLAGS)
    rc, out = _run_tool_subprocess(["cargo", "test"] + safe_flags, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: cargo test timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_test_go(path: str = "./...", flags: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — sandbox] Run go test. path: package path (default './...'). flags: extra go test flags. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_path = _validate_tool_path(path, "run_test_go") or "./..."
    safe_flags, _ = _validate_flags(flags, "run_test_go", _GO_TEST_FLAGS, _GO_TEST_VALUE_FLAGS)
    args = ["go", "test"] + safe_flags + [safe_path]
    rc, out = _run_tool_subprocess(args, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: go test timed out after {SHELL_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


# --- Build ---

def run_build_make(target: str, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Run a Makefile target. target: e.g. 'build', 'test', 'all'. head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    if not _MAKE_TARGET_RE.match(target):
        logger.warning("[security] run_build_make rejected target=%r", target)
        return f"[security] make target {target!r} rejected: must match ^[a-zA-Z0-9_.\\-/]+$"
    rc, out = _run_tool_subprocess(["make", target], cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: make timed out after {_BUILD_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_cargo(args: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Build a Rust/Cargo project (cargo build). head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_flags, _ = _validate_flags(args, "run_build_cargo", _CARGO_BUILD_FLAGS, _CARGO_BUILD_VALUE_FLAGS)
    rc, out = _run_tool_subprocess(["cargo", "build"] + safe_flags, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: cargo build timed out after {_BUILD_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_go(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Build a Go project (go build ./...). head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    rc, out = _run_tool_subprocess(["go", "build", "./..."], cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: go build timed out after {_BUILD_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_npm(script: str = "build", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Run an npm build script (npm run <script>). head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    if not _NPM_SCRIPT_RE.match(script):
        logger.warning("[security] run_build_npm rejected script=%r", script)
        return f"[security] npm script {script!r} rejected: must match ^[a-zA-Z0-9_\\-:.]+$"
    rc, out = _run_tool_subprocess(["npm", "run", script], cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: npm build timed out after {_BUILD_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_tsc(args: str = "", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Run the TypeScript compiler (tsc). head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    safe_flags, _ = _validate_flags(args, "run_build_tsc", _TSC_FLAGS, _TSC_VALUE_FLAGS)
    rc, out = _run_tool_subprocess(["tsc"] + safe_flags, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: tsc timed out after {_BUILD_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_gradle(target: str, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Run a Gradle task. target: e.g. 'build', 'assemble'. head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    if not _MAKE_TARGET_RE.match(target):
        logger.warning("[security] run_build_gradle rejected target=%r", target)
        return f"[security] gradle target {target!r} rejected"
    gradle_exe = "./gradlew" if os.path.isfile(os.path.join(cwd, "gradlew")) else "gradle"
    rc, out = _run_tool_subprocess([gradle_exe, target], cwd, _BUILD_TIMEOUT_SECONDS * 2, f"ERROR: gradle timed out after {_BUILD_TIMEOUT_SECONDS * 2}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_build_mvn(goal: str, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — build] Run a Maven goal. goal: e.g. 'package', 'compile'. head/tail/grep filter output. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    if not _MVN_GOAL_RE.match(goal):
        logger.warning("[security] run_build_mvn rejected goal=%r", goal)
        return f"[security] mvn goal {goal!r} rejected"
    rc, out = _run_tool_subprocess(["mvn", goal, "--batch-mode", "--no-transfer-progress"], cwd, _BUILD_TIMEOUT_SECONDS * 2, f"ERROR: mvn timed out after {_BUILD_TIMEOUT_SECONDS * 2}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


# --- Dependencies ---

def run_deps_pip(args: str, head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — deps] Install Python packages with pip. MUTATES environment. args: e.g. '-r requirements.txt', 'requests>=2.28'. head/tail/grep filter output."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    stripped = args.strip() if args else ""
    if not stripped:
        return "[error] run_deps_pip: no arguments provided"
    if _PIP_ARGS_METACHAR_RE.search(stripped):
        logger.warning("[security] run_deps_pip rejected args=%r: metacharacters", stripped[:200])
        return "[security] pip args rejected: contains shell metacharacters"
    try:
        tokens = shlex.split(stripped)
    except ValueError as e:
        return f"[security] pip args rejected: {e}"
    allowed_pip_tokens = {"--upgrade", "-U", "--no-deps", "--quiet", "-q",
                          "--no-index", "--pre"}
    req_file_next = False
    clean_tokens: list = []
    for tok in tokens:
        if req_file_next:
            if re.match(r'^[\w./\-]+\.txt$', tok) and not tok.startswith(".."):
                clean_tokens.append(tok)
            else:
                logger.warning("[security] run_deps_pip rejected requirements file: %r", tok)
                return f"[security] pip requirements file {tok!r} rejected"
            req_file_next = False
        elif tok in ("-r", "--requirement"):
            clean_tokens.append(tok)
            req_file_next = True
        elif tok in allowed_pip_tokens:
            clean_tokens.append(tok)
        elif re.match(r'^[a-zA-Z0-9_\-.\[\]]+([><=!]+[\w.]+)?$', tok):
            clean_tokens.append(tok)
        else:
            logger.warning("[security] run_deps_pip rejected token: %r", tok)
            return f"[security] pip argument {tok!r} rejected"
    args_list = [_venv_python(cwd), "-m", "pip", "install"] + clean_tokens
    rc, out = _run_tool_subprocess(args_list, cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: pip install timed out after {_DEPS_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_deps_npm(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — deps] Install Node.js dependencies (npm install). MUTATES environment. head/tail/grep filter output. Call after modifying package.json."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    rc, out = _run_tool_subprocess(["npm", "install"], cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: npm install timed out after {_DEPS_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_deps_cargo(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — deps] Fetch Rust/Cargo dependencies (cargo fetch). MUTATES environment. head/tail/grep filter output. Call after modifying Cargo.toml."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    rc, out = _run_tool_subprocess(["cargo", "fetch"], cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: cargo fetch timed out after {_DEPS_TIMEOUT_SECONDS}s.")
    return _slice_output(out, head=head, tail=tail, grep=grep)


# --- Security scanners ---

def run_audit_bandit(path: str = ".", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — audit] Run bandit Python security linter. No project-file mutation. path: dir or file (default '.'). head/tail/grep filter output."""
    from app.agent.security_review import run_shell_security
    out = run_shell_security("bandit", path)
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_audit_pip(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — audit] Audit installed Python packages for known vulnerabilities (pip-audit). No project-file mutation. head/tail/grep filter output."""
    from app.agent.security_review import run_shell_security
    out = run_shell_security("pip-audit")
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_audit_semgrep(path: str = ".", config: str = "auto", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — audit] Run semgrep static analysis. No project-file mutation. config: ruleset (default 'auto', always used). head/tail/grep filter output."""
    if config != "auto":
        logger.warning("[security] run_audit_semgrep: config=%r ignored, using 'auto'", config)
    from app.agent.security_review import run_shell_security
    out = run_shell_security("semgrep", path)
    return _slice_output(out, head=head, tail=tail, grep=grep)


def run_audit_npm(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[RUN — audit] Run npm audit to check Node.js dependencies for vulnerabilities. No project-file mutation. head/tail/grep filter output."""
    from app.agent.security_review import run_shell_security
    out = run_shell_security("npm-audit")
    return _slice_output(out, head=head, tail=tail, grep=grep)


# ---------------------------------------------------------------------------
# Shared subprocess runner (internal — not exposed as a tool)
# ---------------------------------------------------------------------------

_BUILD_TIMEOUT_SECONDS = 300
_DEPS_TIMEOUT_SECONDS = 600


def _run_tool_subprocess(
    args: list,
    cwd: str,
    timeout: int,
    timeout_msg: str,
) -> tuple:
    """
    Execute a fixed command+args list with shell=False.
    Never accepts a string command. Never interprets shell metacharacters.
    Returns (returncode, combined_output).
    """
    from app.agent.worktree import setup_test_environment
    setup_test_environment(cwd)

    try:
        proc = subprocess.Popen(
            args,
            shell=False,  # immutable — never change this
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            cwd=cwd,
        )
        try:
            stdout, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True,
                )
            else:
                proc.kill()
            proc.communicate()
            return 1, timeout_msg
        output = stdout or ""
        rc = proc.returncode
        body = output if output else ""
        return rc, f"[EXIT:{rc}]\n{body}"
    except FileNotFoundError as exc:
        return 1, f"Command not found: {exc}"
    except Exception as exc:
        return 1, f"Subprocess error: {exc}"


# ---------------------------------------------------------------------------
# New helper tools (Phase 1: additive additions)
# ---------------------------------------------------------------------------

def read_last_output(
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
    offset: int | None = None,
    limit: int | None = None,
) -> str:
    """[READ] Slice the previous tool call's full output without re-running it. No state change.

    Uses the output buffer populated by the most recent tool call in this session.
    head/tail/grep/offset/limit apply in this order: offset, limit, grep, head/tail.
    """
    with _output_buffer_lock:
        buf = _output_buffer.get("_sync", "")
    if not buf:
        return "(no previous tool output in buffer)"
    return _slice_output(buf, head=head, tail=tail, grep=grep, offset=offset, limit=limit)


def read_diff_stat(since: str = "main", head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] git diff --stat from <since> to HEAD, parsed into added/removed per file. head/tail/grep filter output. No state change."""
    effective_cwd = _task_git_cwd.get()
    if not effective_cwd:
        return "ERROR: No task git working directory configured."
    try:
        result = subprocess.run(
            ["git", "diff", f"{since}..HEAD", "--stat"],
            cwd=effective_cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return _slice_output(result.stdout.strip(), head=head, tail=tail, grep=grep)
        return f"No changes since '{since}'."
    except Exception as exc:
        return f"Unable to compute diff stat: {exc}"


# ---------------------------------------------------------------------------
# Static analysis tools (backed by static_analysis.py tree-sitter parser)
# ---------------------------------------------------------------------------

_sa_cache: dict[str, Any] = {}  # project_root → ProjectAnalysis
_sa_cache_lock = threading.Lock()


def _get_project_analysis():
    """Return a cached ProjectAnalysis for the current task's project root."""
    from app.agent.static_analysis import analyze_project, analysis_to_dict
    root = _task_git_cwd.get()
    if not root:
        return None, "ERROR: No task git working directory configured."
    with _sa_cache_lock:
        if root in _sa_cache:
            return _sa_cache[root], None
    # Collect Python files
    py_files: list[str] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune excluded dirs
        dirnames[:] = [d for d in dirnames if d not in LISTING_EXCLUDED_DIRS and not d.startswith(".")]
        for f in filenames:
            if f.endswith(".py"):
                py_files.append(os.path.join(dirpath, f))
    if not py_files:
        return None, "No .py files found in project root."
    try:
        analysis = analyze_project(py_files)
        with _sa_cache_lock:
            _sa_cache[root] = analysis
        return analysis, None
    except Exception as exc:
        return None, f"ERROR: static analysis failed: {exc}"


def invalidate_sa_cache() -> None:
    """Invalidate the static analysis cache for the current task root (called after write_file)."""
    root = _task_git_cwd.get()
    if root:
        with _sa_cache_lock:
            _sa_cache.pop(root, None)


def find_symbol(name: str, kind: str = "any") -> str:
    """[READ] Find function/class definitions by name using tree-sitter. kind: function|class|any. No state change."""
    analysis, err = _get_project_analysis()
    if err:
        return err
    results: list[str] = []
    for path, file_analysis in analysis.files.items():
        if kind in ("function", "any"):
            for fn in file_analysis.functions:
                if fn.name == name or name.lower() in fn.name.lower():
                    results.append(f"{path}:{getattr(fn, 'line', '?')} function {fn.name}")
        if kind in ("class", "any"):
            for cls in file_analysis.classes:
                if cls.name == name or name.lower() in cls.name.lower():
                    results.append(f"{path}:{getattr(cls, 'line', '?')} class {cls.name}")
                for method in getattr(cls, 'methods', []):
                    method_name = method if isinstance(method, str) else method.name
                    method_line = getattr(method, 'line_start', getattr(cls, 'line_start', '?'))
                    if method_name == name or name.lower() in method_name.lower():
                        results.append(f"{path}:{method_line} method {cls.name}.{method_name}")
    return "\n".join(results) if results else f"No symbol '{name}' found (kind={kind})."


def find_callers(symbol: str) -> str:
    """[READ] Find files that import or likely call the given symbol, using the static analysis import graph. No state change."""
    analysis, err = _get_project_analysis()
    if err:
        return err
    results: list[str] = []
    sym_lower = symbol.lower()
    for path, file_analysis in analysis.files.items():
        for imp in file_analysis.imports:
            if sym_lower in imp.lower():
                results.append(f"{path}: imports '{imp}'")
    return "\n".join(results) if results else f"No import references to '{symbol}' found."


def find_imports_of(module_path: str) -> str:
    """[READ] Find all files that import the given module (by relative path or module name). No state change."""
    analysis, err = _get_project_analysis()
    if err:
        return err
    # Normalize: 'app/agent/tools.py' → 'app.agent.tools' or 'tools'
    module_name = module_path.replace("/", ".").replace("\\", ".").rstrip(".py")
    if module_name.endswith(".py"):
        module_name = module_name[:-3]
    results: list[str] = []
    for path, file_analysis in analysis.files.items():
        for imp in file_analysis.imports:
            if module_name in imp:
                results.append(f"{path}: imports '{imp}'")
    # Also check reverse_imports graph if available
    ri = getattr(analysis, "reverse_imports", {})
    for key, importers in ri.items():
        if module_name in key:
            for importer in importers:
                entry = f"{importer}: imports '{key}' (graph)"
                if entry not in results:
                    results.append(entry)
    return "\n".join(results) if results else f"No imports of '{module_path}' found."


def read_test_summary() -> str:
    """[READ] Parse the most recent run_test_pytest output into {passed, failed, errors, failing_names}. No state change."""
    output = _last_test_output.get("")
    if not output:
        return "No pytest output recorded. Run run_test_pytest first."
    import re as _re
    passed = failed = errors = 0
    failing_names: list[str] = []
    for line in output.splitlines():
        # Use regex to extract counts — pytest decorates the summary line with
        # "=== N passed in Xs ===" so int(line.split()[0]) always gives "===".
        m = _re.search(r'(\d+) passed', line)
        if m:
            passed = int(m.group(1))
        m = _re.search(r'(\d+) failed', line)
        if m:
            failed = int(m.group(1))
        m = _re.search(r'(\d+) error', line, _re.IGNORECASE)
        if m:
            errors = int(m.group(1))
        if line.startswith("FAILED "):
            failing_names.append(line[7:].strip())
    import json as _json
    return _json.dumps({
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failing_names": failing_names,
    }, indent=2)


def get_system_health(head: int | None = None, tail: int | None = None, grep: str | None = None) -> str:
    """[READ] Returns a comprehensive report on system health, including log tail, stagnant tasks, and stuck jobs. head/tail/grep filter output."""
    import datetime as _dt_mod
    from app.database import SessionLocal, Task, TransitionResult, ResearchJob, FileSummaryJob, ArchGenJob
    from sqlalchemy import func

    report_lines = ["== SYSTEM HEALTH REPORT =="]

    # 1. Log Tail
    log_path = os.path.join(PROJECT_ROOT, "logs", "maestro.log")
    if os.path.exists(log_path):
        report_lines.append("\n-- Recent Log Entries (Last 50 lines) --")
        try:
            with open(log_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
                for line in lines[-50:]:
                    report_lines.append(line.rstrip())
        except Exception as exc:
            report_lines.append(f"Error reading log: {exc}")
    else:
        report_lines.append(f"\nLog file not found at {log_path}")

    # 2. Stagnant Tasks (>24h)
    report_lines.append("\n-- Stagnant Tasks (>24h since last progress) --")
    try:
        db = SessionLocal()
        try:
            # Subquery to get latest transition per task
            subq = (
                db.query(TransitionResult.task_id, func.max(TransitionResult.created_at).label("max_ca"))
                  .join(Task, Task.id == TransitionResult.task_id)
                  .filter(Task.is_active == True)
                  .group_by(TransitionResult.task_id)
                  .subquery()
            )
            threshold = _dt_mod.datetime.now() - _dt_mod.timedelta(hours=24)
            stagnant = db.query(Task.id, Task.title, Task.type, subq.c.max_ca)\
                         .join(subq, subq.c.task_id == Task.id)\
                         .filter(subq.c.max_ca < threshold)\
                         .order_by(subq.c.max_ca.asc())\
                         .limit(20).all()
            if not stagnant:
                report_lines.append("No stagnant tasks detected.")
            for tid, title, ttype, last_at in stagnant:
                # Handle last_at as string or datetime
                if isinstance(last_at, str):
                    try:
                        last_at = _dt_mod.datetime.fromisoformat(last_at.replace(" ", "T").replace("Z", "+00:00"))
                    except ValueError:
                        last_at = _dt_mod.datetime.now()
                if last_at.tzinfo is None:
                    last_at = last_at.replace(tzinfo=_dt_mod.timezone.utc)
                age_h = (_dt_mod.datetime.now(_dt_mod.timezone.utc) - last_at).total_seconds() / 3600
                report_lines.append(f"  [{tid}] {title} ({ttype}) - stuck for {age_h:.1f}h")
        finally:
            db.close()
    except Exception as exc:
        report_lines.append(f"Error checking stagnant tasks: {exc}")

    # 3. Stuck Jobs
    report_lines.append("\n-- Stuck Background Jobs (running but old) --")
    try:
        db = SessionLocal()
        try:
            job_count = 0
            now = _dt_mod.datetime.now(_dt_mod.timezone.utc)
            for model_cls in [ResearchJob, FileSummaryJob, ArchGenJob]:
                # 30 minute threshold for "stuck" jobs
                threshold = _dt_mod.datetime.now() - _dt_mod.timedelta(minutes=30)
                stuck = db.query(model_cls).filter(model_cls.status == 'running', model_cls.created_at < threshold).all()
                for job in stuck:
                    ca = job.created_at
                    if isinstance(ca, str):
                        try:
                            ca = _dt_mod.datetime.fromisoformat(ca.replace(" ", "T").replace("Z", "+00:00"))
                        except ValueError:
                            ca = now
                    if ca.tzinfo is None:
                        ca = ca.replace(tzinfo=_dt_mod.timezone.utc)
                    age_m = (now - ca).total_seconds() / 60
                    report_lines.append(f"  [{model_cls.__name__}] id={job.id} task_id={getattr(job, 'task_id', 'N/A')} age={age_m:.1f}m")
                    job_count += 1
            if job_count == 0:
                report_lines.append("No stuck background jobs.")
        finally:
            db.close()
    except Exception as exc:
        report_lines.append(f"Error checking background jobs: {exc}")

    return _slice_output("\n".join(report_lines), head=head, tail=tail, grep=grep)


# ---------------------------------------------------------------------------
# Document store tools
# ---------------------------------------------------------------------------

def _doc_project_id() -> int | None:
    """Resolve the current agent's project_id from context."""
    project_name = _task_project_name.get()
    if not project_name:
        # Fallback: derive project from task_id context (handles rapid-retry sessions
        # where _task_project_name was not yet set by the dispatcher).
        task_id = _task_id_ctx.get()
        if task_id:
            from app.database import get_task as _gt
            task = _gt(task_id)
            if task and task.project:
                _task_project_name.set(task.project)
                project_name = task.project
    if not project_name:
        return None
    from app.database.session import SessionLocal
    from app.database.models import Project
    with SessionLocal() as db:
        row = db.query(Project).filter(Project.name == project_name).first()
        return row.id if row else None


def tool_store_document(key: str, content: str, tags: list | None = None) -> str:
    import json as _json
    pid = _doc_project_id()
    if pid is None:
        return "ERROR: No project context — cannot store document."
    task_id = _task_id_ctx.get()
    from app.database.crud_documents import store_document as _store
    doc = _store(pid, key, content, list(tags) if tags else None, task_id)
    return f"OK: document stored — key={doc['key']!r} size={len(content.encode())} bytes"


def tool_get_document(key: str) -> str:
    import json as _json
    pid = _doc_project_id()
    if pid is None:
        return "ERROR: No project context — cannot retrieve document."
    from app.database.crud_documents import get_document as _get
    doc = _get(pid, key)
    if doc is None:
        return f"NOT FOUND: No document with key {key!r}"
    return (
        f"key: {doc['key']}\n"
        f"written_by: {doc['written_by_task_id'] or 'human'}\n"
        f"updated_at: {doc['updated_at']}\n"
        f"tags: {doc['tags']}\n"
        f"---\n{doc['content']}"
    )


def tool_search_documents(query: str, threshold: float = 0.3) -> str:
    import json as _json
    pid = _doc_project_id()
    if pid is None:
        return "ERROR: No project context — cannot search documents."
    from app.database.crud_documents import fuzzy_get_document as _fuzzy
    results = _fuzzy(pid, query, threshold)
    if not results:
        return f"No documents found matching {query!r} (threshold={threshold})"
    lines = [f"Found {len(results)} result(s) for {query!r}:"]
    for r in results:
        size = len((r.get("content") or "").encode())
        lines.append(
            f"  [{r['similarity']:.2f}] {r['key']} "
            f"({size} bytes, written_by={r['written_by_task_id'] or 'human'})"
        )
    return "\n".join(lines)


def tool_list_documents(tag: str | None = None) -> str:
    pid = _doc_project_id()
    if pid is None:
        return "ERROR: No project context — cannot list documents."
    from app.database.crud_documents import list_documents as _list
    docs = _list(pid, tag)
    if not docs:
        prefix = f"[tag={tag}] " if tag else ""
        return f"{prefix}No documents in project store."
    lines = [f"{len(docs)} document(s)" + (f" tagged {tag!r}" if tag else "") + ":"]
    for d in docs:
        lines.append(
            f"  {d['key']}  ({d['content_size_bytes']} bytes)"
            f"  written_by={d['written_by_task_id'] or 'human'}"
            f"  tags={d['tags']}"
            f"  updated={d['updated_at']}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Episodic memory tools (Gap 7)
# ---------------------------------------------------------------------------

def handle_query_episodes(
    question: str,
    k: int = 5,
    episode_type: "str | None" = None,
) -> str:
    """[READ] Search episodic memory for past attempts, failures, or conclusions."""
    import json as _json
    import app.agent.config as _cfg

    if not _cfg.EPISODIC_MEMORY_ENABLED:
        return _json.dumps({"episodes": [], "note": "Episodic memory is not enabled."})

    pid = _doc_project_id()
    if pid is None:
        return _json.dumps({"error": "No project context for episodic memory query."})

    k = max(1, min(int(k), 20))

    from app.agent.episodic_memory import query_episodes
    episodes = query_episodes(
        project_id=pid,
        question=question,
        k=k,
        settings=_cfg,
        episode_type=episode_type or None,
    )

    result = []
    for ep in episodes:
        result.append({
            "episode_type": ep["episode_type"],
            "content": ep["content"],
            "created_at": ep["created_at"].isoformat() if ep.get("created_at") else None,
            "metadata": ep.get("metadata", {}),
            "relevance_score": round(ep.get("relevance_score", 0.0), 4),
        })

    return _json.dumps({"episodes": result, "count": len(result)}, indent=2)


# ---------------------------------------------------------------------------
# Autopilot objective tools (Gap 4)
# ---------------------------------------------------------------------------

def tool_get_objective_detail(objective_id: int) -> str:
    """[READ] Return full details of an autopilot objective including its direct children."""
    import json as _json
    from app.database import get_objective, list_objectives, objective_to_dict
    obj = get_objective(objective_id)
    if not obj:
        return _json.dumps({"error": "objective not found"})
    detail = objective_to_dict(obj)
    children = list_objectives(obj.project_id, status=None, parent_id=objective_id)
    detail["children"] = [objective_to_dict(c) for c in children]
    return _json.dumps(detail, indent=2)


def tool_get_objective_evidence(objective_id: int) -> str:
    """[READ] Return the full evidence log for an autopilot objective."""
    from app.database import get_objective_evidence
    return get_objective_evidence(objective_id)


def tool_append_objective_evidence(objective_id: int, entry: str) -> str:
    """[WRITE] Append a timestamped note to an objective's evidence log."""
    from app.database import append_objective_evidence
    ok = append_objective_evidence(objective_id, entry)
    return "ok" if ok else "error: objective not found"


def tool_list_objectives(status: str = "active") -> str:
    """[READ] List autopilot objectives for the current project."""
    import json as _json
    pid = _doc_project_id()
    if pid is None:
        return "ERROR: No project context — cannot list objectives."
    from app.database import list_objectives, objective_to_dict
    normalized_status: str | None = status if status != "all" else None
    objs = list_objectives(pid, status=normalized_status)
    return _json.dumps([objective_to_dict(o) for o in objs], indent=2)


# ---------------------------------------------------------------------------
# Pipeline management tools
# ---------------------------------------------------------------------------


def list_pipelines() -> str:
    """[READ] List all pipeline templates with stage summaries."""
    import json
    from app.database.crud_malleable import get_all_templates, get_stages_for_template
    templates = get_all_templates()
    result = []
    for t in templates:
        stages = get_stages_for_template(t.id)
        result.append({
            "id": t.id,
            "name": t.name,
            "description": t.description,
            "is_default": t.is_default,
            "is_builtin": t.is_builtin,
            "version": t.version,
            "stages": [
                {
                    "id": s.id,
                    "stage_key": s.stage_key,
                    "label": s.label,
                    "agent_type": s.agent_type,
                    "position": s.position,
                    "has_system_prompt": bool((s.config or {}).get("system_prompt")),
                    "tool_allowlist": (s.config or {}).get("tool_allowlist") or [],
                }
                for s in stages
            ],
        })
    return json.dumps(result, indent=2)


def get_pipeline(template_id: int) -> str:
    """[READ] Get full details of a pipeline template: stages, transitions, groups, arch categories."""
    import json
    from app.database.crud_malleable import get_template, template_to_dict
    t = get_template(template_id)
    if not t:
        return f"ERROR: No pipeline template with id={template_id}"
    return json.dumps(template_to_dict(t), indent=2)


def clone_pipeline(template_id: int, new_name: str) -> str:
    """[WRITE] Deep-copy a pipeline template under a new name. The clone is never builtin."""
    import json
    from app.database.crud_malleable import clone_template
    result = clone_template(template_id, new_name)
    if not result:
        return (
            f"ERROR: Failed to clone template id={template_id}. "
            f"The name '{new_name}' may already be taken, or the source template does not exist."
        )
    return json.dumps({
        "id": result.id,
        "name": result.name,
        "message": f"Pipeline cloned successfully as '{result.name}' (id={result.id}).",
    })


def update_pipeline(
    template_id: int,
    name: str | None = None,
    description: str | None = None,
    is_default: bool | None = None,
) -> str:
    """[WRITE] Update pipeline template metadata (name, description, is_default)."""
    import json
    from app.database.crud_malleable import update_template
    result = update_template(
        template_id,
        name=name,
        description=description,
        is_default=is_default,
        version_bump=True,
    )
    if not result:
        return f"ERROR: Failed to update pipeline template id={template_id}"
    return json.dumps({"id": result.id, "name": result.name, "version": result.version, "message": "Pipeline updated."})


def update_pipeline_stage(
    stage_id: int,
    label: str | None = None,
    agent_type: str | None = None,
    system_prompt: str | None = None,
    tool_allowlist: list | None = None,
    intent: str | None = None,
    gate: str | None = None,
    retries: int | None = None,
    verifier: str | None = None,
    extra_config: dict | None = None,
) -> str:
    """[WRITE] Update a pipeline stage. Merges config keys — does not replace the whole config.
    system_prompt, tool_allowlist, intent, gate, retries, verifier live inside the stage config dict.
    extra_config is merged last (use for any config key not listed above).
    """
    import json
    from app.database.crud_malleable import get_stage_by_id, update_stage
    s = get_stage_by_id(stage_id)
    if not s:
        return f"ERROR: No stage with id={stage_id}"

    # Merge into existing config rather than overwrite
    config = dict(s.config or {})
    if system_prompt is not None:
        config["system_prompt"] = system_prompt
    if tool_allowlist is not None:
        config["tool_allowlist"] = tool_allowlist
    if intent is not None:
        config["intent"] = intent
    if gate is not None:
        config["gate"] = gate
    if retries is not None:
        config["retries"] = retries
    if verifier is not None:
        config["verifier"] = verifier
    if extra_config:
        config.update(extra_config)

    result = update_stage(
        stage_id,
        label=label,
        agent_type=agent_type,
        config=config,
    )
    if not result:
        return f"ERROR: Failed to update stage id={stage_id}"
    updated_cfg = result.config or {}
    return json.dumps({
        "id": result.id,
        "stage_key": result.stage_key,
        "label": result.label,
        "agent_type": result.agent_type,
        "config_keys": sorted(updated_cfg.keys()),
        "tool_allowlist": updated_cfg.get("tool_allowlist") or [],
        "has_system_prompt": bool(updated_cfg.get("system_prompt")),
        "message": "Stage updated.",
    })


def assign_project_pipeline(project_name: str, template_id: int) -> str:
    """[WRITE] Assign a pipeline template to a project.
    Tasks whose stage_key no longer exists in the new template are migrated to the first stage.
    """
    import json
    from app.database import get_project, upsert_project, get_tasks_by_project, update_task
    from app.database.crud_malleable import get_template, get_stages_for_template

    project = get_project(project_name)
    if not project:
        return f"ERROR: Project '{project_name}' not found."
    template = get_template(template_id)
    if not template:
        return f"ERROR: Pipeline template id={template_id} not found."

    stages = get_stages_for_template(template_id)
    valid_keys = {s.stage_key for s in stages}
    fallback_key = stages[0].stage_key if stages else None

    migrated = 0
    if fallback_key:
        tasks = get_tasks_by_project(project_name)
        for task in tasks:
            sk = getattr(task, "stage_key", None) or task.type
            if sk not in valid_keys:
                update_task(task.id, stage_key=fallback_key, type=fallback_key)
                migrated += 1

    upsert_project(project_name, pipeline_template_id=template_id)
    return json.dumps({
        "project": project_name,
        "template_id": template.id,
        "template_name": template.name,
        "migrated_tasks": migrated,
        "message": f"Project '{project_name}' now uses pipeline '{template.name}'. {migrated} card(s) migrated to '{fallback_key}'.",
    })


def transfer_pipeline_cards(
    from_template_id: int,
    to_template_id: int,
    stage_map: dict,
    project_name: str | None = None,
) -> str:
    """[WRITE] Move cards from one pipeline to another using an explicit stage key map.
    stage_map: {"old_stage_key": "new_stage_key", ...}  — unmapped stage keys are left untouched.
    project_name: optional filter to only move cards for a specific project.
    Use get_pipeline to inspect stage keys before mapping.
    """
    import json
    from app.database.crud_malleable import compute_stage_map, transfer_cards, get_template
    from app.database import get_project

    src = get_template(from_template_id)
    dst = get_template(to_template_id)
    if not src:
        return f"ERROR: Source pipeline id={from_template_id} not found."
    if not dst:
        return f"ERROR: Destination pipeline id={to_template_id} not found."

    project_id = None
    if project_name:
        proj = get_project(project_name)
        if not proj:
            return f"ERROR: Project '{project_name}' not found."
        project_id = proj.id

    count = transfer_cards(from_template_id, to_template_id, stage_map, project_id=project_id)
    return json.dumps({
        "from_pipeline": src.name,
        "to_pipeline": dst.name,
        "stage_map": stage_map,
        "cards_moved": count,
        "message": f"Transferred {count} card(s) from '{src.name}' to '{dst.name}'.",
    })


# ---------------------------------------------------------------------------
# Log analysis tools
# ---------------------------------------------------------------------------


def read_log_window(
    hours: int = 1,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[READ] Read log entries from the past N hours with anomaly pattern counts.
    head/tail/grep filter the log lines returned. head/tail/grep filter output.
    """
    import datetime as _dt
    import re as _re

    log_path = os.path.join(PROJECT_ROOT, "logs", "maestro.log")
    if not os.path.exists(log_path):
        return f"No log file at {log_path}"

    cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=hours)

    # Patterns that indicate anomalies
    ANOMALY_PATTERNS = {
        "ERROR": _re.compile(r"\bERROR\b", _re.IGNORECASE),
        "CRITICAL": _re.compile(r"\bCRITICAL\b", _re.IGNORECASE),
        "WARNING": _re.compile(r"\bWARNING\b", _re.IGNORECASE),
        "finish_reason=length": _re.compile(r"finish_reason.*length|reason.*length", _re.IGNORECASE),
        "ContextTooLarge": _re.compile(r"ContextTooLargeError|context.*too.*large", _re.IGNORECASE),
        "tool_failure": _re.compile(r"tool.*fail|consecutive.*fail|REVERT_TO_DESIGN", _re.IGNORECASE),
        "timeout": _re.compile(r"\btimeout\b|\btimed out\b", _re.IGNORECASE),
        "stage_thrash": _re.compile(r"demotion|demoted|reverted.*stage|REVERT", _re.IGNORECASE),
        "zombie": _re.compile(r"zombie|orphan.*session|idle.*session", _re.IGNORECASE),
        "DB_lock": _re.compile(r"deadlock|lock.*timeout|locked.*table|OperationalError", _re.IGNORECASE),
    }

    # Timestamp pattern (ISO-like): 2025-01-15 12:34:56
    TS_RE = _re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})")

    counts: dict[str, int] = {k: 0 for k in ANOMALY_PATTERNS}
    matched_lines: list[str] = []
    total_lines = 0
    in_window = False

    try:
        with open(log_path, "r", encoding="utf-8", errors="replace") as fh:
            for raw in fh:
                line = raw.rstrip()
                m = TS_RE.match(line)
                if m:
                    ts_str = m.group(1).replace("T", " ")
                    try:
                        ts = _dt.datetime.fromisoformat(ts_str).replace(tzinfo=_dt.timezone.utc)
                        in_window = ts >= cutoff
                    except ValueError:
                        pass
                if not in_window:
                    continue
                total_lines += 1
                for name, pat in ANOMALY_PATTERNS.items():
                    if pat.search(line):
                        counts[name] += 1
                matched_lines.append(line)
    except OSError as exc:
        return f"ERROR reading log: {exc}"

    anomaly_summary = "  ".join(
        f"{k}={v}" for k, v in counts.items() if v > 0
    ) or "none"

    header = (
        f"== LOG WINDOW: last {hours}h | {total_lines} lines | anomalies: {anomaly_summary} =="
    )
    body = _slice_output("\n".join(matched_lines), head=head, tail=tail, grep=grep)
    return _cap_tool_result("read_log_window", f"{header}\n{body}")


def get_budget_history(
    task_id: str | None = None,
    hours: int = 4,
    limit: int = 50,
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[READ] Summarize recent LLM call history from budget_entries.
    Shows finish_reason breakdown, token counts, agent names, and error patterns.
    task_id: filter to one task (omit for all tasks).
    hours: how far back to look (default 4).
    limit: max rows to include in detail table (default 50).
    head/tail/grep filter the detail table output.
    """
    import json as _json
    import datetime as _dt
    from app.database import SessionLocal
    from app.database.models import BudgetEntry

    cutoff = _dt.datetime.utcnow() - _dt.timedelta(hours=hours)
    db = SessionLocal()
    try:
        q = db.query(BudgetEntry).filter(BudgetEntry.created_at >= cutoff)
        if task_id:
            q = q.filter(BudgetEntry.task_id == task_id)
        entries = q.order_by(BudgetEntry.created_at.asc()).all()
    finally:
        db.close()

    if not entries:
        scope = f"task {task_id}" if task_id else "all tasks"
        return f"No budget entries in the last {hours}h for {scope}."

    finish_reasons: dict[str, int] = {}
    agent_counts: dict[str, int] = {}
    total_prompt = 0
    total_gen = 0
    detail_rows: list[str] = []

    for e in entries:
        # Extract finish_reason from response_data JSON
        finish_reason = "?"
        if e.response_data:
            try:
                resp = _json.loads(e.response_data)
                choices = resp.get("choices") or []
                if choices:
                    finish_reason = choices[0].get("finish_reason") or "?"
            except Exception:
                pass
        finish_reasons[finish_reason] = finish_reasons.get(finish_reason, 0) + 1

        agent = e.agent_name or "unknown"
        agent_counts[agent] = agent_counts.get(agent, 0) + 1
        total_prompt += e.prompt_cost or 0
        total_gen += e.generation_cost or 0

        ts = (e.created_at or _dt.datetime.utcnow()).isoformat()[:16]
        detail_rows.append(
            f"{ts}  task={e.task_id or '-':12s}  agent={agent:22s}  "
            f"finish={finish_reason:8s}  prompt={e.prompt_cost or 0:6d}  gen={e.generation_cost or 0:6d}"
        )

    reason_str = "  ".join(f"{k}={v}" for k, v in sorted(finish_reasons.items(), key=lambda x: -x[1]))
    agent_str = "  ".join(f"{k}={v}" for k, v in sorted(agent_counts.items(), key=lambda x: -x[1]))

    summary = (
        f"== BUDGET HISTORY: last {hours}h | {len(entries)} calls | "
        f"total prompt={total_prompt} gen={total_gen} ==\n"
        f"finish_reason: {reason_str}\n"
        f"agents: {agent_str}\n"
        f"--- detail (newest last, capped at {limit} rows) ---"
    )
    detail = _slice_output("\n".join(detail_rows[-limit:]), head=head, tail=tail, grep=grep)
    return _cap_tool_result("get_budget_history", f"{summary}\n{detail}")


def get_task_history_recent(task_id: str, max_turns: int = 20) -> str:
    """[READ] Return the most recent N LLM turns for a task as a JSON list.

    Each entry includes entry_id, agent_name, created_at, prompt_tokens,
    completion_tokens, finish_reason, and a 500-char content_preview of the
    assistant message.  max_turns is clamped to [1, 50].
    """
    import json as _json
    from app.database import get_budget_entries

    max_turns = max(1, min(50, int(max_turns)))
    entries = get_budget_entries(task_id=str(task_id), limit=max_turns)
    result = []
    for e in entries:
        content_preview = ""
        finish_reason = ""
        try:
            resp = _json.loads(e.response_data or "{}")
            choices = resp.get("choices") or []
            if choices:
                content = (choices[0].get("message") or {}).get("content") or ""
                content_preview = content[:500]
                finish_reason = choices[0].get("finish_reason") or ""
        except Exception:
            pass
        result.append({
            "entry_id": e.id,
            "agent_name": e.agent_name,
            "created_at": e.created_at.isoformat() if e.created_at else None,
            "prompt_tokens": e.prompt_cost,
            "completion_tokens": e.generation_cost,
            "finish_reason": finish_reason,
            "content_preview": content_preview,
        })
    return _cap_tool_result("get_task_history_recent", _json.dumps(result, indent=2))


# ---------------------------------------------------------------------------
# Math tools
# ---------------------------------------------------------------------------

def run_sympy(code: str, timeout: int = 120) -> str:
    """[RUN — docker-sandbox] Execute Python/SymPy code for mathematical exploration. Returns stdout and stderr from an isolated Docker container. Use for scratch computation; commit final results via write_file + run_test_pytest."""
    timeout = max(10, min(600, int(timeout)))
    from app.agent.sandbox import run_in_sandbox
    result = run_in_sandbox(code, lang="python", timeout=timeout)
    if "error" in result and "stdout" not in result:
        return f"[run_sympy] Error: {result['error']}"
    parts = []
    out = result.get("stdout", "")[:8192]
    err = result.get("stderr", "")[:8192]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    if result.get("timed_out"):
        parts.append("[timed out]")
    elif result.get("ok") is False:
        parts.append("[process exited with non-zero code]")
    return "\n".join(parts) or "[no output]"


def run_lean4(source: str, timeout: int = 120) -> str:
    """[RUN — docker-sandbox] Compile Lean4 source against Mathlib in an isolated Docker container. Returns stdout/stderr. Exit code 0 means the file compiles with no errors and no sorry placeholders."""
    timeout = max(30, min(600, int(timeout)))
    from app.agent.sandbox import run_in_sandbox
    result = run_in_sandbox(source, lang="lean4", timeout=timeout)
    if "error" in result and "stdout" not in result:
        return f"[run_lean4] Error: {result['error']}"
    parts = []
    out = result.get("stdout", "")[:8192]
    err = result.get("stderr", "")[:8192]
    if out:
        parts.append(f"stdout:\n{out}")
    if err:
        parts.append(f"stderr:\n{err}")
    if result.get("timed_out"):
        parts.append("[timed out — lean4 compilation exceeded time limit]")
    elif result.get("ok"):
        parts.append("[compiled successfully — no errors]")
    else:
        parts.append("[compilation failed — non-zero exit code]")
    return "\n".join(parts) or "[no output]"


from app.agent.tools_math import search_arxiv as _search_arxiv_impl, search_oeis as _search_oeis_impl, search_mathlib as _search_mathlib_impl, list_mathlib_topics as _list_mathlib_topics_impl  # noqa: E402


def _get_lean4_proof_state_tool(lean_source: str, line: int, col: int = 0) -> str:
    """[RUN — docker-sandbox] Get Lean4 proof state at a line. Returns JSON."""
    import json as _json
    from app.agent.sandbox import get_lean4_proof_state
    result = get_lean4_proof_state(lean_source, line, col)
    return _json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------------------
# Gap 5 — vote_to_revert tool
# ---------------------------------------------------------------------------

def handle_vote_to_revert(reason: str) -> str:
    """Cast a vote to revert the most recent self-modification merge commit."""
    from app.database import (
        cast_revert_vote, get_revert_votes,
        get_latest_self_mod_merge, mark_self_mod_reverted,
    )
    from app.database.crud_tasks import create_pip

    project_name = _task_project_name.get() or ""
    if project_name != SELF_MODIFICATION_PROJECT:
        return (
            f"[vote_to_revert] ERROR: This tool is only available in the "
            f"'{SELF_MODIFICATION_PROJECT}' project (current: {project_name!r})."
        )

    merge_commit = get_latest_self_mod_merge()
    if not merge_commit:
        return "[vote_to_revert] ERROR: No self-modification merge recorded yet. Nothing to revert."

    task_id = _task_id_ctx.get() or ""
    vote_count = cast_revert_vote(task_id, merge_commit, reason)
    threshold = SELF_MOD_REVERT_VOTE_THRESHOLD

    if vote_count < threshold:
        return (
            f"[vote_to_revert] Vote recorded. {vote_count}/{threshold} votes needed "
            f"to auto-revert merge {merge_commit[:8]}."
        )

    # Threshold reached — execute revert
    import subprocess as _sp
    revert_result = _sp.run(
        ["git", "revert", merge_commit, "--no-edit"],
        capture_output=True, text=True, timeout=60,
        cwd=MAESTRO_GIT_ROOT or PROJECT_ROOT,
    )
    if revert_result.returncode != 0:
        return (
            f"[vote_to_revert] Threshold reached ({vote_count}/{threshold}) but "
            f"git revert failed:\n{revert_result.stderr}"
        )

    # Create PIP card documenting the revert
    votes = get_revert_votes(merge_commit)
    vote_log = "\n".join(
        f"- task {v['task_id']} at {v['created_at']}: {v['reason']}" for v in votes
    )
    pip_desc = (
        f"AUTO-REVERT triggered for merge {merge_commit[:8]}.\n\n"
        f"Vote log ({vote_count} votes):\n{vote_log}"
    )
    try:
        create_pip(
            task_id=task_id,
            origin_stage="self_modification",
            reason=pip_desc,
        )
    except Exception:
        pass  # PIP creation failure must not block the revert confirmation

    mark_self_mod_reverted(merge_commit)
    return (
        f"[vote_to_revert] AUTO-REVERT COMPLETE. Merge {merge_commit[:8]} has been "
        f"reverted on {SELF_MOD_INTEGRATION_BRANCH}. A PIP card was created."
    )


# ---------------------------------------------------------------------------
# Event watch tools (GAP 9)
# ---------------------------------------------------------------------------

def handle_register_watch(
    event_type: str,
    label: str,
    source_config: dict,
    fire_config: dict | None = None,
    *,
    task_id: str | None = None,
    **_,
) -> str:
    from app.database.crud_events import create_watch
    from app.database import get_task as _get_task

    project_id = None
    if task_id:
        task = _get_task(task_id)
        if task:
            project_id = task.project_id
    if project_id is None:
        return "[register_watch] ERROR: could not resolve project_id from task_id"

    watch = create_watch(
        project_id=project_id,
        event_type=event_type,
        label=label,
        source_config=source_config,
        fire_config=fire_config or {},
    )
    if not watch:
        return "[register_watch] ERROR: DB insert failed"

    if event_type == "file_watch":
        from app.agent.file_watcher import get_file_watcher
        fw = get_file_watcher()
        if fw:
            fw.add_watch(watch)

    result = {"watch_id": watch.id, "event_type": event_type, "label": label}
    if event_type == "webhook":
        result["inbound_url"] = f"/api/events/inbound/{watch.id}"
    elif event_type == "api_poll":
        result["message"] = "Will fire on next scheduler tick when interval elapses"

    import json as _json
    return _json.dumps(result)


def handle_list_watches(
    status: str = "active",
    *,
    task_id: str | None = None,
    **_,
) -> str:
    import json as _json
    from app.database.crud_events import list_watches
    from app.database import get_task as _get_task

    project_id = None
    if task_id:
        task = _get_task(task_id)
        if task:
            project_id = task.project_id

    watches = list_watches(project_id=project_id, status=status)
    rows = [
        {
            "id": w.id,
            "event_type": w.event_type,
            "label": w.label,
            "status": w.status,
            "fire_count": w.fire_count,
            "last_fired_at": str(w.last_fired_at) if w.last_fired_at else None,
        }
        for w in watches
    ]
    return _json.dumps(rows)


# ---------------------------------------------------------------------------
# Tool registry + schemas
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    # File read tools
    "read_file": read_file,
    "read_file_metadata": read_file_metadata,
    "read_last_output": read_last_output,
    # File write tools
    "write_file": write_file,
    "append_file": append_file,
    "patch_file": patch_file,
    "move_file": move_file,
    "write_archive": write_archive,
    "workspace_delete_file": workspace_delete_file,
    "workspace_rename_file": workspace_rename_file,
    # Directory / search tools
    "list_directory": list_directory,
    "find_files": find_files,
    "find_in_files": find_in_files,
    # Static analysis helpers
    "find_symbol": find_symbol,
    "find_callers": find_callers,
    "find_imports_of": find_imports_of,
    # Git diff/stat helpers
    "read_diff_stat": read_diff_stat,
    # Test summary helper
    "read_test_summary": read_test_summary,
    # Git read tools
    "read_git_status": read_git_status,
    "read_git_diff": read_git_diff,
    "read_git_log": read_git_log,
    "read_git_blame": read_git_blame,
    "read_git_show": read_git_show,
    # Git write tools
    "write_git_branch": write_git_branch,
    "write_git_commit": write_git_commit,
    "write_git_checkout": write_git_checkout,
    "write_git_restore": write_git_restore,
    # Task DB tools
    "get_task": get_task,
    "list_tasks": list_tasks,
    "write_task_status": write_task_status,
    "write_task_history": write_task_history,
    "batch_create_cards": batch_create_cards,
    "write_plan_fields": write_plan_fields,
    # Web tools
    "web_search": web_search,
    "web_fetch": web_fetch,
    # Architecture/planning write tools
    "write_arch_doc": write_arch_doc,
    "write_mermaid": write_mermaid,
    "write_interface_contract": write_interface_contract,
    "write_benchmark": write_benchmark,
    # Research agents
    "spawn_research_agent": spawn_research_agent,
    "launch_research_agent": launch_research_agent,
    # Test/lint (sandbox — no project mutation)
    "run_test_pytest": run_test_pytest,
    "run_check_mypy": run_check_mypy,
    "run_check_ruff": run_check_ruff,
    "run_check_black": run_check_black,
    "run_test_unittest": run_test_unittest,
    "run_test_npm": run_test_npm,
    "run_test_cargo": run_test_cargo,
    "run_test_go": run_test_go,
    # Build tools (write build artifacts)
    "run_build_make": run_build_make,
    "run_build_cargo": run_build_cargo,
    "run_build_go": run_build_go,
    "run_build_npm": run_build_npm,
    "run_build_tsc": run_build_tsc,
    "run_build_gradle": run_build_gradle,
    "run_build_mvn": run_build_mvn,
    # Dependency tools (mutate environment)
    "run_deps_pip": run_deps_pip,
    "run_deps_npm": run_deps_npm,
    "run_deps_cargo": run_deps_cargo,
    # Security audit tools
    "run_audit_bandit": run_audit_bandit,
    "run_audit_pip": run_audit_pip,
    "run_audit_semgrep": run_audit_semgrep,
    "run_audit_npm": run_audit_npm,
    # Diagnostic / terminal tools
    "report_tool_bug": report_tool_bug,
    "submit_work": submit_work,
    "get_system_health": get_system_health,
    "consult_maestro": consult_maestro,
    # Survey/project summary tools
    "get_project_summary": get_project_summary,
    "get_directory_summary": get_directory_summary,
    "get_module_summary": get_module_summary,
    "list_scope_summaries": list_scope_summaries,
    # Infrastructure remediation tools (Maestro exclusive)
    "cleanup_ghost_worktrees": cleanup_ghost_worktrees,
    "restart_server": restart_server,
    # Pipeline management tools (Maestro exclusive)
    "list_pipelines": list_pipelines,
    "get_pipeline": get_pipeline,
    "clone_pipeline": clone_pipeline,
    "update_pipeline": update_pipeline,
    "update_pipeline_stage": update_pipeline_stage,
    "assign_project_pipeline": assign_project_pipeline,
    "transfer_pipeline_cards": transfer_pipeline_cards,
    # Log and diagnostic history tools
    "read_log_window": read_log_window,
    "get_budget_history": get_budget_history,
    "get_task_history_recent": get_task_history_recent,
    # Document store tools
    "store_document": tool_store_document,
    "get_document": tool_get_document,
    "search_documents": tool_search_documents,
    "list_documents": tool_list_documents,
    # Autopilot objective tools (Gap 4)
    "get_objective_detail":       tool_get_objective_detail,
    "get_objective_evidence":     tool_get_objective_evidence,
    "append_objective_evidence":  tool_append_objective_evidence,
    "list_objectives":            tool_list_objectives,
    # Math tools
    "run_sympy": run_sympy,
    "run_lean4": run_lean4,
    "search_arxiv": _search_arxiv_impl,
    "search_oeis": _search_oeis_impl,
    "search_mathlib": _search_mathlib_impl,
    "list_mathlib_topics": _list_mathlib_topics_impl,
    "get_lean4_proof_state": _get_lean4_proof_state_tool,
    # Self-modification tools (Gap 5)
    "vote_to_revert": handle_vote_to_revert,
    # Episodic memory tools (Gap 7)
    "query_episodes": handle_query_episodes,
    # Inter-agent messaging tools (Gap 8)
    # Handlers live in async_dispatch_tool; stubs here satisfy build_tool_schemas lookups.
    "ask_agent": lambda **_: "ERROR: ask_agent requires async dispatch.",
    "list_active_sessions": lambda **_: "ERROR: list_active_sessions requires async dispatch.",
    # Event watch tools (Gap 9)
    "register_watch": handle_register_watch,
    "list_watches_for_project": handle_list_watches,
}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "cleanup_ghost_worktrees",
            "description": (
                "[READ] Scan all projects for orphaned/ghost git worktrees and locked directories. "
                "Use this when the system reports 'worktree lock' or 'directory busy' errors in the log tail."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "restart_server",
            "description": (
                "[RUN] Trigger a hot-restart of the Maestro server. Use this as a LAST RESORT "
                "when logs indicate catastrophic failures or deep-level staleness that cannot be "
                "resolved by killing sessions."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_system_health",
            "description": (
                "[READ] Returns a comprehensive report on system health, including "
                "the log tail (last 50 lines), stagnant tasks (>24h since progress), "
                "and stuck background jobs (running for >30m). Use this to identify "
                "infrastructure-level issues or projects that are failing but not "
                "triggering normal stall signals. head/tail/grep filter output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
            },
        },
    },
    # ---- Pipeline management tools ----
    {
        "type": "function",
        "function": {
            "name": "list_pipelines",
            "description": (
                "[READ] List all pipeline templates with their stages. "
                "Returns id, name, description, is_default, is_builtin, and stage summaries "
                "(stage_key, label, agent_type, tool_allowlist, has_system_prompt). "
                "Start here before editing or cloning a pipeline."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_pipeline",
            "description": (
                "[READ] Get full details of a pipeline template: all stages with their full config "
                "(system_prompt, tool_allowlist, intent, gate, retries, verifier), transitions, "
                "groups, and arch categories. Use this to inspect a pipeline before modifying it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "integer", "description": "Pipeline template ID (from list_pipelines)."},
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "clone_pipeline",
            "description": (
                "[WRITE] Deep-copy a pipeline template under a new name. The clone is editable and "
                "never marked builtin. Use this to safely experiment with a pipeline: clone it, "
                "edit the clone, then assign projects to the clone with assign_project_pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "integer", "description": "Source pipeline template ID."},
                    "new_name":    {"type": "string",  "description": "Name for the new clone (must be unique)."},
                },
                "required": ["template_id", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_pipeline",
            "description": (
                "[WRITE] Update a pipeline template's top-level metadata: name, description, or "
                "is_default flag. Automatically bumps the version. "
                "To change stage behaviour, use update_pipeline_stage instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "template_id": {"type": "integer", "description": "Pipeline template ID."},
                    "name":        {"type": "string",  "description": "New name (optional)."},
                    "description": {"type": "string",  "description": "New description (optional)."},
                    "is_default":  {"type": "boolean", "description": "Make this the default template (optional)."},
                },
                "required": ["template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_pipeline_stage",
            "description": (
                "[WRITE] Update a stage's configuration. Config keys are MERGED — passing "
                "system_prompt only changes that key, leaving others intact. "
                "Use this to fix broken agent prompts, adjust tool allowlists, change agent_type, "
                "set gate/retries/verifier, or update intent descriptions. "
                "Get stage IDs from get_pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "stage_id":      {"type": "integer", "description": "Stage ID (from get_pipeline stages list)."},
                    "label":         {"type": "string",  "description": "Display label for the stage column."},
                    "agent_type":    {"type": "string",  "description": "Agent type key (from agent registry)."},
                    "system_prompt": {"type": "string",  "description": "Full system prompt for the agent at this stage."},
                    "tool_allowlist":{"type": "array", "items": {"type": "string"}, "description": "List of tool names the agent may use at this stage."},
                    "intent":        {"type": "string",  "description": "Short description of what this stage should accomplish."},
                    "gate":          {"type": "string",  "description": "Gate type: 'none', 'vote', or 'strict'."},
                    "retries":       {"type": "integer", "description": "Max retry attempts before demotion."},
                    "verifier":      {"type": "string",  "description": "Verifier type: 'none', 'python_sympy', 'lean4', 'coq', 'custom_script'."},
                    "extra_config":  {"type": "object",  "description": "Additional config keys to merge in (for any key not listed above)."},
                },
                "required": ["stage_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_project_pipeline",
            "description": (
                "[WRITE] Assign a pipeline template to a project. "
                "Cards whose current stage_key does not exist in the new template are automatically "
                "migrated to the first stage of the new template. "
                "Use after clone_pipeline + update_pipeline_stage to switch a project to a fixed pipeline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project_name": {"type": "string",  "description": "Project name (case-sensitive)."},
                    "template_id":  {"type": "integer", "description": "Pipeline template ID to assign."},
                },
                "required": ["project_name", "template_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "transfer_pipeline_cards",
            "description": (
                "[WRITE] Move cards from one pipeline to another using an explicit stage key mapping. "
                "Use this when you want fine-grained control over which stages map to which — "
                "for example when merging two pipelines or migrating a project. "
                "Cards whose stage_key is not in stage_map are left untouched. "
                "Use get_pipeline to inspect stage keys before building the map."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "from_template_id": {"type": "integer", "description": "Source pipeline template ID."},
                    "to_template_id":   {"type": "integer", "description": "Destination pipeline template ID."},
                    "stage_map": {
                        "type": "object",
                        "description": "Mapping of old stage_key -> new stage_key, e.g. {\"indev\": \"implementation\"}.",
                        "additionalProperties": {"type": "string"},
                    },
                    "project_name": {"type": "string", "description": "Restrict to one project (omit for all)."},
                },
                "required": ["from_template_id", "to_template_id", "stage_map"],
            },
        },
    },
    # ---- Log and diagnostic history tools ----
    {
        "type": "function",
        "function": {
            "name": "read_log_window",
            "description": (
                "[READ] Read log entries from the past N hours and count anomaly patterns "
                "(ERROR, WARNING, finish_reason=length, ContextTooLarge, tool_failure, timeout, "
                "stage_thrash, zombie, DB_lock). Returns a summary header then the raw log lines. "
                "Use grep to focus on a specific task ID, agent name, or error keyword. "
                "head/tail/grep filter output."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "How many hours back to read (default 1)."},
                    "head":  {"type": "integer", "description": "Return only the first N log lines."},
                    "tail":  {"type": "integer", "description": "Return only the last N log lines."},
                    "grep":  {"type": "string",  "description": "Filter log lines matching this regex/substring (applied after time filter)."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_budget_history",
            "description": (
                "[READ] Summarize LLM call history from budget_entries for a time window. "
                "Shows finish_reason breakdown (stop/length/tool_calls), agent name counts, "
                "total token usage, and a per-call detail table. "
                "Use to identify which agents are hitting token limits, which tasks are looping, "
                "or which stages are spending the most tokens. head/tail/grep filter the detail table."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string",  "description": "Filter to a single task ID (omit for all tasks)."},
                    "hours":   {"type": "integer", "description": "Look back this many hours (default 4)."},
                    "limit":   {"type": "integer", "description": "Max rows in the detail table (default 50)."},
                    "head":    {"type": "integer", "description": "Return only the first N detail rows."},
                    "tail":    {"type": "integer", "description": "Return only the last N detail rows."},
                    "grep":    {"type": "string",  "description": "Filter detail rows matching this regex/substring."},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task_history_recent",
            "description": (
                "[READ] Read the most recent N LLM turns for a task. "
                "Returns agent_name, timestamp, token counts, finish_reason, and a 500-char "
                "content_preview of the assistant message per turn. "
                "Use to inspect a worker agent's reasoning when base context is insufficient. "
                "max_turns is clamped to [1, 50]."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id":   {"type": "string",  "description": "Task ID to read history for."},
                    "max_turns": {"type": "integer", "description": "Max turns to return. Clamped to [1, 50]. Default 20."},
                },
                "required": ["task_id"],
            },
        },
    },
    # ---- File read tools ----
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "[READ] Read a file's structure or source code. Capped at 250 lines per call. "
                "FIRST CALL (no range): Returns structural summary (classes, functions, etc.) or full content if file <= 25 lines. "
                "SUBSEQUENT CALLS (no range): Serves the NEXT 250 unserved lines automatically. "
                "TARGETED READ: Provide 'start' (+ optionally 'end' or 'count') to read a specific range. No state change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)."},
                    "start": {"type": "integer", "description": "Starting line number (1-indexed). Omit to get summary (first call) or next chunk."},
                    "end": {"type": "integer", "description": "Ending line number (inclusive). Provide this OR count, not both."},
                    "count": {"type": "integer", "description": "Number of lines from start. Provide this OR end, not both."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file_metadata",
            "description": "[READ] Return file size, modification time, sha256, line count, and binary flag. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_last_output",
            "description": (
                "[READ] Slice the previous tool call's full output without re-running it. "
                "Applies offset, limit, grep, head, tail in that order. No state change."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N lines."},
                    "tail": {"type": "integer", "description": "Return only the last N lines."},
                    "grep": {"type": "string", "description": "Filter lines matching this regex/substring."},
                    "offset": {"type": "integer", "description": "Skip first N lines before applying other filters."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return after offset."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "[WRITE — files] Overwrite a file with the given content. Auto-stages for git. Reversible only via write_git_restore before commit. Path must be inside project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to write."},
                    "content": {"type": "string", "description": "Full text content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_file",
            "description": "[WRITE — files] Append text to the end of a file (creates it if absent). Auto-stages for git. Path must be inside project root.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file."},
                    "content": {"type": "string", "description": "Text to append."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "patch_file",
            "description": (
                "[WRITE — files] Replace an exact string in a file. "
                "old_str must match exactly once — extend it with more surrounding lines if ambiguous. "
                "Fails with a clear error if old_str appears 0 or 2+ times. "
                "CRLF (\\r\\n) in old_str is auto-normalized — you never need to worry about \\r. "
                "Auto-repairs trailing whitespace differences, extra blank lines around old_str, and "
                "indentation mismatches when an exact match is not found — repair applied is noted in the OK message. "
                "read_file marks trailing whitespace as <trailing:Nsp> or <trailing:Ntab>; "
                "you do NOT need to include these markers in old_str — they are informational only. "
                "Prefer this over write_file for any targeted edit: avoids full-file rewrites and the "
                "silent regressions they cause. Auto-stages for git. Path must be inside project root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to patch."},
                    "old_str": {
                        "type": "string",
                        "description": (
                            "Text to find and replace — copied verbatim from a recent read_file() call. "
                            "Must appear exactly once. Include 2-3 lines of surrounding context "
                            "(blank lines, function signatures) to ensure uniqueness. "
                            "Leading indentation must match exactly. "
                            "CRLF is auto-normalized. Trailing whitespace differences are auto-repaired."
                        ),
                    },
                    "new_str": {
                        "type": "string",
                        "description": "Replacement text. May be empty string to delete old_str.",
                    },
                },
                "required": ["path", "old_str", "new_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_archive",
            "description": "[WRITE — archive] Safely 'delete' a file by moving it to .archive/<timestamp>/. NEVER hard-deletes. Reversible: copy from the archive path in the return value.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path of the file to archive."},
                    "reason": {"type": "string", "description": "Human-readable reason for archiving.", "default": ""},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_delete_file",
            "description": (
                "[WRITE — safe delete] Move a file to .archive/ with a DB recovery record. "
                "The file is gone from the worktree but fully restorable via the UI or workspace_undelete_file. "
                "Returns archive_id you can report to the user."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File path relative to the worktree root."},
                    "reason": {"type": "string", "description": "Why you are deleting this file.", "default": ""},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "workspace_rename_file",
            "description": "[WRITE — rename] Rename (move) a file within the worktree. Fails if destination already exists.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Source path relative to worktree root."},
                    "dst": {"type": "string", "description": "Destination path relative to worktree root."},
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": (
                "[WRITE — files] Move or rename a file within the project. "
                "Renaming is just a move with a different name at the same directory level. "
                "If dst already exists, it is archived to .archive/ before being overwritten — never hard-deleted. "
                "Auto-stages both the old and new paths for git. Both paths must be inside project root."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string", "description": "Current path of the file to move."},
                    "dst": {"type": "string", "description": "Destination path (including filename). May be a rename, a relocation, or both."},
                },
                "required": ["src", "dst"],
            },
        },
    },
    # ---- Directory / search tools ----
    {
        "type": "function",
        "function": {
            "name": "list_directory",
            "description": "[READ] List files and subdirectories at a given path with annotations for gitignored/excluded/protected entries. head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list.", "default": "."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_in_files",
            "description": "[READ] Search file contents using a regex pattern. Returns file:line matches (up to 200). head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "directory": {"type": "string", "description": "Directory to search in.", "default": "."},
                    "head": {"type": "integer", "description": "Return only the first N matches."},
                    "tail": {"type": "integer", "description": "Return only the last N matches."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "[READ] Find files by glob pattern (e.g. '*.py', 'test_*.py'). head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "glob_pattern": {"type": "string", "description": "Glob pattern to match filenames."},
                    "directory": {"type": "string", "description": "Root directory to search from.", "default": "."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["glob_pattern"],
            },
        },
    },
    # ---- Static analysis helpers ----
    {
        "type": "function",
        "function": {
            "name": "find_symbol",
            "description": "[READ] Find function/class definitions by name using tree-sitter. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "Symbol name or substring to search for."},
                    "kind": {"type": "string", "description": "Symbol kind: 'function', 'class', or 'any' (default).", "default": "any"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_callers",
            "description": "[READ] Find files that import or reference a given symbol, using the static analysis import graph. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "symbol": {"type": "string", "description": "Symbol name to find references for."},
                },
                "required": ["symbol"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_imports_of",
            "description": "[READ] Find all files that import a given module (by relative path or module name). No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_path": {"type": "string", "description": "Module relative path (e.g. 'app/agent/tools.py') or dotted module name."},
                },
                "required": ["module_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_diff_stat",
            "description": "[READ] git diff --stat from <since> to HEAD, showing added/removed lines per file. head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "Base ref (branch, tag, or commit). Default: 'main'.", "default": "main"},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_test_summary",
            "description": "[READ] Parse the most recent run_test_pytest output into {passed, failed, errors, failing_names}. No state change.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_status",
            "description": "[READ] Return the current git status of the project. head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_diff",
            "description": "[READ] Return git diff (staged + unstaged). Optionally scoped to a path. head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional path to scope the diff."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_log",
            "description": "[READ] Return recent git log entries. Optionally scoped to a specific file. head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file path to scope the log to."},
                    "max_count": {"type": "integer", "description": "Maximum number of log entries to return (1-100, default 20).", "default": 20},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_blame",
            "description": "[READ] Show git blame for a file (last-modified info per line). head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to blame."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_show",
            "description": "[READ] Show a file's content at a specific git ref, or show commit details (message + diffstat). head/tail/grep filter output. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Git ref (commit hash, branch, tag, HEAD~N, etc.)."},
                    "path": {"type": "string", "description": "Optional file path to show at the given ref. If omitted, shows commit details + diffstat."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["ref"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_git_branch",
            "description": f"[WRITE — git] Create and checkout a new branch. Must be prefixed with '{GIT_SAFETY_BRANCH_PREFIX}'. Reversible via write_git_checkout to prior branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch_name": {"type": "string", "description": f"Branch name (must start with '{GIT_SAFETY_BRANCH_PREFIX}')."},
                },
                "required": ["branch_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_git_commit",
            "description": "[WRITE — git] Stage all tracked changes and create a git commit. Permanent record; reversible only via a subsequent revert commit.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Commit message."},
                },
                "required": ["message"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_git_checkout",
            "description": "[WRITE — git] Checkout a maestro/task-* branch. Only maestro/task-* branches are permitted — agents in worktrees must stay on their assigned branch.",
            "parameters": {
                "type": "object",
                "properties": {
                    "branch": {"type": "string", "description": "Branch name to checkout."},
                },
                "required": ["branch"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": "[READ] Fetch a Kanban task by ID. Returns a JSON object with all task fields. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The unique task ID."},
                },
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_tasks",
            "description": "[READ] List task summaries for a project, optionally filtered by column. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Project name to list tasks for."},
                    "column": {
                        "type": "string",
                        "description": "Optional column/type filter (e.g. 'planning', 'development', 'review', 'completed', 'architecture').",
                    },
                },
                "required": ["project"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_task_status",
            "description": (
                "[WRITE — db] Advance a task through the Kanban pipeline. "
                "Valid statuses: PENDING, ACTIVE, VERIFYING, ACCEPTED, REJECTED."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The unique task ID."},
                    "new_status": {
                        "type": "string",
                        "enum": ["PENDING", "ACTIVE", "VERIFYING", "ACCEPTED", "REJECTED"],
                        "description": "Target status.",
                    },
                },
                "required": ["task_id", "new_status"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_task_history",
            "description": "[WRITE — db] Append a proof-of-work entry to a task's history log.",
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The unique task ID."},
                    "entry": {"type": "string", "description": "Description of what was done."},
                },
                "required": ["task_id", "entry"],
            },
        },
    },
    # --- Planning tools ---
    {
        "type": "function",
        "function": {
            "name": "write_arch_doc",
            "description": (
                "[WRITE — db] Produce a structured markdown architecture document from components and relationships "
                "and persist it to the architecture task DB record."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Architecture document title."},
                    "components": {
                        "type": "array",
                        "description": "List of components. Each can be a string or {name, description, technology}.",
                        "items": {},
                    },
                    "relationships": {
                        "type": "array",
                        "description": "List of relationships. Each can be a string or {from, to, label}.",
                        "items": {},
                    },
                },
                "required": ["title", "components", "relationships"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_mermaid",
            "description": (
                "[WRITE — db] Validate and persist a Mermaid diagram. "
                "Valid types: flowchart, sequence, class, er, gantt, state, pie."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "diagram_type": {
                        "type": "string",
                        "description": "Diagram type: flowchart, sequence, class, er, gantt, state, pie.",
                    },
                    "definition": {"type": "string", "description": "Mermaid diagram definition body."},
                },
                "required": ["diagram_type", "definition"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_interface_contract",
            "description": (
                "[WRITE — db] Define and persist the API surface / interface contract between components. "
                "Returns structured JSON describing what a component provides and consumes."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "component_name": {"type": "string", "description": "Name of the component."},
                    "provides": {
                        "type": "array",
                        "description": "What this component provides (API endpoints, exports, events). Each item can be a string or {name, type, description}.",
                        "items": {},
                    },
                    "consumes": {
                        "type": "array",
                        "description": "What this component needs from others. Each item can be a string or {name, type, source}.",
                        "items": {},
                    },
                },
                "required": ["component_name", "provides", "consumes"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "spawn_research_agent",
            "description": (
                "[RUN — sandbox] Launch a research agent to investigate a domain question. "
                "The agent has read-only codebase access and returns findings. "
                "Use when you need domain knowledge about unfamiliar technologies."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {"type": "string", "description": "The research question to investigate."},
                    "context": {"type": "string", "description": "Optional context to provide to the research agent.", "default": ""},
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "launch_research_agent",
            "description": (
                "[RUN — sandbox] Schedule a research agent job through the Maestro scheduler and "
                "block until it completes. Unlike spawn_research_agent (which runs "
                "inline), this releases the caller's LLM slot while waiting so the "
                "scheduler can dispatch the research on the same endpoint without "
                "deadlocking. The caller resumes with the full findings as the tool "
                "result — the wait may span minutes to an hour depending on queue "
                "depth and research complexity. Use for deep investigations where "
                "you want proper scheduler visibility, budget tracking, and "
                "diagnostics UI integration."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The research question to investigate.",
                    },
                    "context": {
                        "type": "string",
                        "description": "Optional background context for the researcher.",
                        "default": "",
                    },
                },
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_benchmark",
            "description": (
                "[WRITE — db] Record a before/after profiling benchmark for an optimization sub-task. "
                "Call with benchmark_type='before' before making changes, and 'after' when done. "
                "Run actual timed benchmarks (run_test_pytest --benchmark or similar) before recording - do NOT estimate. "
                "metrics must be a JSON string with the following keys: "
                "test_duration_ms (float, required) - measured wall time in ms for scale_n items; "
                "memory_peak_mb (float, required) - peak RSS during benchmark in MB; "
                "complexity_score (int, required) - subjective 0-100 code complexity estimate; "
                "big_o_class (str) - Big O of the critical path: O(1), O(log n), O(n), O(n log n), O(n^2), O(n^3), O(2^n), O(n!); "
                "scale_n (int) - N used in the synthetic benchmark run; "
                "readability_cost (float) - 0.0 (no cost) to 1.0 (very hard to understand); "
                "is_premature (bool) - true if optimizing a non-bottleneck; "
                "tech_debt_resolved (bool) - true if this consolidates or resolves known tech debt; "
                "notes (str) - qualitative notes (optional)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "string", "description": "The ID of the current optimization sub-task."},
                    "parent_task_id": {"type": "string", "description": "The ID of the parent optimization task."},
                    "benchmark_type": {"type": "string", "enum": ["before", "after"], "description": "Whether this is a before or after measurement."},
                    "metrics": {"type": "string", "description": "JSON string with profiling metrics. Required: test_duration_ms, memory_peak_mb, complexity_score. Recommended: big_o_class, scale_n, readability_cost, is_premature, tech_debt_resolved, notes."},
                },
                "required": ["task_id", "parent_task_id", "benchmark_type", "metrics"],
            },
        },
    },
    # ---- git file operations ----
    {
        "type": "function",
        "function": {
            "name": "write_git_restore",
            "description": (
                "[WRITE — git] Restore a tracked file to its HEAD state, DISCARDING all local changes. "
                "Irreversible for unsaved work. The file must be tracked by git. "
                "Does NOT affect already-staged files — commit or reset the index first if needed."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to restore (relative to project root)."},
                },
                "required": ["path"],
            },
        },
    },
    # ---- Named testing / linting tools ----
    {
        "type": "function",
        "function": {
            "name": "run_test_pytest",
            "description": (
                "[RUN — sandbox] Run pytest in the task's project directory. "
                "The project venv's Python is used automatically. "
                "A per-test timeout of 300s is injected unless pytest.ini already sets one. "
                "head/tail/grep filter output. Does not modify project files."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Test target(s): a file, directory, or space-separated list of files/dirs (default: '.' for all tests). IMPORTANT: put all test paths here, not in flags. Example: 'tests/test_a.py tests/test_b.py'."},
                    "flags": {"type": "string", "description": "Pytest option flags ONLY (e.g. '-v', '-k test_foo', '--tb=short'). Do NOT put test file paths here."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check_mypy",
            "description": "[RUN — sandbox] Run mypy type-checker on the given path. The project venv's Python is used. head/tail/grep filter output. Does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or package to type-check."},
                    "flags": {"type": "string", "description": "Additional mypy flags (e.g. '--strict', '--ignore-missing-imports')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check_ruff",
            "description": "[RUN — sandbox] Run ruff linter on the given path. Check-only; head/tail/grep filter output. Does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to lint (default: '.')."},
                    "flags": {"type": "string", "description": "Additional ruff flags (e.g. '--fix', '--select E,F')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check_black",
            "description": "[RUN — sandbox] Check code formatting with black. Read-only — head/tail/grep filter output. Does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to check (default: '.')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_unittest",
            "description": "[RUN — sandbox] Run Python unittest discovery or a specific test module. head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module": {"type": "string", "description": "Dotted module name to run (e.g. 'tests.test_foo'). Leave empty for full discovery."},
                    "pattern": {"type": "string", "description": "File pattern for discover (e.g. 'test_*.py'). Ignored if module is set."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_npm",
            "description": "[RUN — sandbox] Run npm test in the task's project directory. head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_cargo",
            "description": "[RUN — sandbox] Run cargo test in the task's project directory. head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional cargo test flags (e.g. '--release', '-- --nocapture')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_go",
            "description": "[RUN — sandbox] Run go test in the task's project directory. head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Package path to test (default: './...' for all packages)."},
                    "flags": {"type": "string", "description": "Additional go test flags (e.g. '-v', '-run TestFoo')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    # ---- Named build tools ----
    {
        "type": "function",
        "function": {
            "name": "run_build_make",
            "description": "[RUN — build] Run a Makefile target in the task's project directory. head/tail/grep filter output. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Makefile target (e.g. 'build', 'test', 'lint', 'all'). Must be alphanumeric/underscore/dash/dot/slash only."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_cargo",
            "description": "[RUN — build] Build a Rust/Cargo project (cargo build). head/tail/grep filter output. Writes build artifacts to target/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional cargo build flags (e.g. '--release')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_go",
            "description": "[RUN — build] Build a Go project (go build ./...). head/tail/grep filter output. Writes binary output inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_npm",
            "description": "[RUN — build] Run an npm build script (npm run <script>). head/tail/grep filter output. Writes build artifacts inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "The npm script name (default: 'build')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_tsc",
            "description": "[RUN — build] Run the TypeScript compiler (tsc). head/tail/grep filter output. Writes compiled output inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional tsc flags (e.g. '--noEmit', '--watch')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_gradle",
            "description": "[RUN — build] Run a Gradle task in the task's project directory. head/tail/grep filter output. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Gradle task name (e.g. 'build', 'test', 'assemble'). Must be alphanumeric/underscore/dash/dot/slash only."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_mvn",
            "description": "[RUN — build] Run a Maven goal in the task's project directory. head/tail/grep filter output. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Maven lifecycle phase or plugin goal (e.g. 'package', 'test', 'compile')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["goal"],
            },
        },
    },
    # ---- Named dependency tools ----
    {
        "type": "function",
        "function": {
            "name": "run_deps_pip",
            "description": (
                "[RUN — deps] Install Python packages with pip. Mutates the venv environment. "
                "Call after modifying requirements.txt or pyproject.toml. head/tail/grep filter output. "
                "Examples: run_deps_pip('-r requirements.txt'), run_deps_pip('requests>=2.28')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "pip install arguments (e.g. '-r requirements.txt', 'requests>=2.28', '-e .')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deps_npm",
            "description": "[RUN — deps] Install Node.js dependencies (npm install). Mutates node_modules. head/tail/grep filter output. Call after modifying package.json.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deps_cargo",
            "description": "[RUN — deps] Fetch Rust/Cargo dependencies (cargo fetch). Mutates the local cargo registry cache. head/tail/grep filter output. Call after modifying Cargo.toml.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    # ---- Named security scanner tools ----
    {
        "type": "function",
        "function": {
            "name": "run_audit_bandit",
            "description": "[RUN — audit] Run the bandit Python security linter. Read-only; head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file to scan (default: '.')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_pip",
            "description": "[RUN — audit] Audit installed Python packages for known vulnerabilities (pip-audit). Read-only; head/tail/grep filter output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_semgrep",
            "description": "[RUN — audit] Run semgrep static analysis. Read-only; head/tail/grep filter output. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to scan (default: '.')."},
                    "config": {"type": "string", "description": "Semgrep config/ruleset (default: 'auto')."},
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_npm",
            "description": "[RUN — audit] Run npm audit to check Node.js dependencies for known vulnerabilities. Read-only; head/tail/grep filter output.",
            "parameters": {
                "type": "object",
                "properties": {
                    "head": {"type": "integer", "description": "Return only the first N output lines."},
                    "tail": {"type": "integer", "description": "Return only the last N output lines."},
                    "grep": {"type": "string", "description": "Filter output lines matching this regex/substring."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "[READ] Execute a web search using Tavily, DuckDuckGo, or Brave Search. "
                "Returns a JSON string of results with titles, URLs, and snippets. "
                "Preferred provider is configured in maestro.ini. "
                "Requires TAVILY_API_KEY or BRAVE_API_KEY depending on provider. "
                "To respect our 1000 queries/month budget, we only allow one search every 30 minutes. "
                "Cached queries do not count against this limit and are returned immediately. "
                "Use this only when you need up-to-date information or to research unknown technologies/APIs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query to execute."},
                    "count": {"type": "integer", "description": "Number of results to return (default 5).", "default": 5},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "[READ] Fetch the content of a URL and return a text-only summary. "
                "Strips HTML tags, scripts, and styles. Use this to read the full "
                "content of a web page after finding interesting URLs in search results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_project_summary",
            "description": "[READ] Return the top-level project health summary if it exists and is fresh. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Optional project name (inferred from context if omitted)."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directory_summary",
            "description": "[READ] Return the summary for a specific directory within the project. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "rel_dir": {"type": "string", "description": "Relative path to the directory (e.g. 'app/agent')."},
                    "project": {"type": "string", "description": "Optional project name."},
                },
                "required": ["rel_dir"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_module_summary",
            "description": "[READ] Return the summary for a logical module within the project. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "module_name": {"type": "string", "description": "Name of the module (as assigned during survey)."},
                    "project": {"type": "string", "description": "Optional project name."},
                },
                "required": ["module_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_scope_summaries",
            "description": "[READ] List all available scope summaries (type, key, short_summary, freshness) for a project. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {"type": "string", "description": "Optional project name."},
                    "scope_type": {"type": "string", "description": "Optional filter: 'directory', 'module', 'collection', or 'project'."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_plan_fields",
            "description": (
                "[WRITE — db] Patch one or more fields on a planning_results row. "
                "Use this to make targeted corrections to design_rationale, interface_contracts, "
                "dependency_graph, file_manifest, test_strategy, or implementation_steps. "
                "Pass result_id (from the system prompt) and fields_json as a JSON object "
                "mapping field names to their corrected values. "
                "Call once with all corrections — do not call multiple times."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "result_id": {
                        "type": "integer",
                        "description": "The planning_results row ID to patch (provided in the system prompt).",
                    },
                    "fields_json": {
                        "type": "string",
                        "description": (
                            "JSON object mapping field name(s) to corrected value(s). "
                            "Allowed keys: design_rationale, interface_contracts, dependency_graph, "
                            "file_manifest, test_strategy, implementation_steps. "
                            'Example: {"interface_contracts": [...corrected list...]}'
                        ),
                    },
                },
                "required": ["result_id", "fields_json"],
            },
        },
    },
    # ---- Diagnostic tool ----
    {
        "type": "function",
        "function": {
            "name": "report_tool_bug",
            "description": (
                "[WRITE — diagnostics] Report a tool malfunction to the Maestro bug tracker. "
                "Use this when a tool behaves in a way that prevents you from making progress — "
                "wrong output, stale content, unexpected error, missing capability, silent failure, etc. "
                "After filing the report, try an alternative approach or call submit_work. "
                "Do NOT use this for expected errors like 'file not found' — only for tool misbehaviour."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tool_name": {
                        "type": "string",
                        "description": "Name of the misbehaving tool, e.g. 'patch_file', 'run_test_pytest'.",
                    },
                    "trying_to": {
                        "type": "string",
                        "description": "What you were attempting when the tool failed, e.g. 'replace the retry logic in llm_client.py lines 40-55'.",
                    },
                    "expected": {
                        "type": "string",
                        "description": "What the tool should have done or returned.",
                    },
                    "actual": {
                        "type": "string",
                        "description": "What it actually did or returned. Include the exact error message or describe the wrong output.",
                    },
                },
                "required": ["tool_name", "trying_to", "expected", "actual"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "consult_maestro",
            "description": (
                "[RUNS] Pause execution and consult the Maestro orchestrator (or human) for guidance on a complex decision. "
                "Use this when you are facing an architectural ambiguity, repeatedly failing a test, or unsure of the best implementation path. "
                "Execution will be suspended until a steering hint is provided. Zero turns are lost during the pause."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "The specific question or dilemma you need guidance on. Be clear and provide context.",
                    },
                },
                "required": ["question"],
            },
        },
    },
    # ---- Card factory tool (Phase 5) ----
    {
        "type": "function",
        "function": {
            "name": "batch_create_cards",
            "description": (
                "[WRITE — db] Create multiple new task cards in the current task's project. "
                "Each card enters the pipeline at its entry_stage. "
                "Use this to decompose the current task into sub-tasks. "
                "If new_parent is provided, a parent card is created and the new cards are nested under it. "
                "If archive_origin is true, the current task is demoted to 'archive' after creation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "cards": {
                        "type": "array",
                        "description": "List of cards to create.",
                        "items": {
                            "type": "object",
                            "properties": {
                                "title":        {"type": "string"},
                                "description":  {"type": "string"},
                                "entry_stage":  {
                                    "type": "string",
                                    "description": "stage_key where the card starts (default: 'idea')",
                                },
                                "tags":         {"type": "array", "items": {"type": "string"}},
                                "prereq_ids":   {
                                    "type": "array",
                                    "items": {"type": "string"},
                                    "description": "IDs of prerequisite cards (use 'sub-N' for same-batch siblings by index)",
                                },
                            },
                            "required": ["title"],
                        },
                    },
                    "new_parent": {
                        "type": ["object", "null"],
                        "description": "If provided, creates this parent card and nests the new cards under it.",
                        "properties": {
                            "title":       {"type": "string"},
                            "description": {"type": "string"},
                        },
                    },
                    "archive_origin": {
                        "type": "boolean",
                        "default": False,
                        "description": "If true, demote the current card to 'archive' after creating sub-cards.",
                    },
                },
                "required": ["cards"],
            },
        },
    },
    # ---- Terminal tool ----
    {
        "type": "function",
        "function": {
            "name": "submit_work",
            "description": (
                "[FINISH] Terminate your session and report outcome. "
                "Call this ONCE at the very end — it immediately stops the agent loop. "
                "If a gate rejects your submission, satisfy the required tools then call "
                "submit_work(signal='ACCEPTED', previous=True) to re-submit without retyping your summary. "
                "Implementors: ACCEPTED (work done, tests pass), "
                "REVERT_TO_DESIGN (design is broken, cannot proceed), "
                "SUBDIVIDE (task too large for one agent), "
                "PLAN_UPDATED (planning correction complete). "
                "Reviewers: ACCEPTED (implementation passes), "
                "REJECTED (implementation fails review criteria). "
                "Any agent: NEEDS_HUMAN (decision requires human judgment — escalates to human review)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "enum": ["ACCEPTED", "REJECTED", "REVERT_TO_DESIGN", "SUBDIVIDE", "PLAN_UPDATED", "NEEDS_HUMAN"],
                        "description": (
                            "ACCEPTED: work complete or review passes. "
                            "REJECTED: reviewer finds the implementation fails criteria. "
                            "REVERT_TO_DESIGN: implementor cannot proceed — design is broken. "
                            "SUBDIVIDE: task too large, needs breakdown. "
                            "PLAN_UPDATED: planning correction applied. "
                            "NEEDS_HUMAN: decision exceeds agent confidence — escalates to human review."
                        ),
                    },
                    "summary": {
                        "type": "string",
                        "description": "A concise final report of work done or justification for the signal.",
                    },
                    "payload": {
                        "type": ["object", "null"],
                        "description": (
                            "Optional dictionary for agent-specific data (e.g., test results, "
                            "sub-task lists, verdict details). Pass null or omit if no payload needed."
                        ),
                    },
                    "previous": {
                        "type": "boolean",
                        "description": (
                            "If true, reuse signal/summary/payload from the most recent prior "
                            "submit_work call in this session. Chains back through previous=True "
                            "calls to find concrete arguments. Use after a gate rejection to "
                            "re-submit without regenerating your summary. "
                            "ERROR if no prior submit_work exists in this session — you must "
                            "call submit_work with explicit arguments at least once first."
                        ),
                    },
                },
                "required": ["signal", "summary"],
            },
        },
    },
    # ----- Document store tools -----
    {
        "type": "function",
        "function": {
            "name": "store_document",
            "description": (
                "[WRITE — doc-store] Write a named document to the project's shared document store. "
                "Other agents in this project can read it by key. "
                "Use path-style keys like 'proofs/lemma_3' or 'characters/elara'. "
                "If a document with the same key already exists it is overwritten (last-write-wins). "
                "Use unique keys for distinct artifacts — 'proofs/attempt_1', 'proofs/attempt_2', etc."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Unique name for this document, e.g. 'proofs/lemma_3' or 'characters/elara'",
                    },
                    "content": {
                        "type": "string",
                        "description": "The document body to store.",
                    },
                    "tags": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Optional tags for categorisation, e.g. ['math', 'proof']",
                    },
                },
                "required": ["key", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_document",
            "description": (
                "[READ] Retrieve a document from the project's shared document store by exact key. "
                "Returns the full content. Returns NOT FOUND if the key does not exist. "
                "Use search_documents if you are unsure of the exact key."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "Exact key of the document to retrieve.",
                    },
                },
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_documents",
            "description": (
                "[READ] Find documents whose key is similar to the query using fuzzy matching. "
                "Returns up to 10 results sorted by similarity (0.0–1.0). "
                "Use this when you are not sure of the exact key or want to discover related documents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Key fragment or approximate key to search for.",
                    },
                    "threshold": {
                        "type": "number",
                        "description": "Minimum similarity score (0.0–1.0). Default 0.3. Lower = more results.",
                        "default": 0.3,
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_documents",
            "description": (
                "[READ] List all document keys in the project store (metadata only, no content). "
                "Optionally filter by tag. Use to discover what has been stored before reading."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "tag": {
                        "type": "string",
                        "description": "Optional tag to filter by.",
                    },
                },
            },
        },
    },
    # --- Autopilot objective tools (Gap 4) ---
    {
        "type": "function",
        "function": {
            "name": "get_objective_detail",
            "description": (
                "[READ] Return full details of an autopilot objective including its direct children. "
                "Use to inspect priority, status, last assessment, and child objectives."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective_id": {
                        "type": "integer",
                        "description": "ID of the autopilot objective to retrieve.",
                    },
                },
                "required": ["objective_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_objective_evidence",
            "description": (
                "[READ] Return the full evidence log for an autopilot objective. "
                "The log is an append-only record of findings, milestones, and dead ends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective_id": {
                        "type": "integer",
                        "description": "ID of the objective whose evidence log to retrieve.",
                    },
                },
                "required": ["objective_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "append_objective_evidence",
            "description": (
                "[WRITE] Append a timestamped note to an objective's evidence log. "
                "Use to record findings, progress milestones, or dead ends during investigation."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "objective_id": {
                        "type": "integer",
                        "description": "ID of the objective to append evidence to.",
                    },
                    "entry": {
                        "type": "string",
                        "description": "The evidence note to append. Markdown is supported.",
                    },
                },
                "required": ["objective_id", "entry"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_objectives",
            "description": (
                "[READ] List autopilot objectives for the current project. "
                "Returns one-line summaries including id, priority, status, and description."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "paused", "complete", "all"],
                        "description": "Filter by status. Default 'active'.",
                        "default": "active",
                    },
                },
            },
        },
    },
    # --- Math tools ---
    {
        "type": "function",
        "function": {
            "name": "run_sympy",
            "description": (
                "[RUN — docker-sandbox] Execute Python/SymPy code for mathematical computation. "
                "STATELESS: each call starts a FRESH container — files, variables, and imports from "
                "previous calls are completely gone. Do NOT use for lake, lean, or any Lean4 operations; "
                "use run_lean4 for those. No network access. "
                "Returns stdout and stderr. "
                "To persist results between calls, extract the value from stdout and store it with "
                "store_document() or write_file() before the call ends."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "Python source code to execute (may import sympy, numpy, scipy, mpmath).",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max execution seconds (default 120, clamped to [10, 600]).",
                    },
                },
                "required": ["code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_lean4",
            "description": (
                "[RUN — docker-sandbox] Compile a Lean4 source file against the pre-built Mathlib. "
                "Writes your source string to /mathlib-project/Verify.lean inside the container, "
                "then runs `lake env lean /mathlib-project/Verify.lean` — Mathlib imports work "
                "immediately with no lake init, no cache fetch, and no project setup of any kind. "
                "STATELESS: each call is a fresh container; use write_file() in the workspace to "
                "persist the .lean source between calls. "
                "Returns stdout and stderr. "
                "Exit code 0 (message: 'compiled successfully') means no errors and no sorry. "
                "DO NOT use run_sympy to run lake or lean commands — use this tool instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "Complete Lean4 source file contents to compile.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Max compilation seconds (default 120, clamped to [30, 600]).",
                    },
                },
                "required": ["source"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_arxiv",
            "description": (
                "[READ] Search arXiv for mathematical papers. "
                "Returns structured records: id, title, authors, year, abstract, url, pdf."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query string, e.g. 'twin prime conjecture'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 20).",
                    },
                    "category": {
                        "type": "string",
                        "description": "arXiv category filter, e.g. 'math.NT' for number theory (optional).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_oeis",
            "description": (
                "[READ] Search the OEIS (Online Encyclopedia of Integer Sequences). "
                "Returns structured records: id, name, values, offset, formula, url."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — a sequence description, keyword, or comma-separated integers.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Number of results to return (default 5, max 20).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    # Gap 12 — Lean4 proof depth
    {
        "type": "function",
        "function": {
            "name": "search_mathlib",
            "description": (
                "[READ] Search Lean4 Mathlib for existing theorems, lemmas, and definitions. "
                "Always call this before attempting to prove something — it may already exist. "
                "Returns list of {name, type, module, doc}."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search terms, e.g. 'prime gap sieve' or 'Nat.Prime dvd'.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "Max results to return (default 10, max 50).",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_mathlib_topics",
            "description": (
                "[READ] Browse curated Mathlib topic areas with key lemma names. "
                "Call with no argument to see all topics; pass a category to filter. "
                "Use this to orient yourself before calling search_mathlib. "
                "Categories: Number Theory, Algebra, Combinatorics, Logic, Order Theory, Analysis, Lean4 Tactics."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "category": {
                        "type": "string",
                        "description": (
                            "Category filter (case-insensitive, partial match). "
                            "Omit to return all topics."
                        ),
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_lean4_proof_state",
            "description": (
                "[RUN — docker-sandbox] Get the Lean4 proof state (goal + hypotheses) at a "
                "specific line in a .lean source file. Place a `sorry` at the point you want "
                "to inspect — the infoview shows what sorry is standing in for. "
                "Use after writing a proof attempt to understand what remains to be proved."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "lean_source": {
                        "type": "string",
                        "description": "Full Lean4 source code of the file.",
                    },
                    "line": {
                        "type": "integer",
                        "description": "1-indexed line number to inspect (place `sorry` there).",
                    },
                    "col": {
                        "type": "integer",
                        "description": "Column number (default 0).",
                    },
                },
                "required": ["lean_source", "line"],
            },
        },
    },
    # Gap 5 — self-modification
    {
        "type": "function",
        "function": {
            "name": "vote_to_revert",
            "description": (
                "[RUN] Cast a vote to revert the most recent self-modification merge commit. "
                "Use when you observe that a recent change caused regressions or broken "
                "functionality. When votes reach the configured threshold the system "
                "automatically runs git revert and creates a PIP card. "
                "Only available in _maestro_self project sessions."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "reason": {
                        "type": "string",
                        "description": "Why this merge should be reverted.",
                    },
                },
                "required": ["reason"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_episodes",
            "description": (
                "[READ] Search episodic memory for past attempts, failures, or conclusions "
                "related to a question or approach. Returns semantically similar past "
                "episodes with their outcomes. Use before starting an approach that may "
                "have been tried and failed before."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "What to search for — describe the approach or problem area.",
                    },
                    "k": {
                        "type": "integer",
                        "description": "Max results to return (1–20). Default 5.",
                    },
                    "episode_type": {
                        "type": "string",
                        "description": (
                            "Optional filter: 'failure', 'session_summary', or 'document'. "
                            "Omit to search all types."
                        ),
                    },
                },
                "required": ["question"],
            },
        },
    },
    # ---- Inter-agent messaging (Gap 8) ----
    {
        "type": "function",
        "function": {
            "name": "ask_agent",
            "description": (
                "[RUN] Ask another running agent session a question and receive its answer inline. "
                "The other agent runs a fresh reasoning session with your question and returns "
                "a direct answer. This is a blocking call — your session waits for the reply. "
                "Use list_active_sessions() first to find the right target."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target_session_id": {
                        "type": "string",
                        "description": "Session ID of the agent to ask. Use list_active_sessions() to find it.",
                    },
                    "question": {
                        "type": "string",
                        "description": "The question or request to send to the other agent.",
                    },
                },
                "required": ["target_session_id", "question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_active_sessions",
            "description": (
                "[READ] List all currently running agent sessions across all projects. "
                "Returns session ID, task ID, task title, agent type, and LLM ID. "
                "Use to find a target before calling ask_agent()."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "project": {
                        "type": "string",
                        "description": "Optional — filter to sessions in this project only.",
                    },
                },
                "required": [],
            },
        },
    },
    # ---- Event watch tools (Gap 9) ----
    {
        "type": "function",
        "function": {
            "name": "register_watch",
            "description": (
                "[RUN] Register an event watch that triggers a Maestro autopilot tick when "
                "an external event occurs. Supported types: webhook (HTTP POST), "
                "file_watch (filesystem), api_poll (periodic URL fetch)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "event_type": {
                        "type": "string",
                        "enum": ["webhook", "file_watch", "api_poll"],
                        "description": "Type of event source.",
                    },
                    "label": {
                        "type": "string",
                        "description": "Human-readable name for this watch.",
                    },
                    "source_config": {
                        "type": "object",
                        "description": (
                            "Event-source configuration. "
                            "webhook: {secret?}. "
                            "file_watch: {path, recursive?, events?}. "
                            "api_poll: {url, poll_interval_seconds?, timeout_seconds?, headers?}."
                        ),
                    },
                    "fire_config": {
                        "type": "object",
                        "description": (
                            "Dedup/expiry configuration (all optional): "
                            "{cooldown_seconds, use_content_hash, max_fires, expires_at}."
                        ),
                    },
                },
                "required": ["event_type", "label", "source_config"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_watches_for_project",
            "description": "[READ] List event watches for the current project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "enum": ["active", "paused", "expired"],
                        "description": "Filter by watch status (default: active).",
                    },
                },
                "required": [],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool list for PlanningCorrectionAgent
# ---------------------------------------------------------------------------

TOOL_CATEGORIES: dict[str, str] = {
    # Infrastructure — always-on tools and system control
    "submit_work":            "Infrastructure",
    "report_tool_bug":        "Infrastructure",
    "consult_maestro":        "Infrastructure",
    "cleanup_ghost_worktrees": "Infrastructure",
    "restart_server":         "Infrastructure",
    "get_system_health":      "Infrastructure",
    "read_log_window":        "Infrastructure",
    "get_budget_history":         "Infrastructure",
    "get_task_history_recent":    "Infrastructure",
    # Pipelines — pipeline template management
    "list_pipelines":          "Pipelines",
    "get_pipeline":            "Pipelines",
    "clone_pipeline":          "Pipelines",
    "update_pipeline":         "Pipelines",
    "update_pipeline_stage":   "Pipelines",
    "assign_project_pipeline": "Pipelines",
    "transfer_pipeline_cards": "Pipelines",
    # Files — reading, writing, searching the filesystem
    "read_file":              "Files",
    "read_file_metadata":     "Files",
    "read_last_output":       "Files",
    "write_file":             "Files",
    "append_file":            "Files",
    "patch_file":             "Files",
    "write_archive":          "Files",
    "workspace_delete_file":  "Files",
    "workspace_rename_file":  "Files",
    "move_file":              "Files",
    "list_directory":         "Files",
    "find_in_files":          "Files",
    "find_files":             "Files",
    # Code Analysis — symbol and import graph navigation
    "find_symbol":            "Code Analysis",
    "find_callers":           "Code Analysis",
    "find_imports_of":        "Code Analysis",
    # Git — version control operations
    "read_git_status":        "Git",
    "read_git_diff":          "Git",
    "read_git_log":           "Git",
    "read_git_blame":         "Git",
    "read_git_show":          "Git",
    "read_diff_stat":         "Git",
    "write_git_branch":       "Git",
    "write_git_commit":       "Git",
    "write_git_checkout":     "Git",
    "write_git_restore":      "Git",
    # Tasks — kanban card management
    "get_task":               "Tasks",
    "list_tasks":             "Tasks",
    "write_task_status":      "Tasks",
    "write_task_history":     "Tasks",
    "batch_create_cards":     "Tasks",
    # Planning — design documents and architectural artifacts
    "write_arch_doc":         "Planning",
    "write_mermaid":          "Planning",
    "write_interface_contract": "Planning",
    "write_benchmark":        "Planning",
    "write_plan_fields":      "Planning",
    # Testing — test execution and result parsing
    "run_test_pytest":        "Testing",
    "run_test_unittest":      "Testing",
    "run_test_npm":           "Testing",
    "run_test_cargo":         "Testing",
    "run_test_go":            "Testing",
    "read_test_summary":      "Testing",
    # Code Quality — linting and formatting checks
    "run_check_mypy":         "Code Quality",
    "run_check_ruff":         "Code Quality",
    "run_check_black":        "Code Quality",
    # Build — compilation and bundling
    "run_build_make":         "Build",
    "run_build_cargo":        "Build",
    "run_build_go":           "Build",
    "run_build_npm":          "Build",
    "run_build_tsc":          "Build",
    "run_build_gradle":       "Build",
    "run_build_mvn":          "Build",
    # Dependencies — package installation
    "run_deps_pip":           "Dependencies",
    "run_deps_npm":           "Dependencies",
    "run_deps_cargo":         "Dependencies",
    # Security — vulnerability scanning
    "run_audit_bandit":       "Security",
    "run_audit_pip":          "Security",
    "run_audit_semgrep":      "Security",
    "run_audit_npm":          "Security",
    # Web — external search and fetch
    "web_search":             "Web",
    "web_fetch":              "Web",
    # Research — spawning sub-agents
    "spawn_research_agent":   "Research",
    "launch_research_agent":  "Research",
    # Documents — project document store
    "store_document":         "Documents",
    "get_document":           "Documents",
    "search_documents":       "Documents",
    "list_documents":         "Documents",
    # Objectives — autopilot objective tools (Gap 4)
    "get_objective_detail":       "Objectives",
    "get_objective_evidence":     "Objectives",
    "append_objective_evidence":  "Objectives",
    "list_objectives":            "Objectives",
    # Summaries — hierarchical scope summaries
    "get_project_summary":    "Summaries",
    "get_directory_summary":  "Summaries",
    "get_module_summary":     "Summaries",
    "list_scope_summaries":   "Summaries",
    # Math — formal proof and Mathlib tools
    "run_sympy":              "Math",
    "run_lean4":              "Math",
    "search_arxiv":           "Math",
    "search_oeis":            "Math",
    "search_mathlib":         "Math",
    "list_mathlib_topics":    "Math",
    "get_lean4_proof_state":  "Math",
    # Self-modification (Gap 5)
    "vote_to_revert":         "Infrastructure",
    # Episodic memory (Gap 7)
    "query_episodes":         "Infrastructure",
    # Inter-agent messaging (Gap 8)
    "ask_agent":              "Infrastructure",
    "list_active_sessions":   "Infrastructure",
}


CORRECTION_AGENT_TOOLS: list[str] = [
    "read_file",
    "find_in_files",
    "find_files",
    "list_directory",
    "get_task",
    "list_tasks",
    "write_plan_fields",
]


# ---------------------------------------------------------------------------
# Tool schema filter helper
# ---------------------------------------------------------------------------

def build_tool_schemas(allowed_names: list[str]) -> list[dict]:
    """Return a filtered copy of TOOL_SCHEMAS containing only the named tools."""
    allowed = set(allowed_names)
    return [s for s in TOOL_SCHEMAS if s.get("function", {}).get("name") in allowed]


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

def dispatch_tool(name: str, arguments: dict, *, task_id: str | None = None) -> str:
    """
    Route a tool call to its implementation.
    Returns a string result suitable for feeding back to the LLM as a
    tool-role message.
    """
    if name not in TOOL_REGISTRY:
        return f"ERROR: Unknown tool '{name}'. Available tools: {sorted(TOOL_REGISTRY.keys())}"
    func = TOOL_REGISTRY[name]
    logger.debug("Tool call: %s args=%s", name, list(arguments.keys()))
    try:
        result = func(**arguments)
        # Ensure the result is always a string
        if not isinstance(result, str):
            import json
            result = json.dumps(result, default=str)

        # Record to success store before truncation so [EXIT:N] prefix is always present
        if task_id:
            from app.agent.tool_success_store import TRACKED_TOOLS, record, infer_success
            if name in TRACKED_TOOLS:
                record(task_id, name, infer_success(name, result))

        return _cap_tool_result(name, result)
    except TypeError as exc:
        logger.warning("Tool error [%s]: %s", name, exc)
        return f"ERROR: Bad arguments for tool '{name}': {exc}"
    except ValueError as exc:
        logger.warning("Tool error [%s]: %s", name, exc)
        return f"ERROR: {exc}"
    except Exception as exc:
        logger.warning("Tool error [%s]: %s", name, exc)
        return f"ERROR: Unexpected error in tool '{name}': {type(exc).__name__}: {exc}"


async def async_dispatch_tool(
    name: str,
    arguments: dict,
    *,
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
) -> str:
    """
    Async version of dispatch_tool.
    For spawn_research_agent: runs the async research agent.
    For all other tools: falls through to synchronous dispatch_tool.
    """
    if name == "read_file":
        path = arguments.get("path", "")
        start = arguments.get("start")
        end = arguments.get("end")
        count = arguments.get("count")
        
        safe_path = _assert_safe_path(path)
        if not os.path.isfile(safe_path):
            return f"ERROR: '{path}' is not a file or does not exist."
        if _is_binary_path(safe_path):
            return (
                f"ERROR: '{path}' is a binary file (contains null bytes) and cannot be read as text. "
                "Binary files must be inspected with appropriate non-text tools."
            )

        norm_path = os.path.normpath(os.path.realpath(safe_path))
        from app.agent.project_snapshot import _count_file_lines, async_build_file_summary
        
        # FIRST CALL (no range): returns summary (or small file)
        if not _is_file_prepped(safe_path) and start is None and end is None and count is None:
            _mark_file_prepped(safe_path)
            if _count_file_lines(safe_path) <= 25:
                result = _inline_small_file(safe_path)
                _record_served_range(norm_path, 1, _count_file_lines(safe_path))
                return result
            result = await async_build_file_summary(
                safe_path,
                summary_length="brief",
                task_id=task_id,
                llm_id=llm_id,
                budget_id=budget_id,
            )
            return _cap_tool_result("read_file", result)

        # OTHERWISE: use the sync implementation for range logic
        # It handles paged reads (no range) and targeted reads correctly.
        return dispatch_tool(name, arguments)

    if name in ("write_file", "append_file"):
        # Capture old summary BEFORE the write (file still has old content)
        old_summary: str | None = None
        try:
            p = _assert_safe_path(arguments.get("path", ""))
            if os.path.isfile(p) and llm_id is not None:
                from app.database import get_file_summary_by_path
                row = get_file_summary_by_path(p)
                if row:
                    # Prefer short_summary as the change-context hint - it's
                    # concise and purpose-built for this kind of diff prompt.
                    old_summary = (getattr(row, 'short_summary', None) or "").strip() \
                        or row.summary
        except Exception:
            pass

        # Execute the write synchronously
        result = dispatch_tool(name, arguments)

        # If write succeeded: enqueue high-priority summary update + invalidate snapshot
        if result.startswith("OK:") and llm_id is not None:
            try:
                from app.agent.file_summary_agent import enqueue_file_summary
                from app.agent.project_snapshot import clear_snapshot_cache
                safe_p = _assert_safe_path(arguments.get("path", ""))
                enqueue_file_summary(
                    safe_p, task_id=task_id, llm_id=llm_id, budget_id=budget_id,
                    previous_summary=old_summary, priority=-2.0,
                )
                clear_snapshot_cache()
            except Exception as exc:
                logger.debug("post-write summary enqueue failed (non-fatal): %s", exc)
        return result

    if name == "spawn_research_agent":
        try:
            from app.agent.research import run_research
            question = arguments.get("question", "")
            context_str = arguments.get("context", "")
            context_dict = {"question": question, "context": context_str}
            result = await run_research(
                question=question,
                context=context_dict,
                task_id=task_id,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
            return (
                f"Research findings (verdict: {result.vote.get('verdict', '?')}, "
                f"confidence: {result.vote.get('confidence', '?')}):\n\n"
                f"{result.findings}"
            )
        except Exception as exc:
            return f"ERROR: spawn_research_agent failed: {type(exc).__name__}: {exc}"

    if name == "launch_research_agent":
        import asyncio as _asyncio
        import json as _json
        from app.database import create_research_job as _create_rj, get_research_job as _get_rj
        from app.agent.scheduler import (
            get_or_create_completion_event as _get_event,
            park_session as _park,
            unpark_session as _unpark,
            is_shutting_down as _shutting_down,
        )

        question   = arguments.get("question", "").strip()
        context_str = arguments.get("context", "")
        if not question:
            return "ERROR: launch_research_agent requires a non-empty 'question'."

        job = _create_rj(
            task_id=task_id,
            question=question,
            context=_json.dumps({"question": question, "context": context_str}),
            priority=0.0,
            llm_id=llm_id,
            budget_id=budget_id,
        )
        if not job:
            return "ERROR: launch_research_agent failed to create research job in DB."

        # Release this session's LLM slot so the scheduler can dispatch the research
        # job immediately, even if this agent uses the same endpoint.
        completion_key = f"research_job_{job.id}"
        event, _ = _get_event(completion_key)
        session_parked = False
        if task_id and llm_id:
            _park(task_id, llm_id)
            session_parked = True

        MAX_WAIT_SECS = 7200.0   # 2 hours
        POLL_INTERVAL = 30.0
        loop = _asyncio.get_event_loop()
        elapsed = 0.0
        try:
            while not event.is_set() and elapsed < MAX_WAIT_SECS:
                if _shutting_down():
                    return "ERROR: Server shutting down while waiting for research job."
                remaining = min(POLL_INTERVAL, MAX_WAIT_SECS - elapsed)
                done = await loop.run_in_executor(None, event.wait, remaining)
                if done:
                    break
                elapsed += remaining
        finally:
            if session_parked:
                _unpark(task_id, llm_id)

        if not event.is_set():
            return f"ERROR: Research job {job.id} timed out after {MAX_WAIT_SECS:.0f}s."

        result = _get_rj(job.id)
        if not result:
            return f"ERROR: Research job {job.id} record missing after completion."
        if result.status == "failed":
            return f"Research job {job.id} failed — no findings produced."
        if not result.findings:
            return f"Research job {job.id} completed but produced no findings."

        verdict_str = ""
        if result.verdict:
            try:
                v = _json.loads(result.verdict)
                verdict_str = (
                    f" (verdict: {v.get('verdict', '?')}, "
                    f"confidence: {v.get('confidence', '?')})"
                )
            except Exception:
                pass

        return f"Research findings{verdict_str}:\n\n{result.findings}"

    if name == "web_search":
        try:
            query = arguments.get("query", "")
            count = arguments.get("count", 5)
            from app.agent.tools import web_search as sync_web_search
            # 1. Get raw results (cached or fresh)
            raw_json = sync_web_search(query, count)
            if raw_json.startswith("ERROR:"):
                return raw_json

            import json as _json
            data = _json.loads(raw_json)
            results = data.get("results", [])

            # 2. Run synthesis agent
            from app.agent.research import WebSearchAgent
            agent = WebSearchAgent(
                query=query,
                results=results,
                task_id=task_id,
                llm_id=llm_id,
                budget_id=budget_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
            )
            return await agent.run()
        except Exception as exc:
            return f"ERROR: agentic web_search failed: {exc}"

    if name == "consult_maestro":
        from app.agent.config import CONSULT_MAX_CALLS_PER_SESSION
        question = arguments.get("question", "").strip()
        if not question:
            return "ERROR: consult_maestro requires a non-empty 'question'."

        effective_task_id = task_id or "_unknown"
        call_count = _increment_consult_count(effective_task_id)

        if call_count > CONSULT_MAX_CALLS_PER_SESSION:
            return (
                f"You have reached the consult_maestro limit for this session "
                f"({CONSULT_MAX_CALLS_PER_SESSION} calls).  Make your best judgment "
                "with the information available and proceed."
            )

        # Resolve project context for ConsultAgent
        project_name: str | None = None
        project_maestro_llm_id: int | None = None
        if task_id:
            try:
                from app.database import get_task as _get_task_rec, get_project as _get_proj
                task_rec = _get_task_rec(task_id)
                if task_rec:
                    project_name = getattr(task_rec, "project", None)
                    if project_name:
                        proj = _get_proj(project_name)
                        if proj:
                            project_maestro_llm_id = getattr(proj, "maestro_llm_id", None)
            except Exception as _exc:
                logger.debug("consult_maestro: project lookup failed: %s", _exc)

        try:
            from app.agent.consult_agent import run_consult_agent
            answer = await run_consult_agent(
                question=question,
                task_id=effective_task_id,
                caller_llm_id=llm_id,
                budget_id=budget_id,
                project_name=project_name,
                project_maestro_llm_id=project_maestro_llm_id,
            )
            return answer
        except Exception as exc:
            return f"ERROR: consult_maestro failed: {type(exc).__name__}: {exc}"

    if name == "ask_agent":
        from app.agent.config import ASK_AGENT_MAX_DEPTH
        target_session_id = arguments.get("target_session_id", "").strip()
        question = arguments.get("question", "").strip()
        if not target_session_id or not question:
            return "ERROR: ask_agent requires non-empty 'target_session_id' and 'question'."

        ask_depth = _ask_depth_ctx.get()
        if ask_depth >= ASK_AGENT_MAX_DEPTH:
            return (
                f"Max inter-agent ask depth ({ASK_AGENT_MAX_DEPTH}) reached. "
                "Make your best judgment with the information available."
            )

        effective_task_id = task_id or "_unknown"
        if effective_task_id == target_session_id:
            return "Cannot ask yourself. Use list_active_sessions() to find a different target."

        from app.agent.scheduler import get_active_session_info as _get_session_info
        session_info = _get_session_info(target_session_id)
        if session_info is None:
            return (
                f"Session '{target_session_id}' is not active. "
                "Use list_active_sessions() to see current sessions."
            )

        # TODO KV cache checkpoint: if KV serialization were implemented,
        # the parent session's context would be written to disk here, allowing
        # the parent's prompt prefix to be restored from cache after this call.

        try:
            from app.agent.inter_agent_session import InterAgentSession
            session = InterAgentSession(
                question=question,
                target_task_id=target_session_id,
                calling_task_id=effective_task_id,
                calling_session_id=_session_id_ctx.get(),
                ask_depth=ask_depth + 1,
                llm_id=session_info.get("llm_id") or llm_id,
                budget_id=budget_id,
            )
            return await session.run()
        except Exception as exc:
            return f"ERROR: ask_agent failed: {type(exc).__name__}: {exc}"

    if name == "list_active_sessions":
        import json as _json
        from app.agent.scheduler import list_active_sessions as _list_sessions
        project_filter = arguments.get("project") or None
        calling_task_id = task_id
        sessions = _list_sessions(exclude_task_id=calling_task_id, project_filter=project_filter)
        if not sessions:
            return "No other active agent sessions found."
        return _json.dumps(sessions, indent=2)

    # All other tools - synchronous
    return dispatch_tool(name, arguments, task_id=task_id)
