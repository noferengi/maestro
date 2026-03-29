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
import logging
import os
import re
import shutil
import subprocess
import sys
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

# ---------------------------------------------------------------------------
# Per-task git working directory
# ---------------------------------------------------------------------------
# Using a ContextVar so parallel agent sessions (each in their own thread or
# asyncio task) each have an independent working directory. Never defaults to
# TheMaestro's own source tree — always requires explicit configuration per task.
_task_git_cwd: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "_task_git_cwd", default=None
)

# ---------------------------------------------------------------------------
# Per-task file read-range tracking
# ---------------------------------------------------------------------------
# Maps normalised abs-path → sorted, merged list of (start, end) inclusive
# line intervals already delivered to the LLM in this session.
# A path being present (even with an empty interval list) means read_file()
# has been called on it at least once.
#
# Maximum lines served per call — shared by both read_file and read_file_harder.
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
    result = [f"== FILE: {safe_path} (lines {start}–{end} of {total}) =="]
    for i in range(start, end + 1):
        result.append(f"{i}: {all_lines[i - 1].rstrip()}")
    return "\n".join(result)


def _served_ranges_str(norm_path: str) -> str:
    served = _get_prepped_files().get(norm_path, [])
    return ", ".join(f"{s}–{e}" for s, e in served) if served else "none"


# Paths where git init has already been attempted this process lifetime.
# Prevents repeated init attempts if the first one fails.
_git_init_attempted: set[str] = set()


def ensure_git_repo(path: str) -> None:
    """If ``path`` has no .git directory, attempt ``git init`` once.

    Subsequent calls for the same path (including after failure) are no-ops.
    Logs the outcome but never raises — callers should proceed and let the
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
# Add entries there to extend — no code change required.
# The root .archive folder and .git are always excluded by absolute path
# regardless of this set.

LISTING_EXCLUDED_DIRS: set[str] = TOOL_LISTING_EXCLUDED_DIRS

BLOCKED_PATTERNS: list[str] = [
    r"rm\s+-[rRfF]",           # rm -rf / rm -fr etc.
    r"del\s+/[sfSF]",          # del /s /f
    r"rmdir\s+/[sS]",          # rmdir /s
    r"shutil\.rmtree",         # Python rmtree in a shell string
    r"os\.remove",             # Python os.remove in a shell string
    r"os\.unlink",             # Python os.unlink in a shell string
    r"format\s+[a-zA-Z]:",     # format C:
    r"mkfs",                   # mkfs.*
    r">\s*/dev/",              # redirect to /dev/
    r"dd\s+if=",               # dd if= (disk destroyer)
    r":\(\)\{",                # fork bomb :(){ :|:& };:
    r"\.\./\.\./\.\.",         # deep path traversal ../../..
    r"shutdown",               # shutdown / restart
    r"reboot",
    r"halt",
    r"poweroff",
    r"curl\s+.*\|\s*[sb]ash",  # curl | bash / curl | sh
    r"wget\s+.*\|\s*[sb]ash",  # wget | bash / wget | sh
    r"eval\s+\$\(",            # eval $(...) injection
]

_BLOCKED_RE = re.compile("|".join(BLOCKED_PATTERNS), re.IGNORECASE)


def _assert_safe_path(path: str) -> str:
    """
    Resolve path and assert it stays inside the effective project root.

    When a task git working directory has been set via set_task_git_cwd(),
    containment is checked against that path instead of PROJECT_ROOT.
    This allows agents to operate on managed projects without escaping
    their own tree.

    Returns the absolute resolved path string.
    Raises ValueError if the path escapes the effective root.
    """
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    # Resolve relative paths against the effective project root, not the process CWD.
    # Without this, 'PRD.md' would resolve to TheMaestro's directory when an agent
    # is operating on a different project (e.g. AndroidStreetPass).
    if os.path.isabs(path):
        resolved = os.path.realpath(path)
    else:
        resolved = os.path.realpath(os.path.join(effective_root, path))
    root = os.path.realpath(effective_root)
    if not resolved.startswith(root):
        logger.warning("Path escape attempt: %s outside %s", resolved, effective_root)
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}' which is outside "
            f"the effective project root '{root}'. Access denied."
        )
    return resolved


def _assert_archivable(path: str) -> str:
    """
    Extended safety check used exclusively by archive_file.

    Rules (in priority order):
      1. Path must be inside PROJECT_ROOT (delegates to _assert_safe_path).
      2. Path must NOT touch .git or anything inside it — git history is
         permanently protected. Archiving git internals would destroy the repo.
      3. Path must NOT be inside the root archive directory (ARCHIVE_DIR).
         Re-archiving an already-archived file makes no sense and is rejected
         with instructions on how to restore instead.

    Returns the resolved absolute path on success.
    Raises ValueError with a descriptive message on any violation.
    """
    safe = _assert_safe_path(path)          # raises ValueError if outside PROJECT_ROOT
    root = os.path.realpath(PROJECT_ROOT)
    git_dir = os.path.join(root, ".git")
    archive_root = os.path.realpath(ARCHIVE_DIR)

    # Hard reject: .git folder and every path inside it
    if safe == git_dir or safe.startswith(git_dir + os.sep):
        raise ValueError(
            f"HARD REJECTION: '{path}' is inside the .git folder. "
            "Archiving git internals would permanently destroy repository history. "
            "This operation is blocked and cannot be overridden."
        )

    # Hard reject: root archive dir and every path inside it
    if safe == archive_root or safe.startswith(archive_root + os.sep):
        raise ValueError(
            f"HARD REJECTION: '{path}' is already inside the archive directory "
            f"'{ARCHIVE_DIR}'. Cannot re-archive an archived file. "
            "If you need to restore it, see the undelete instructions returned "
            "by calling archive_file on the original path."
        )

    return safe


def _find_archived_copies(rel_path: str) -> list[str]:
    """
    Scan ARCHIVE_DIR for all previously archived copies of rel_path.
    rel_path must be relative to PROJECT_ROOT.
    Returns a list of absolute paths ordered most-recent first.
    """
    archive_root = os.path.realpath(ARCHIVE_DIR)
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


def _is_command_blocked(command: str) -> tuple[bool, str]:
    """Return (blocked, reason) for a shell command string."""
    match = _BLOCKED_RE.search(command)
    if match:
        logger.warning("Shell command blocked by pattern %r: %s", match.group(), command)
        return True, f"Command contains blocked pattern: '{match.group()}'"
    return False, ""


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    """Read a file's structure: classes, functions, imports, and line ranges.

    Returns a structural summary instead of raw content on the first call.
    Subsequent calls serve the next 250 unserved source lines.
    Use read_file_harder() to read a specific line range.

    Note: async_dispatch_tool handles the LLM-summary path automatically.
    This sync fallback always returns the structural summary only on first call.
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    norm = os.path.normpath(os.path.realpath(safe_path))
    from app.agent.project_snapshot import _count_file_lines, build_file_summary
    # Subsequent call — serve next unserved source lines
    if norm in _get_prepped_files():
        total = _count_file_lines(safe_path)
        unserved = _next_unserved_range(norm, 1, total)
        if unserved is None:
            rel = os.path.relpath(safe_path, PROJECT_ROOT)
            return (
                f"ALREADY IN CONTEXT: all lines of '{rel}' have been served "
                f"(ranges: {_served_ranges_str(norm)}). "
                f"Call read_file_harder('{rel}', start=N) for a specific range."
            )
        return _serve_file_lines(safe_path, unserved[0], unserved[1])
    # First call — structural summary
    _mark_file_prepped(safe_path)
    if _count_file_lines(safe_path) <= 25:
        result = _inline_small_file(safe_path)
        # Whole file shown inline — record all lines as served
        try:
            with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
                lc = sum(1 for _ in fh)
            _record_served_range(norm, 1, lc)
        except OSError:
            pass
        return result
    return build_file_summary(safe_path)


