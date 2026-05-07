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
)
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
    """Drop any served-range record for path so the next read_file returns fresh content."""
    try:
        norm = os.path.normpath(os.path.realpath(path))
    except OSError:
        return
    _get_prepped_files().pop(norm, None)


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
    result = [f"== FILE: {safe_path} (lines {start}-{end} of {total}) =="]
    for i in range(start, end + 1):
        result.append(f"{i}: {all_lines[i - 1].rstrip()}")
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


def set_task_git_cwd(path: str | None) -> None:
    """
    Set the git working directory for all git tool calls in the current context.

    Call this before dispatching any tools for a task, passing the filesystem
    path of the project the task belongs to. Pass None to clear the override
    (git tools will error rather than fall back to TheMaestro's own repo).

    Returns the contextvars.Token so the caller can restore the previous value
    if needed (e.g. in tests).
    """
    _task_git_cwd.set(path)


def get_task_git_cwd() -> str | None:
    """Return the currently active task git working directory, or None."""
    return _task_git_cwd.get()


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

    Returns the resolved absolute path.
    Raises ValueError with a descriptive message on any violation.
    """
    resolved = _assert_safe_path(path)   # Layer 0: blocks .git + .archive
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    root = os.path.realpath(effective_root)

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
        _git_run(["git", "add", safe_path])
        return f"OK: wrote {len(content)} chars to '{path}' and staged for git.{archived_msg}"
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
        return f"OK: appended {len(content)} chars to '{path}'."
    except OSError as exc:
        return f"ERROR appending to '{path}': {exc}"


def patch_file(path: str, old_str: str, new_str: str) -> str:
    """[WRITE — files] Replace an exact string in a file. old_str must appear exactly once.
    Auto-stages for git. Path must be inside project root.
    Use this instead of write_file when making targeted edits — avoids full-file rewrites.
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
    count = original.count(old_str)
    if count == 0:
        # Detailed diagnostics for common whitespace/line-ending mismatches
        msg = [f"ERROR: old_str not found in '{path}'."]
        
        # 1. Check for basic presence of the text ignoring whitespace
        import re
        def _canonical(s): return re.sub(r"\s+", "", s)
        if _canonical(old_str) in _canonical(original):
            msg.append("HINT: The text exists but whitespace/indentation does not match exactly.")
            if "\t" in old_str or "\t" in original:
                old_has_tabs = "\t" in old_str
                file_has_tabs = "\t" in original
                if old_has_tabs != file_has_tabs:
                    msg.append(f"DIAGNOSTIC: Your string {'has' if old_has_tabs else 'lacks'} TABS, but the file {'has' if file_has_tabs else 'lacks'} them.")
            
            # Find the closest match to show the user what they missed
            lines = original.splitlines()
            old_lines = old_str.splitlines()
            if old_lines:
                first_line_clean = old_lines[0].strip()
                for i, line in enumerate(lines, 1):
                    if first_line_clean in line:
                        msg.append(f"DIAGNOSTIC: Found similar text on line {i}:")
                        msg.append(f"  FILE: '{line.replace('\t', '\\t')}'")
                        msg.append(f"  YOUR: '{old_lines[0].replace('\t', '\\t')}'")
                        break
        else:
            msg.append("HINT: The text was not found even after ignoring whitespace. Please call read_file() again to verify the content.")
        
        msg.append("Check whitespace, indentation, and line endings (\\n vs \\r\\n).")
        return "\n".join(msg)
    if count > 1:
        return (
            f"ERROR: old_str appears {count} times in '{path}' — patch is ambiguous. "
            "Extend old_str to include more surrounding context so it matches exactly once."
        )
    # Locate the line range of old_str and verify those lines have been served.
    char_offset = original.index(old_str)
    start_line = original[:char_offset].count("\n") + 1
    end_line = start_line + old_str.count("\n")
    norm = os.path.normpath(os.path.realpath(safe_path))
    unserved = _next_unserved_range(norm, start_line, end_line)
    if unserved is not None:
        return (
            f"ERROR: lines {start_line}-{end_line} of '{path}' have not been read yet. "
            f"Call read_file('{path}', start={start_line}, end={end_line}) first "
            "so the exact text is in your context before patching."
        )
    # Archive a copy of the pre-patch file before making any changes.
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    rel_path = os.path.relpath(os.path.realpath(safe_path), os.path.realpath(effective_root))
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
    archive_dest = os.path.join(_effective_archive_dir(), timestamp, rel_path)
    try:
        os.makedirs(os.path.dirname(archive_dest), exist_ok=True)
        shutil.copy2(safe_path, archive_dest)
    except OSError as exc:
        return f"ERROR: could not archive pre-patch copy of '{path}': {exc}"

    patched = original.replace(old_str, new_str, 1)
    try:
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(patched)
    except OSError as exc:
        return f"ERROR writing '{path}': {exc}"
    _invalidate_prepped_cache(safe_path)
    _git_run(["git", "add", safe_path])
    lines_changed = new_str.count("\n") - old_str.count("\n")
    return (
        f"OK: patched '{path}' (lines {start_line}-{end_line}, net {lines_changed:+d} lines). "
        f"Staged for git. Pre-patch copy archived to '{archive_dest}'."
    )


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