_SMALL_FILE_HEADER = "== FILE (full content): {path} =="


def _inline_small_file(abs_path: str) -> str:
    """Return raw content for tiny files (≤ 25 lines), with a header."""
    try:
        with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
            content = fh.read()
        header = f"== FILE (full content): {abs_path} =="
        return f"{header}\n{content}"
    except OSError as exc:
        return f"ERROR reading '{abs_path}': {exc}"


def write_file(path: str, content: str) -> str:
    """
    Write (overwrite) a file with the given content.
    Auto-stages the file for git tracking after writing.
    """
    safe_path = _assert_safe_path(path)
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "w", encoding="utf-8") as fh:
            fh.write(content)
        # Stage for git
        _git_run(["git", "add", safe_path])
        return f"OK: wrote {len(content)} chars to '{path}' and staged for git."
    except OSError as exc:
        return f"ERROR writing '{path}': {exc}"


def append_file(path: str, content: str) -> str:
    """Append content to the end of a file (creates the file if absent)."""
    safe_path = _assert_safe_path(path)
    try:
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        with open(safe_path, "a", encoding="utf-8") as fh:
            fh.write(content)
        _git_run(["git", "add", safe_path])
        return f"OK: appended {len(content)} chars to '{path}'."
    except OSError as exc:
        return f"ERROR appending to '{path}': {exc}"


def _get_cached_summary_for_listing(abs_path: str) -> "str | None":
    """Sync DB lookup for a file's cached summary. Returns first 160 chars or None."""
    try:
        from app.database import get_file_summary_by_path
        row = get_file_summary_by_path(abs_path)
        if row and row.summary:
            first = row.summary.split("\n")[0].strip()
            return (first[:160] + "…") if len(first) > 160 else first
    except Exception as exc:
        logger.debug("summary lookup failed for %s: %s", abs_path, exc)
    return None


def list_directory(path: str = ".") -> str:
    """
    List files and directories at the given path.

    Files are shown with their cached summary inline (or SUMMARY NOT AVAILABLE
    on a cache miss).  Special entries:
      - .git: shown as DIR with [PROTECTED]
      - gitignored entries: shown with [PROTECTED — gitignored]
      - symlinks escaping the project root: shown with [PROTECTED — symlink escapes project]
      - .archive and LISTING_EXCLUDED_DIRS: hidden (counted in footer)
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isdir(safe_path):
        return f"ERROR: '{path}' is not a directory."

    _archive_real = os.path.realpath(ARCHIVE_DIR)
    root_real = os.path.realpath(PROJECT_ROOT)
    git_dir = os.path.join(root_real, ".git")

    try:
        from app.agent.project_snapshot import _is_git_ignored, _is_symlink_escaping
    except Exception:
        _is_git_ignored = lambda paths, cwd: set()  # noqa: E731
        _is_symlink_escaping = lambda p, r: False  # noqa: E731

    entries = sorted(os.listdir(safe_path))
    all_full_paths = [os.path.join(safe_path, e) for e in entries]
    ignored_set = _is_git_ignored(all_full_paths, safe_path)

    lines: list[str] = []
    hidden = 0
    for entry, full in zip(entries, all_full_paths):
        full_real = os.path.realpath(full)

        # Always hide: archive dir and excluded dirs (not .git — show it as PROTECTED)
        if full_real == _archive_real:
            hidden += 1
            continue
        if os.path.isdir(full) and entry in LISTING_EXCLUDED_DIRS:
            hidden += 1
            continue

        is_dir = os.path.isdir(full)
        kind = "DIR " if is_dir else "FILE"

        # .git — show as PROTECTED
        if full_real == git_dir:
            lines.append(f"{kind}  {entry}/  [PROTECTED]")
            continue

        # Gitignored
        if full in ignored_set:
            suffix = "/" if is_dir else ""
            lines.append(f"{kind}  {entry}{suffix}  [PROTECTED — gitignored]")
            continue

        # Symlink escaping project
        if _is_symlink_escaping(full, root_real):
            target = os.readlink(full) if os.path.islink(full) else "?"
            lines.append(f"{kind}  {entry} → {target}  [PROTECTED — symlink escapes project]")
            continue

        # Normal directory
        if is_dir:
            lines.append(f"{kind}  {entry}/")
            continue

        # Normal file — show with summary
        summary = _get_cached_summary_for_listing(full)
        if summary is not None:
            lines.append(f"{kind}  {entry}  — {summary}")
        else:
            logger.debug("list_directory: no cached summary for %s", full)
            lines.append(f"{kind}  {entry}  — (SUMMARY NOT AVAILABLE)")

    result = "\n".join(lines) if lines else "(empty directory)"
    if hidden:
        result += f"\n[{hidden} system/excluded director{'ies' if hidden != 1 else 'y'} hidden]"
    return result


def search_files(pattern: str, directory: str = ".") -> str:
    """
    Ripgrep-style content search.
    Returns matches in 'file:line_number: content' format (up to 200 results).
    """
    safe_dir = _assert_safe_path(directory)
    results: list[str] = []
    try:
        compiled = re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        return f"ERROR: invalid regex pattern '{pattern}': {exc}"

    _archive_real = os.path.realpath(ARCHIVE_DIR)
    for root, dirs, files in os.walk(safe_dir):
        # Skip excluded directories (LISTING_EXCLUDED_DIRS covers .git, venv, etc.)
        # Also skip the root archive folder by absolute path so nested .archive
        # subdirectories inside source trees are not affected.
        dirs[:] = [
            d for d in dirs
            if d not in LISTING_EXCLUDED_DIRS
            and os.path.realpath(os.path.join(root, d)) != _archive_real
        ]
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                with open(fpath, "r", encoding="utf-8", errors="ignore") as fh:
                    for lineno, line in enumerate(fh, 1):
                        if compiled.search(line):
                            rel = os.path.relpath(fpath, safe_dir)
                            results.append(f"{rel}:{lineno}: {line.rstrip()}")
                            if len(results) >= TOOL_MAX_SEARCH_RESULTS:
                                results.append(f"... (truncated at {TOOL_MAX_SEARCH_RESULTS} results)")
                                return "\n".join(results)
            except OSError:
                continue

    return "\n".join(results) if results else "No matches found."


def read_file_lines(path: str, start: int, end: int) -> str:
    """Read a specific line range from a file (1-indexed, inclusive on both ends)."""
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    try:
        with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
        total = len(lines)
        # Clamp to actual file bounds
        start = max(1, min(start, total))
        end = max(start, min(end, total))
        result_lines: list[str] = []
        for i in range(start, end + 1):
            result_lines.append(f"{i}: {lines[i - 1].rstrip()}")
        return "\n".join(result_lines)
    except OSError as exc:
        return f"ERROR reading '{path}': {exc}"


def read_file_harder(
    path: str,
    start: int | None = None,
    end: int | None = None,
    count: int | None = None,
) -> str:
    """Read up to 250 source-code lines per call, never repeating lines already in context.

    Omit start/end/count to get the next 250 unserved lines from line 1 forward.
    Provide start (+ optionally end or count) to target a specific range — only the
    unserved portion of that range is returned, capped at 250 lines.

    If read_file() has not been called first, it is called automatically.
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."

    # Auto-prep: show structural summary first if not yet done
    if not _is_file_prepped(safe_path):
        return read_file(path)

    norm = os.path.normpath(os.path.realpath(safe_path))
    from app.agent.project_snapshot import _count_file_lines
    total = _count_file_lines(safe_path)

    # Resolve requested range
    if end is not None and count is not None:
        return "ERROR: provide 'end' OR 'count', not both."
    if start is None:
        req_start, req_end = 1, total
    else:
        req_start = start
        if count is not None:
            req_end = start + count - 1
        elif end is not None:
            req_end = end
        else:
            req_end = start + _READ_FILE_MAX_LINES - 1

    # Clip requested range to unserved lines (max 250)
    unserved = _next_unserved_range(norm, req_start, min(req_end, total))
    if unserved is None:
        rel = os.path.relpath(safe_path, PROJECT_ROOT)
        served = _get_prepped_files().get(norm, [])
        next_hint = f" Next unserved: line {served[-1][1] + 1}." if served and served[-1][1] < total else ""
        return (
            f"ALREADY IN CONTEXT: lines {req_start}–{min(req_end, total)} of '{rel}' "
            f"are already in this session (served: {_served_ranges_str(norm)}).{next_hint}"
        )

    return _serve_file_lines(safe_path, unserved[0], unserved[1])