def list_directory(path: str = ".") -> str:
    """[READ] List files and directories at the given path. No state change.

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

    return "\n".join(lines) if lines else "(empty directory)"


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


def find_files(glob_pattern: str, directory: str = ".") -> str:
    """
    Find files matching a glob pattern under directory.
    Returns one path per line (up to 200 results).
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
    return "\n".join(lines)


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

    return (
        f"OK: archived '{path}' -> '{dest}'.\n"
        f"Restore by copying: shutil.copy(r'{dest}', r'{safe_path}')"
    )


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
            logger.warning("git %s failed: %s", args[1:], stderr)
        return rc, stdout, stderr
    except Exception as exc:
        return 1, "", str(exc)


def read_git_status() -> str:
    """[READ] Return the current git status of the project. No state change."""
    rc, out, err = _git_run(["git", "status"])
    if rc != 0:
        return f"ERROR: git status failed: {err}"
    return out


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


def read_git_blame(path: str) -> str:
    """[READ] Show git blame for a file (last-modified info per line). No state change."""
    try:
        safe_path = _assert_safe_path(path)
    except ValueError as exc:
        return f"BLOCKED: {exc}"
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    rc, out, err = _git_run(["git", "blame", safe_path])
    if rc != 0:
        return f"ERROR: git blame failed: {err}"
    return out or "(no blame output)"


def read_git_show(ref: str, path: str | None = None) -> str:
    """[READ] Show a file at a git ref, or commit details (message + diffstat). No state change."""
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
    return out or "(no output)"


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
    """[WRITE — git] Stage all tracked changes and create a commit. Permanent record — reversible only via a revert commit."""
    _git_run(["git", "add", "-u"])
    rc, out, err = _git_run(["git", "commit", "-m", message])
    if rc != 0:
        if "nothing to commit" in err or "nothing to commit" in out:
            return "OK: nothing to commit - working tree clean."
        return f"ERROR: git commit failed: {err}"
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




def submit_work(signal: str, summary: str, payload: dict | None = None) -> str:
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

        # Truncate to reasonable length for context
        if len(clean_text) > 8000:
            clean_text = clean_text[:8000] + "\n... (content truncated)"

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
        serialized = {
            k: (v if isinstance(v, str) else _json.dumps(v))
            for k, v in fields.items()
        }
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


# --- Testing / linting ---

def run_test_pytest(
    path: str = ".",
    flags: str = "",
    head: int | None = None,
    tail: int | None = None,
    grep: str | None = None,
) -> str:
    """[RUN — sandbox] Run pytest. path: file or dir (default '.'). flags: extra pytest flags. Per-test timeout injected automatically. head/tail/grep filter output. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"python -m pytest {path}"
    if flags:
        cmd += f" {flags}"
    # Inject per-test timeout unless the project config already sets one.
    if "--timeout" not in cmd:
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
            injected = max(60, SHELL_TIMEOUT_SECONDS // 2) if SHELL_TIMEOUT_SECONDS > 60 else SHELL_TIMEOUT_SECONDS
            cmd = cmd.rstrip() + f" --timeout={injected}"
    timeout_msg = (
        f"ERROR: Command timed out after {SHELL_TIMEOUT_SECONDS}s. "
        "This may indicate a hang, infinite loop, or high computational complexity."
    )
    result = _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, timeout_msg, replace_python=True)
    _last_test_output.set(result)
    return _slice_output(result, head=head, tail=tail, grep=grep)


def run_check_mypy(path: str, flags: str = "") -> str:
    """[RUN — sandbox] Run mypy type-checker. path: file or package. flags: extra mypy flags. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"python -m mypy {path}"
    if flags:
        cmd += f" {flags}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: mypy timed out after {SHELL_TIMEOUT_SECONDS}s.", replace_python=True)


def run_check_ruff(path: str = ".", flags: str = "") -> str:
    """[RUN — sandbox] Run ruff linter. path: file or dir (default '.'). flags: extra ruff flags. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"python -m ruff check {path}"
    if flags:
        cmd += f" {flags}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: ruff timed out after {SHELL_TIMEOUT_SECONDS}s.", replace_python=True)


def run_check_black(path: str = ".") -> str:
    """[RUN — sandbox] Check formatting with black (read-only — does not modify files). path: file or dir (default '.')."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    return _execute_in_project(f"python -m black --check {path}", cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: black timed out after {SHELL_TIMEOUT_SECONDS}s.", replace_python=True)


def run_test_unittest(args: str = "") -> str:
    """[RUN — sandbox] Run Python unittest discovery or a specific test module/class/method. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "python -m unittest"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: unittest timed out after {SHELL_TIMEOUT_SECONDS}s.", replace_python=True)


def run_test_npm(args: str = "") -> str:
    """[RUN — sandbox] Run npm test. args: additional arguments for the test runner. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "npm test"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: npm test timed out after {SHELL_TIMEOUT_SECONDS}s.")


def run_test_cargo(args: str = "") -> str:
    """[RUN — sandbox] Run cargo test. args: extra cargo test flags. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "cargo test"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: cargo test timed out after {SHELL_TIMEOUT_SECONDS}s.")


def run_test_go(path: str = "./...", flags: str = "") -> str:
    """[RUN — sandbox] Run go test. path: package path (default './...'). flags: extra go test flags. No project-file mutation."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"go test {path}"
    if flags:
        cmd += f" {flags}"
    return _execute_in_project(cmd, cwd, SHELL_TIMEOUT_SECONDS, f"ERROR: go test timed out after {SHELL_TIMEOUT_SECONDS}s.")


# --- Build ---

def run_build_make(target: str, args: str = "") -> str:
    """[RUN — build] Run a Makefile target. target: e.g. 'build', 'test', 'all'. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"make {target}"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: make timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_cargo(args: str = "") -> str:
    """[RUN — build] Build a Rust/Cargo project (cargo build). Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "cargo build"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: cargo build timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_go(args: str = "") -> str:
    """[RUN — build] Build a Go project (go build). Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "go build"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: go build timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_npm(script: str = "build") -> str:
    """[RUN — build] Run an npm build script (npm run <script>). Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    return _execute_in_project(f"npm run {script}", cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: npm build timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_tsc(args: str = "") -> str:
    """[RUN — build] Run the TypeScript compiler (tsc). Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "tsc"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: tsc timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_gradle(target: str, args: str = "") -> str:
    """[RUN — build] Run a Gradle task. target: e.g. 'build', 'assemble'. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"gradle {target}"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: gradle timed out after {_BUILD_TIMEOUT_SECONDS}s.")


def run_build_mvn(goal: str, args: str = "") -> str:
    """[RUN — build] Run a Maven goal. goal: e.g. 'package', 'compile'. Creates build artifacts inside the project."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = f"mvn {goal}"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _BUILD_TIMEOUT_SECONDS, f"ERROR: mvn timed out after {_BUILD_TIMEOUT_SECONDS}s.")


# --- Dependencies ---

def run_deps_pip(args: str) -> str:
    """[RUN — deps] Install Python packages with pip. MUTATES environment. args: e.g. '-r requirements.txt', 'requests>=2.28', '-e .'."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    return _execute_in_project(f"python -m pip install {args}", cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: pip install timed out after {_DEPS_TIMEOUT_SECONDS}s.", replace_python=True)