def count_lines(path: str) -> str:
    """Return the line count and byte size of a file without reading full content into the response."""
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    try:
        byte_size = os.path.getsize(safe_path)
        with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
            line_count = sum(1 for _ in fh)
        return f"{line_count} lines, {byte_size} bytes"
    except OSError as exc:
        return f"ERROR reading '{path}': {exc}"


def find_files(glob_pattern: str, directory: str = ".") -> str:
    """
    Find files matching a glob pattern under directory.
    Returns one path per line (up to 200 results).
    """
    safe_dir = _assert_safe_path(directory)
    full_pattern = os.path.join(safe_dir, "**", glob_pattern)
    matches = _glob.glob(full_pattern, recursive=True)
    _archive_real = os.path.realpath(ARCHIVE_DIR)
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

def archive_file(path: str, reason: str = "") -> str:
    """
    Safely 'delete' a file or directory by moving it into
    .archive/<timestamp>/<original_relative_path>.

    Safety guarantees:
    - NEVER calls shutil.rmtree, os.remove, os.unlink, or any destructive primitive.
    - HARD REJECTS paths inside .git — the repository must never be touched.
    - HARD REJECTS paths already inside ARCHIVE_DIR — cannot re-archive.
    - HARD REJECTS paths outside PROJECT_ROOT — no cross-project accidents.

    Undelete support:
    - If the target path does not exist but was previously archived, returns
      the archived location(s) and exact restore instructions.

    Returns the archive destination path and restore instructions on success.
    """
    try:
        safe_path = _assert_archivable(path)
    except ValueError as exc:
        return f"BLOCKED: {exc}"

    root_real = os.path.realpath(PROJECT_ROOT)
    rel_path = os.path.relpath(safe_path, root_real)

    if not os.path.exists(safe_path):
        # Check if this path was previously archived — emit undelete guide
        archived = _find_archived_copies(rel_path)
        if archived:
            lines = [
                f"ERROR: '{path}' does not exist — it was previously archived.",
                "",
                "Archived copies found (most recent first):",
            ]
            for loc in archived:
                lines.append(f"  {loc}")
            lines += [
                "",
                "To restore the most recent copy run:",
                f'  run_shell(\'python -c "import shutil; '
                f'shutil.copy(r\\"{archived[0]}\\", r\\"{safe_path}\\")"\')',
                "",
                "To restore a directory tree (if a folder was archived) run:",
                f'  run_shell(\'python -c "import shutil; '
                f'shutil.copytree(r\\"{archived[0]}\\", r\\"{safe_path}\\")"\')',
            ]
            return "\n".join(lines)
        return f"ERROR: '{path}' does not exist — nothing to archive."

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest = os.path.join(ARCHIVE_DIR, timestamp, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    try:
        shutil.move(safe_path, dest)
    except OSError as exc:
        logger.error("archive_file: failed to move %s → %s: %s", safe_path, dest, exc)
        return f"ERROR: could not archive '{path}': {exc}"
    logger.info("Archived %s → %s", safe_path, dest)

    if reason:
        reason_file = dest + "._reason.txt"
        with open(reason_file, "w", encoding="utf-8") as fh:
            fh.write(f"Archived at: {timestamp}\nReason: {reason}\n")

    restore_cmd = (
        f'python -c "import shutil; shutil.copy(r\\"{dest}\\", r\\"{safe_path}\\")"'
    )
    return (
        f"OK: archived '{path}' → '{dest}'.\n"
        f"Restore with: run_shell('{restore_cmd}')"
    )


# ---------------------------------------------------------------------------
# Restricted shell
# ---------------------------------------------------------------------------

# INTERNAL ONLY — not in TOOL_SCHEMAS. LLMs cannot call this.
def run_shell(command: str, working_dir: str = ".") -> str:
    """
    Execute a shell command with safety restrictions.
    - Blocks any command matching BLOCKED_PATTERNS.
    - Enforces PROJECT_ROOT containment for the working directory.
    - Hard 30-second timeout.
    Returns stdout + stderr + exit code as a formatted string.
    """
    blocked, reason = _is_command_blocked(command)
    if blocked:
        return f"BLOCKED: {reason}"

    try:
        safe_cwd = _assert_safe_path(working_dir)
    except ValueError as exc:
        return f"BLOCKED: {exc}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=safe_cwd,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
        )
        output_parts = []
        if result.stdout:
            output_parts.append(f"STDOUT:\n{result.stdout}")
        if result.stderr:
            output_parts.append(f"STDERR:\n{result.stderr}")
        output_parts.append(f"EXIT_CODE: {result.returncode}")
        return "\n".join(output_parts) if output_parts else f"EXIT_CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {SHELL_TIMEOUT_SECONDS} seconds."
    except Exception as exc:
        return f"ERROR running shell command: {exc}"


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
    Internal helper — run a git command, return (returncode, stdout, stderr).

    Working directory resolution order:
      1. Explicit ``cwd`` argument (rare — used by internal callers that already
         know the path, e.g. write_file staging).
      2. The per-task context set via ``set_task_git_cwd()``.
      3. Hard error — no fallback to TheMaestro's own repo.

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


def git_status() -> str:
    """Return the current git status of the project."""
    rc, out, err = _git_run(["git", "status"])
    if rc != 0:
        return f"ERROR: git status failed: {err}"
    return out


def git_diff(path: str | None = None) -> str:
    """Return git diff (staged + unstaged). Optionally scoped to a path."""
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
    return out or "(no changes)"


def git_log(path: str | None = None, max_count: int = 20) -> str:
    """
    Return recent git log entries. Optionally scoped to a specific file path.
    Read-only operation — safe for research agents.
    """
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
    return out or "(no log entries)"


def git_blame(path: str) -> str:
    """
    Return git blame output for a file, showing last-modified info per line.
    Read-only operation — safe for research agents.
    """
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


def git_show(ref: str, path: str | None = None) -> str:
    """
    Show a file's content at a specific git commit/ref, or show full commit
    details (message + diffstat) when no path is given.
    Read-only operation — safe for research agents.
    """
    # Validate ref: only allow safe characters
    if not re.match(r'^[A-Za-z0-9\-_./~^@]+$', ref):
        return "ERROR: ref contains invalid characters. Only alphanumeric, -, _, ., /, ~, ^, @ are allowed."
    if path:
        try:
            safe_path = _assert_safe_path(path)
        except ValueError as exc:
            return f"BLOCKED: {exc}"
        # Convert to relative path from the task's working directory for git
        working_dir = _task_git_cwd.get() or PROJECT_ROOT
        rel_path = os.path.relpath(safe_path, working_dir).replace("\\", "/")
        args = ["git", "show", f"{ref}:{rel_path}"]
    else:
        args = ["git", "show", ref, "--stat"]
    rc, out, err = _git_run(args)
    if rc != 0:
        return f"ERROR: git show failed: {err}"
    return out or "(no output)"


def git_create_branch(branch_name: str) -> str:
    """
    Create and checkout a new branch.
    Branch name must start with GIT_SAFETY_BRANCH_PREFIX ('maestro/task-').
    """
    if not branch_name.startswith(GIT_SAFETY_BRANCH_PREFIX):
        return (
            f"ERROR: Branch name must start with '{GIT_SAFETY_BRANCH_PREFIX}'. "
            f"Got '{branch_name}'."
        )
    rc, out, err = _git_run(["git", "checkout", "-b", branch_name])
    if rc != 0:
        return f"ERROR: could not create branch '{branch_name}': {err}"
    return f"OK: created and checked out branch '{branch_name}'."


def git_commit(message: str) -> str:
    """Stage all tracked changes and create a commit with the given message."""
    # Stage tracked modified files
    _git_run(["git", "add", "-u"])
    rc, out, err = _git_run(["git", "commit", "-m", message])
    if rc != 0:
        if "nothing to commit" in err or "nothing to commit" in out:
            return "OK: nothing to commit — working tree clean."
        return f"ERROR: git commit failed: {err}"
    return f"OK: committed.\n{out}"


def git_checkout(branch: str) -> str:
    """
    Checkout a branch. Only maestro/* branches and main/master are allowed.
    """
    allowed = branch.startswith(GIT_SAFETY_BRANCH_PREFIX) or branch in GIT_ALLOWED_BASE_BRANCHES
    if not allowed:
        logger.warning("Blocked git checkout to disallowed branch: %s", branch)
        return (
            f"ERROR: Checkout of '{branch}' is not permitted. "
            f"Only 'maestro/task-*', 'main', and 'master' branches are allowed."
        )
    rc, out, err = _git_run(["git", "checkout", branch])
    if rc != 0:
        return f"ERROR: git checkout '{branch}' failed: {err}"
    return f"OK: checked out '{branch}'."


# ---------------------------------------------------------------------------
# Task / Kanban tools
# ---------------------------------------------------------------------------

def _import_db():
    """Lazy import of database functions to avoid circular import at load time."""
    # The database module lives at app/database.py — add app dir to path if needed
    app_dir = os.path.join(PROJECT_ROOT, "app")
    if app_dir not in sys.path:
        sys.path.insert(0, app_dir)
    import database as _db  # noqa: PLC0415
    return _db