def run_deps_npm(args: str = "") -> str:
    """[RUN — deps] Install Node.js dependencies (npm install). MUTATES environment. Call after modifying package.json."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    cmd = "npm install"
    if args:
        cmd += f" {args}"
    return _execute_in_project(cmd, cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: npm install timed out after {_DEPS_TIMEOUT_SECONDS}s.")


def run_deps_cargo() -> str:
    """[RUN — deps] Fetch Rust/Cargo dependencies (cargo fetch). MUTATES environment. Call after modifying Cargo.toml."""
    cwd = _task_git_cwd.get()
    if cwd is None:
        return "ERROR: No task git working directory configured."
    return _execute_in_project("cargo fetch", cwd, _DEPS_TIMEOUT_SECONDS, f"ERROR: cargo fetch timed out after {_DEPS_TIMEOUT_SECONDS}s.")


# --- Security scanners ---

def run_audit_bandit(path: str = ".", args: str = "") -> str:
    """[RUN — audit] Run bandit Python security linter. No project-file mutation. path: dir or file (default '.')."""
    cmd = f"python -m bandit -r {path}"
    if args:
        cmd += f" {args}"
    from app.agent.security_review import run_shell_security
    return run_shell_security(cmd)


def run_audit_pip() -> str:
    """[RUN — audit] Audit installed Python packages for known vulnerabilities (pip-audit). No project-file mutation."""
    from app.agent.security_review import run_shell_security
    return run_shell_security("python -m pip audit")


def run_audit_semgrep(path: str = ".", config: str = "auto") -> str:
    """[RUN — audit] Run semgrep static analysis. No project-file mutation. config: ruleset (default 'auto')."""
    from app.agent.security_review import run_shell_security
    return run_shell_security(f"semgrep --config {config} {path}")


def run_audit_npm() -> str:
    """[RUN — audit] Run npm audit to check Node.js dependencies for vulnerabilities. No project-file mutation."""
    from app.agent.security_review import run_shell_security
    return run_shell_security("npm audit")


# ---------------------------------------------------------------------------
# Shared subprocess runner (internal — not exposed as a tool)
# ---------------------------------------------------------------------------

_BUILD_TIMEOUT_SECONDS = 300
_DEPS_TIMEOUT_SECONDS = 600


def _execute_in_project(
    command: str,
    cwd: str,
    timeout: int,
    timeout_msg: str,
    *,
    replace_python: bool = False,
) -> str:
    """Run command in the project cwd. Sets up venv; optionally swaps bare python for project venv."""
    from app.agent.worktree import setup_test_environment, venv_python as _venv_python
    setup_test_environment(cwd)

    if replace_python:
        _py = _venv_python(cwd)
        if _py != "python":
            command = re.sub(r"^python\b", _py.replace("\\", "/"), command)

    try:
        proc = subprocess.Popen(
            command,
            shell=True,
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
            return timeout_msg
        output = stdout or ""
        rc = proc.returncode
        return output if output else f"EXIT_CODE: {rc}"
    except Exception as exc:
        return f"ERROR running command: {exc}"


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


def read_diff_stat(since: str = "main") -> str:
    """[READ] git diff --stat from <since> to HEAD, parsed into added/removed per file. No state change."""
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
            return result.stdout.strip()
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
                    if method.name == name or name.lower() in method.name.lower():
                        results.append(f"{path}:{getattr(method, 'line', '?')} method {cls.name}.{method.name}")
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
    passed = failed = errors = 0
    failing_names: list[str] = []
    for line in output.splitlines():
        if " passed" in line:
            for part in line.split(","):
                part = part.strip()
                if "passed" in part:
                    try:
                        passed = int(part.split()[0])
                    except (ValueError, IndexError):
                        pass
        if " failed" in line:
            for part in line.split(","):
                part = part.strip()
                if "failed" in part:
                    try:
                        failed = int(part.split()[0])
                    except (ValueError, IndexError):
                        pass
        if " error" in line.lower():
            for part in line.split(","):
                part = part.strip()
                if "error" in part.lower():
                    try:
                        errors = int(part.split()[0])
                    except (ValueError, IndexError):
                        pass
        if line.startswith("FAILED "):
            failing_names.append(line[7:].strip())
    import json as _json
    return _json.dumps({
        "passed": passed,
        "failed": failed,
        "errors": errors,
        "failing_names": failing_names,
    }, indent=2)


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
    # Terminal tool
    "submit_work": submit_work,
    # Survey/project summary tools
    "get_project_summary": get_project_summary,
    "get_directory_summary": get_directory_summary,
    "get_module_summary": get_module_summary,
    "list_scope_summaries": list_scope_summaries,
}

TOOL_SCHEMAS: list[dict] = [
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
                "REQUIRES the specific lines being edited to have been served by read_file() — enforced. "
                "old_str must match exactly once — extend it with more surrounding lines if ambiguous. "
                "Fails with a clear error if old_str appears 0 or 2+ times. "
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
                            "Exact text to find and replace. Must appear exactly once in the file. "
                            "Include enough surrounding context (blank lines, function signatures) "
                            "to be unambiguous. Whitespace and indentation must match precisely."
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
            "description": "[READ] List files and subdirectories at a given path with annotations for gitignored/excluded/protected entries. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path to list.", "default": "."},
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
            "description": "[READ] Find files by glob pattern (e.g. '*.py', 'test_*.py'). No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "glob_pattern": {"type": "string", "description": "Glob pattern to match filenames."},
                    "directory": {"type": "string", "description": "Root directory to search from.", "default": "."},
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
            "description": "[READ] git diff --stat from <since> to HEAD, showing added/removed lines per file. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since": {"type": "string", "description": "Base ref (branch, tag, or commit). Default: 'main'.", "default": "main"},
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
            "description": "[READ] Return the current git status of the project. No state change.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_diff",
            "description": "[READ] Return git diff (staged + unstaged). Optionally scoped to a path. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional path to scope the diff."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_log",
            "description": "[READ] Return recent git log entries. Optionally scoped to a specific file. No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Optional file path to scope the log to."},
                    "max_count": {"type": "integer", "description": "Maximum number of log entries to return (1-100, default 20).", "default": 20},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_blame",
            "description": "[READ] Show git blame for a file (last-modified info per line). No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file to blame."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_git_show",
            "description": "[READ] Show a file's content at a specific git ref, or show commit details (message + diffstat). No state change.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ref": {"type": "string", "description": "Git ref (commit hash, branch, tag, HEAD~N, etc.)."},
                    "path": {"type": "string", "description": "Optional file path to show at the given ref. If omitted, shows commit details + diffstat."},
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
                    "path": {"type": "string", "description": "File or directory to test (default: '.' for all tests)."},
                    "flags": {"type": "string", "description": "Additional pytest flags (e.g. '-v', '-k test_foo', '--tb=short')."},
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
            "description": "[RUN — sandbox] Run mypy type-checker on the given path. The project venv's Python is used. Does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or package to type-check."},
                    "flags": {"type": "string", "description": "Additional mypy flags (e.g. '--strict', '--ignore-missing-imports')."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check_ruff",
            "description": "[RUN — sandbox] Run ruff linter on the given path. Check-only; does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to lint (default: '.')."},
                    "flags": {"type": "string", "description": "Additional ruff flags (e.g. '--fix', '--select E,F')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_check_black",
            "description": "[RUN — sandbox] Check code formatting with black. Read-only — does not modify files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to check (default: '.')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_unittest",
            "description": "[RUN — sandbox] Run Python unittest discovery or a specific test module/class/method. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Module, class, or method (e.g. 'tests.test_foo'). Leave empty for full discovery."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_npm",
            "description": "[RUN — sandbox] Run npm test in the task's project directory. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional arguments to pass to the test runner."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_cargo",
            "description": "[RUN — sandbox] Run cargo test in the task's project directory. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional cargo test flags (e.g. '--release', '-- --nocapture')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_test_go",
            "description": "[RUN — sandbox] Run go test in the task's project directory. Does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Package path to test (default: './...' for all packages)."},
                    "flags": {"type": "string", "description": "Additional go test flags (e.g. '-v', '-run TestFoo')."},
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
            "description": "[RUN — build] Run a Makefile target in the task's project directory. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Makefile target (e.g. 'build', 'test', 'lint', 'all')."},
                    "args": {"type": "string", "description": "Additional make arguments."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_cargo",
            "description": "[RUN — build] Build a Rust/Cargo project (cargo build). Writes build artifacts to target/.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional cargo build flags (e.g. '--release')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_go",
            "description": "[RUN — build] Build a Go project (go build). Writes binary output inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional go build flags (e.g. '-o bin/app', './cmd/...')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_npm",
            "description": "[RUN — build] Run an npm build script (npm run <script>). Writes build artifacts inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "script": {"type": "string", "description": "The npm script name (default: 'build')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_tsc",
            "description": "[RUN — build] Run the TypeScript compiler (tsc). Writes compiled output inside the project.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional tsc flags (e.g. '--noEmit', '--watch')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_gradle",
            "description": "[RUN — build] Run a Gradle task in the task's project directory. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "Gradle task name (e.g. 'build', 'test', 'assemble')."},
                    "args": {"type": "string", "description": "Additional Gradle flags."},
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_build_mvn",
            "description": "[RUN — build] Run a Maven goal in the task's project directory. May write build artifacts.",
            "parameters": {
                "type": "object",
                "properties": {
                    "goal": {"type": "string", "description": "Maven lifecycle phase or plugin goal (e.g. 'package', 'test', 'compile')."},
                    "args": {"type": "string", "description": "Additional Maven flags (e.g. '-DskipTests')."},
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
                "Call after modifying requirements.txt or pyproject.toml. "
                "Examples: run_deps_pip('-r requirements.txt'), run_deps_pip('requests>=2.28')."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "pip install arguments (e.g. '-r requirements.txt', 'requests>=2.28', '-e .')."},
                },
                "required": ["args"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deps_npm",
            "description": "[RUN — deps] Install Node.js dependencies (npm install). Mutates node_modules. Call after modifying package.json.",
            "parameters": {
                "type": "object",
                "properties": {
                    "args": {"type": "string", "description": "Additional npm install flags (e.g. '--save-dev')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_deps_cargo",
            "description": "[RUN — deps] Fetch Rust/Cargo dependencies (cargo fetch). Mutates the local cargo registry cache. Call after modifying Cargo.toml.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    # ---- Named security scanner tools ----
    {
        "type": "function",
        "function": {
            "name": "run_audit_bandit",
            "description": "[RUN — audit] Run the bandit Python security linter. Read-only; does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory or file to scan (default: '.')."},
                    "args": {"type": "string", "description": "Additional bandit flags (e.g. '-ll', '--skip B101')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_pip",
            "description": "[RUN — audit] Audit installed Python packages for known vulnerabilities (pip-audit). Read-only.",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_semgrep",
            "description": "[RUN — audit] Run semgrep static analysis. Read-only; does not modify project files.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "File or directory to scan (default: '.')."},
                    "config": {"type": "string", "description": "Semgrep config/ruleset (default: 'auto')."},
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_audit_npm",
            "description": "[RUN — audit] Run npm audit to check Node.js dependencies for known vulnerabilities. Read-only.",
            "parameters": {
                "type": "object",
                "properties": {},
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
    # ---- Terminal tool ----
    {
        "type": "function",
        "function": {
            "name": "submit_work",
            "description": (
                "[FINISH] Terminate your session and report outcome. "
                "Call this ONCE at the very end — it immediately stops the agent loop. "
                "Choose signal: ACCEPTED (work done, tests pass), "
                "REVERT_TO_DESIGN (blocked by design flaw or exhausted retries), "
                "SUBDIVIDE (task too large, needs breakdown into sub-tasks), "
                "PLAN_UPDATED (planning correction complete)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "signal": {
                        "type": "string",
                        "enum": ["ACCEPTED", "REVERT_TO_DESIGN", "SUBDIVIDE", "PLAN_UPDATED"],
                        "description": (
                            "Terminal signal: ACCEPTED (task complete), REVERT_TO_DESIGN "
                            "(task impossible/needs re-plan), SUBDIVIDE (needs further breakdown), "
                            "PLAN_UPDATED (correction complete)."
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
                },
                "required": ["signal", "summary"],
            },
        },
    },
]


# ---------------------------------------------------------------------------
# Tool list for PlanningCorrectionAgent
# ---------------------------------------------------------------------------

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

def dispatch_tool(name: str, arguments: dict) -> str:
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

    # All other tools - synchronous
    return dispatch_tool(name, arguments)