# ---------------------------------------------------------------------------
# Planning tools (pure computation — no I/O, safe for any agent)
# ---------------------------------------------------------------------------

def generate_architecture_doc(title: str, components: list, relationships: list) -> str:
    """
    Produce a structured markdown architecture document and write it to
    .maestro/architecture.md in the project root.  Returns a short stub
    so the full document does not bloat the conversation context.
    """
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


def generate_mermaid_diagram(diagram_type: str, definition: str) -> str:
    """
    Validate and format a Mermaid diagram definition, then write it to
    .maestro/diagrams/{diagram_type}.md in the project root.  Returns a
    short stub so the diagram body does not bloat the conversation context.
    """
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


def generate_interface_contract(component_name: str, provides: list, consumes: list) -> str:
    """
    Define the API surface / interface contract for a component and write it
    to .maestro/contracts/{component_name}.json in the project root.  Returns
    a short stub so the contract body does not bloat the conversation context.
    """
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


def record_benchmark(task_id: str, parent_task_id: str, benchmark_type: str, metrics: str) -> str:
    """
    Record a before/after profiling benchmark for an optimization sub-task.
    benchmark_type must be 'before' or 'after'.
    metrics must be a JSON string: {test_duration_ms, memory_peak_mb, complexity_score, ...}
    """
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
    import json as _json
    from app.database import get_search_cache, create_search_cache
    from app.agent.config import BRAVE_API_KEY, SEARCH_PROVIDER

    # 1. Check local cache first
    q = query.strip()
    cached = get_search_cache(q)
    if cached:
        logger.info("Search Cache HIT for query: '%s'", q)
        return cached.result_json

    # 2. Cache miss — call the selected search provider
    provider = SEARCH_PROVIDER.lower()
    search_results = []

    try:
        if provider == "duckduckgo":
            logger.info("Search Cache MISS for query: '%s' — calling DuckDuckGo", q)
            search_results = _ddg_search(q, count)
        elif provider == "brave":
            if not BRAVE_API_KEY:
                return "ERROR: BRAVE_API_KEY not set but search_provider='brave'. Web search is unavailable."
            logger.info("Search Cache MISS for query: '%s' — calling Brave Search API", q)
            search_results = _brave_search(q, count, BRAVE_API_KEY)
        else:
            return f"ERROR: Unknown search_provider '{provider}'. Supported: duckduckgo, brave."

        final_json = _json.dumps({"query": q, "provider": provider, "results": search_results}, indent=2)

        # 3. Persist to cache for next time
        create_search_cache(q, final_json)

        return final_json
    except ImportError as e:
        lib = "duckduckgo-search" if provider == "duckduckgo" else "brave"
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
    results = brave.search(q=query, count=min(count, 10))

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
    Placeholder for synchronous dispatch — the actual async version is in
    async_dispatch_tool(). When called synchronously, returns an error
    directing the caller to use the async path.
    """
    return (
        "ERROR: spawn_research_agent requires async dispatch. "
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
        return json.dumps({
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
        }, indent=2)
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


def update_task_status(task_id: str, new_status: str) -> str:
    """
    Advance a task through the Kanban pipeline.
    Valid transitions: PENDING→ACTIVE→VERIFYING→ACCEPTED / REJECTED.
    Maps agent status names to Kanban column types.
    """
    STATUS_TO_TYPE = {
        "PENDING": "planning",
        "ACTIVE": "development",
        "VERIFYING": "review",
        "ACCEPTED": "completed",
        "REJECTED": "planning",
    }
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


def append_task_history(task_id: str, entry: str) -> str:
    """
    Append a proof-of-work entry to a task's history log.
    entry should be a human-readable string describing what was done.
    """
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


# ---------------------------------------------------------------------------
# Allowlisted shell wrappers (delegate to pipeline modules)
# ---------------------------------------------------------------------------

def _run_shell_security_wrapper(command: str) -> str:
    """Allowlisted shell for security scanning tools."""
    from app.agent.security_review import run_shell_security
    return run_shell_security(command)


def _run_shell_review_wrapper(command: str) -> str:
    """Allowlisted shell for review/test runner tools."""
    from app.agent.full_review import run_shell_review
    return run_shell_review(command)


# ---------------------------------------------------------------------------
# In-dev allowlisted shell (test/lint runners only)
# ---------------------------------------------------------------------------

_INDEV_ALLOWLIST_PATTERNS: list[str] = [
    r"^python\s+-m\s+pytest\b",
    r"^python\s+-m\s+mypy\b",
    r"^python\s+-m\s+ruff\b",
    r"^python\s+-m\s+black\s+--check\b",
    r"^npm\s+test\b",
    r"^npm\s+run\s+(?:test|build|lint|typecheck)\b",
    r"^cargo\s+test\b",
    r"^go\s+test\b",
    r"^make\s+(?:test|build|lint)\b",
]
_INDEV_ALLOWLIST_RE = [re.compile(p) for p in _INDEV_ALLOWLIST_PATTERNS]


def run_shell_indev(command: str) -> str:
    """Execute an allowlisted development command in the task's project directory.

    Only commands matching _INDEV_ALLOWLIST_PATTERNS are permitted.
    cwd is resolved from the per-task context (_task_git_cwd).
    """
    cwd = _task_git_cwd.get()
    if cwd is None:
        return (
            "ERROR: No task git working directory configured. "
            "Call set_task_git_cwd(project_path) before using run_shell_indev."
        )

    command = command.strip()
    allowed = any(pat.match(command) for pat in _INDEV_ALLOWLIST_RE)
    if not allowed:
        return f"Command not in allowlist: {command}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
        return output[:8000] if output else f"EXIT_CODE: {result.returncode}"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {SHELL_TIMEOUT_SECONDS} seconds."
    except Exception as exc:
        return f"ERROR running command: {exc}"


# ---------------------------------------------------------------------------
# Tool registry + schemas
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "read_file": read_file,
    "read_file_harder": read_file_harder,
    "read_file_lines": read_file_lines,
    "count_lines": count_lines,
    "write_file": write_file,
    "append_file": append_file,
    "list_directory": list_directory,
    "search_files": search_files,
    "find_files": find_files,
    "archive_file": archive_file,
    "run_shell": run_shell,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_blame": git_blame,
    "git_show": git_show,
    "git_create_branch": git_create_branch,
    "git_commit": git_commit,
    "git_checkout": git_checkout,
    "get_task": get_task,
    "list_tasks": list_tasks,
    "update_task_status": update_task_status,
    "append_task_history": append_task_history,
    "web_search": web_search,
    "web_fetch": web_fetch,
    "generate_architecture_doc": generate_architecture_doc,
    "generate_mermaid_diagram": generate_mermaid_diagram,
    "generate_interface_contract": generate_interface_contract,
    "spawn_research_agent": spawn_research_agent,
    "record_benchmark": record_benchmark,
    "run_shell_security": _run_shell_security_wrapper,
    "run_shell_review": _run_shell_review_wrapper,
    "run_shell_indev": run_shell_indev,
}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "First call: returns a natural-language summary (LLM-generated, cached) plus "
                "structural analysis: classes, functions, imports, and line ranges. "
                "For tiny files (≤ 25 lines) embeds raw content directly. "
                "Each subsequent call on the same file serves the next 250 unserved source lines — "
                "never repeating lines already in context. "
                "Call repeatedly to page through a file, or use read_file_harder() for a specific range."
            ),
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
            "name": "read_file_harder",
            "description": (
                "Read up to 250 source-code lines per call, never repeating lines already in context. "
                "Omit start/end/count to get the next 250 unserved lines from line 1 forward. "
                "Provide start (+ optionally end or count) to target a specific range — only the "
                "unserved portion of that range is returned. "
                "If read_file() has not been called first, it is called automatically."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)."},
                    "start": {"type": "integer", "description": "Starting line number (1-indexed). Omit to continue from last served line."},
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
            "name": "read_file_lines",
            "description": "Read a specific line range from a file (1-indexed, inclusive). Lines are returned prefixed with line numbers.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path to the file (relative to project root or absolute)."},
                    "start": {"type": "integer", "description": "First line number to read (1-indexed). Clamped to file bounds."},
                    "end": {"type": "integer", "description": "Last line number to read (1-indexed, inclusive). Clamped to file bounds."},
                },
                "required": ["path", "start", "end"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_lines",
            "description": "Return the line count and byte size of a file without reading the full content.",
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
            "name": "write_file",
            "description": "Write (overwrite) a file with the given content. The file is automatically staged for git.",
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
            "description": "Append text to the end of a file (creates the file if it does not exist).",
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
            "name": "list_directory",
            "description": "List files and subdirectories at a given path.",
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
            "name": "search_files",
            "description": "Search file contents using a regex pattern. Returns file:line matches (up to 200).",
            "parameters": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string", "description": "Regex pattern to search for."},
                    "directory": {"type": "string", "description": "Directory to search in.", "default": "."},
                },
                "required": ["pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_files",
            "description": "Find files by glob pattern (e.g. '*.py', 'test_*.py').",
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
    {
        "type": "function",
        "function": {
            "name": "archive_file",
            "description": (
                "Safely 'delete' a file by moving it to .archive/<timestamp>/. "
                "NEVER performs a hard delete. Use this instead of any rm/del command."
            ),
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
            "name": "git_status",
            "description": "Return the current git status of the project.",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "git_diff",
            "description": "Return git diff (staged + unstaged). Optionally scoped to a path.",
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
            "name": "git_log",
            "description": "Return recent git log entries. Optionally scoped to a specific file. Read-only.",
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
            "name": "git_blame",
            "description": "Show git blame for a file (last-modified info per line). Read-only.",
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
            "name": "git_show",
            "description": "Show a file's content at a specific git ref, or show commit details (message + diffstat). Read-only.",
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
            "name": "git_create_branch",
            "description": f"Create and checkout a new branch. Must be prefixed with '{GIT_SAFETY_BRANCH_PREFIX}'.",
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
            "name": "git_commit",
            "description": "Stage all tracked changes and create a git commit.",
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
            "name": "git_checkout",
            "description": "Checkout a branch. Only maestro/* and main/master branches are permitted.",
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
            "description": "Fetch a Kanban task by ID. Returns a JSON object with all task fields.",
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
            "description": "List task summaries for a project, optionally filtered by column. Read-only.",
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
            "name": "update_task_status",
            "description": (
                "Advance a task through the Kanban pipeline. "
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
            "name": "append_task_history",
            "description": "Append a proof-of-work entry to a task's history log.",
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
            "name": "generate_architecture_doc",
            "description": (
                "Produce a structured markdown architecture document from components and relationships. "
                "Pure computation — stays in agent context, NOT written to disk."
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
            "name": "generate_mermaid_diagram",
            "description": (
                "Validate and format a Mermaid diagram. Returns formatted mermaid markup. "
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
            "name": "generate_interface_contract",
            "description": (
                "Define the API surface / interface contract between components. "
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
                "Launch a research agent to investigate a domain question. "
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
            "name": "record_benchmark",
            "description": (
                "Record a before/after profiling benchmark for an optimization sub-task. "
                "Call with benchmark_type='before' before making changes, and 'after' when done. "
                "Run actual timed benchmarks using run_shell before recording — do NOT estimate. "
                "metrics must be a JSON string with the following keys: "
                "test_duration_ms (float, required) — measured wall time in ms for scale_n items; "
                "memory_peak_mb (float, required) — peak RSS during benchmark in MB; "
                "complexity_score (int, required) — subjective 0-100 code complexity estimate; "
                "big_o_class (str) — Big O of the critical path: O(1), O(log n), O(n), O(n log n), O(n^2), O(n^3), O(2^n), O(n!); "
                "scale_n (int) — N used in the synthetic benchmark run; "
                "readability_cost (float) — 0.0 (no cost) to 1.0 (very hard to understand); "
                "is_premature (bool) — true if optimizing a non-bottleneck; "
                "tech_debt_resolved (bool) — true if this consolidates or resolves known tech debt; "
                "notes (str) — qualitative notes (optional)."
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
    {
        "type": "function",
        "function": {
            "name": "run_shell_security",
            "description": (
                "Execute a shell command from the security scanner allowlist. "
                "Only permits: bandit, safety, pip-audit, semgrep, trivy, grype, syft, trufflehog. "
                "All other commands are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run (must match allowlist)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_review",
            "description": (
                "Execute a shell command from the review runner allowlist. "
                "Only permits: pytest, ruff, mypy, black --check, npm test, npm run lint. "
                "All other commands are blocked."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The shell command to run (must match allowlist)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_shell_indev",
            "description": (
                "Execute an allowlisted development command in the task's project directory. "
                "Permitted: python -m pytest, python -m mypy, python -m ruff, "
                "python -m black --check, npm test, npm run (test|build|lint|typecheck), "
                "cargo test, go test, make (test|build|lint). "
                "All other commands are blocked. cwd is the task's project directory."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "The command to run (must match allowlist)."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "Execute a web search using the Brave Search API. "
                "Returns a JSON string of results with titles, URLs, and snippets. "
                "Requires BRAVE_API_KEY environment variable. Use this when you need "
                "up-to-date information or to research unknown technologies/APIs."
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
                "Fetch the content of a URL and return a text-only summary. "
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
        return result
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
        safe_path = _assert_safe_path(path)
        if not os.path.isfile(safe_path):
            return f"ERROR: '{path}' is not a file or does not exist."
        norm_path = os.path.normpath(os.path.realpath(safe_path))
        from app.agent.project_snapshot import _count_file_lines, async_build_file_summary
        # Subsequent call — serve the next unserved 250-line chunk
        if norm_path in _get_prepped_files():
            total = _count_file_lines(safe_path)
            unserved = _next_unserved_range(norm_path, 1, total)
            if unserved is None:
                rel = os.path.relpath(safe_path, PROJECT_ROOT)
                return (
                    f"ALREADY IN CONTEXT: all lines of '{rel}' have been served "
                    f"(ranges: {_served_ranges_str(norm_path)}). "
                    f"Call read_file_harder('{rel}', start=N) for a specific range."
                )
            return _serve_file_lines(safe_path, unserved[0], unserved[1])
        # First call — LLM-enriched structural summary
        _mark_file_prepped(safe_path)
        if _count_file_lines(safe_path) <= 25:
            result = _inline_small_file(safe_path)
            try:
                with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
                    lc = sum(1 for _ in fh)
                _record_served_range(norm_path, 1, lc)
            except OSError:
                pass
            return result
        return await async_build_file_summary(
            safe_path,
            summary_length="brief",
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
        )

    if name in ("write_file", "append_file"):
        # Capture old summary BEFORE the write (file still has old content)
        old_summary: str | None = None
        try:
            p = _assert_safe_path(arguments.get("path", ""))
            if os.path.isfile(p) and llm_id is not None:
                from app.database import get_file_summary_by_path
                row = get_file_summary_by_path(p)
                if row:
                    old_summary = row.summary
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

    # All other tools — synchronous
    return dispatch_tool(name, arguments)
