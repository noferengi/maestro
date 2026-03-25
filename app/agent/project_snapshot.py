"""
app/agent/project_snapshot.py
------------------------------
Project structure snapshot and file summary builders.

Eliminates agent orientation turns by injecting a pre-built project tree
into initial context, and provides tiered file reading via structural
summaries before raw source access.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

from app.agent.config import (
    PROJECT_ROOT,
    TOOL_LISTING_EXCLUDED_DIRS,
)

# Lazy imports for snapshot config — avoids circular import issues
def _snapshot_max_depth() -> int:
    from app.agent.config import SNAPSHOT_MAX_DEPTH
    return SNAPSHOT_MAX_DEPTH

def _snapshot_max_tokens() -> int:
    from app.agent.config import SNAPSHOT_MAX_TOKENS
    return SNAPSHOT_MAX_TOKENS

def _snapshot_cache_ttl() -> int:
    from app.agent.config import SNAPSHOT_CACHE_TTL
    return SNAPSHOT_CACHE_TTL


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------

# (project_root) -> (timestamp, snapshot_str)
_snapshot_cache: dict[str, tuple[float, str]] = {}

# (abs_path, mtime, size) -> summary_str
_file_summary_cache: dict[tuple[str, float, int], str] = {}


def clear_snapshot_cache() -> None:
    """Clear all cached snapshots and file summaries."""
    _snapshot_cache.clear()
    _file_summary_cache.clear()


# ---------------------------------------------------------------------------
# Project snapshot
# ---------------------------------------------------------------------------

def _count_file_lines(path: str) -> int:
    """Count lines in a file without reading entire content into memory."""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return sum(1 for _ in fh)
    except OSError:
        return 0


def _analyze_py_file(path: str) -> tuple[int, int, int]:
    """Return (line_count, class_count, function_count) for a Python file.

    Uses tree-sitter static analysis when available, falls back to line count only.
    """
    line_count = _count_file_lines(path)
    try:
        from app.agent.static_analysis import analyze_file
        analysis = analyze_file(path)
        return line_count, len(analysis.classes), len(analysis.functions)
    except (ImportError, Exception) as exc:
        logger.debug("static_analysis unavailable for %s: %s", path, exc)
        return line_count, -1, -1


def _format_size(size_bytes: int) -> str:
    """Format byte size in human-readable form (1024-based)."""
    if size_bytes < 1024:
        return f"{size_bytes}B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f}KB"
    else:
        return f"{size_bytes / (1024 * 1024):.1f}MB"


def build_project_snapshot(project_root: str | None = None, max_depth: int | None = None) -> str:
    """Build an indented directory tree with file annotations.

    For .py files: shows line count, class count, function count.
    For other files: shows file size.

    Respects TOOL_LISTING_EXCLUDED_DIRS. Truncates if estimated
    token count exceeds the configured budget.
    """
    if project_root is None:
        project_root = PROJECT_ROOT
    if max_depth is None:
        max_depth = _snapshot_max_depth()

    project_root = os.path.normpath(os.path.abspath(project_root))

    # Check cache
    cache_ttl = _snapshot_cache_ttl()
    cached = _snapshot_cache.get(project_root)
    if cached is not None:
        ts, snapshot = cached
        if time.time() - ts < cache_ttl:
            return snapshot

    excluded = TOOL_LISTING_EXCLUDED_DIRS
    lines: list[str] = ["== PROJECT STRUCTURE =="]
    max_tokens = _snapshot_max_tokens()

    def _walk(dir_path: str, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return

        dirs: list[str] = []
        files: list[str] = []
        for entry in entries:
            if entry.startswith(".") and entry not in (".env.example",):
                continue
            full = os.path.join(dir_path, entry)
            if os.path.isdir(full):
                if entry not in excluded:
                    dirs.append(entry)
            elif os.path.isfile(full):
                files.append(entry)

        for fname in files:
            full = os.path.join(dir_path, fname)
            if fname.endswith(".py"):
                lc, cc, fc = _analyze_py_file(full)
                if cc >= 0:
                    lines.append(f"{prefix}{fname} — {lc} lines, {cc} classes, {fc} functions")
                else:
                    lines.append(f"{prefix}{fname} — {lc} lines")
            else:
                try:
                    sz = os.path.getsize(full)
                    lines.append(f"{prefix}{fname} — {_format_size(sz)}")
                except OSError:
                    lines.append(f"{prefix}{fname}")

        for dname in dirs:
            lines.append(f"{prefix}{dname}/")
            _walk(os.path.join(dir_path, dname), prefix + "  ", depth + 1)

    _walk(project_root, "  ", 0)

    # Token budget enforcement — estimate tokens as len/4
    result = "\n".join(lines)
    estimated_tokens = len(result) // 4
    if estimated_tokens > max_tokens:
        # Truncate: keep the header + app/ subtree lines preferentially
        truncated: list[str] = [lines[0]]
        budget_chars = max_tokens * 4
        used = len(truncated[0])
        for line in lines[1:]:
            if used + len(line) + 1 > budget_chars:
                truncated.append("  ... (truncated — project has more files)")
                break
            truncated.append(line)
            used += len(line) + 1
        result = "\n".join(truncated)

    _snapshot_cache[project_root] = (time.time(), result)
    return result


# ---------------------------------------------------------------------------
# File summary
# ---------------------------------------------------------------------------

def build_file_summary(path: str, summary_length: str = "none") -> str:
    """Build a structural summary of a file (sync, no LLM call).

    For Python files: shows classes with methods + line ranges, functions
    with params + line ranges, imports, globals.
    For other files: shows line count and byte size.

    The summary_length parameter is accepted but ignored in sync mode
    (LLM summarization requires async_build_file_summary).
    """
    abs_path = os.path.normpath(os.path.abspath(path))

    if not os.path.isfile(abs_path):
        return f"ERROR: '{path}' is not a file or does not exist."

    try:
        stat = os.stat(abs_path)
        mtime = stat.st_mtime
        size = stat.st_size
    except OSError as exc:
        return f"ERROR: cannot stat '{path}': {exc}"

    # Check cache
    cache_key = (abs_path, mtime, size)
    cached = _file_summary_cache.get(cache_key)
    if cached is not None:
        return cached

    line_count = _count_file_lines(abs_path)
    size_str = _format_size(size)
    parts: list[str] = [f"== FILE: {path} ({line_count} lines, {size_str}) =="]

    if abs_path.endswith(".py"):
        try:
            from app.agent.static_analysis import analyze_file
            analysis = analyze_file(abs_path)

            # Classes
            parts.append(f"\n## Classes ({len(analysis.classes)})")
            if not analysis.classes:
                parts.append("  (none)")
            else:
                for cls in analysis.classes:
                    bases_str = f"({', '.join(cls.bases)})" if cls.bases else ""
                    parts.append(f"  {cls.name}{bases_str} — lines {cls.line_start}-{cls.line_end}")
                    for method in cls.methods:
                        parts.append(f"    .{method}()")

            # Functions
            parts.append(f"\n## Functions ({len(analysis.functions)})")
            if not analysis.functions:
                parts.append("  (none)")
            else:
                for func in analysis.functions:
                    async_prefix = "async " if func.is_async else ""
                    params_str = ", ".join(func.params)
                    parts.append(f"  {async_prefix}{func.name}({params_str}) — lines {func.line_start}-{func.line_end}")

            # Imports
            if analysis.imports:
                parts.append(f"\n## Imports")
                parts.append(f"  {', '.join(analysis.imports)}")

            # Globals
            if analysis.global_variables:
                parts.append(f"\n## Globals")
                parts.append(f"  {', '.join(analysis.global_variables)}")

        except (ImportError, Exception) as exc:
            logger.debug("tree-sitter unavailable for summary of %s: %s", path, exc)
            parts.append("\n(tree-sitter analysis unavailable — showing metadata only)")

    parts.append(f"\n--> Use read_file_harder(path, start=N, end=N) to read source code.")

    result = "\n".join(parts)
    _file_summary_cache[cache_key] = result
    return result


async def async_build_file_summary(
    path: str,
    summary_length: str = "none",
    *,
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> str:
    """Async version of build_file_summary.

    Always prepends an LLM-generated natural-language summary (DB-cached by
    SHA1 + filesize via FileSummaryAgent) unless summary_length is "none".
    Falls back to structural-only on any error.
    """
    # Always start with the structural summary
    structural = build_file_summary(path, summary_length="none")

    if summary_length == "none":
        return structural

    abs_path = os.path.normpath(os.path.abspath(path))

    # Session-level in-memory fast path (avoids repeated DB queries within one run).
    # Uses a "llm:" prefix on the key so these entries are distinct from the
    # structural-only entries that build_file_summary writes under the same path.
    try:
        stat = os.stat(abs_path)
        session_key = ("llm", abs_path, stat.st_mtime, stat.st_size)
        cached = _file_summary_cache.get(session_key)
        if cached is not None:
            return cached
    except OSError:
        pass

    try:
        import asyncio as _asyncio
        from app.agent.file_summary_agent import enqueue_file_summary
        from app.database import get_file_summary

        completion_key, sha1, filesize = enqueue_file_summary(
            abs_path,
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
        )

        if completion_key:
            # Cache miss — wait for scheduler to dispatch and complete the job
            from app.agent.scheduler import wait_for_completion
            loop = _asyncio.get_event_loop()
            completed = await loop.run_in_executor(
                None, wait_for_completion, completion_key, 120.0
            )
            if not completed:
                logger.warning("file_summary timed out for %s — using structural only", path)
                return structural

        cached = get_file_summary(sha1, filesize)
        if cached:
            result = f"## Summary\n{cached.summary}\n\n{structural}"
            try:
                _file_summary_cache[session_key] = result
            except UnboundLocalError:
                pass
            return result

    except Exception as exc:
        logger.warning("LLM summarization failed for %s: %s", path, exc)

    return structural


# ---------------------------------------------------------------------------
# Gitignore + symlink safety helpers
# ---------------------------------------------------------------------------

def _is_git_ignored(paths: list[str], cwd: str) -> set[str]:
    """Batch-check which of the given absolute paths are gitignored.

    Returns a set of absolute paths that git considers ignored.
    Returns an empty set on any failure (git unavailable, not a repo, etc.).
    """
    if not paths:
        return set()
    try:
        rel_paths = [os.path.relpath(p, cwd) for p in paths]
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(rel_paths) + "\0",
            capture_output=True, text=True, cwd=cwd, timeout=10,
        )
        if result.returncode not in (0, 1):
            return set()
        ignored_rels = {r for r in result.stdout.split("\0") if r}
        return {p for p, r in zip(paths, rel_paths) if r in ignored_rels}
    except Exception as exc:
        logger.debug("_is_git_ignored failed: %s", exc)
        return set()


def _is_symlink_escaping(abs_path: str, project_root: str) -> bool:
    """Return True if abs_path is a symlink whose real target is outside project_root."""
    if not os.path.islink(abs_path):
        return False
    real = os.path.realpath(abs_path)
    root_real = os.path.realpath(project_root)
    return not (real.startswith(root_real + os.sep) or real == root_real)


# ---------------------------------------------------------------------------
# Pre-warm
# ---------------------------------------------------------------------------

def prewarm_project_summaries(
    project_root: str,
    *,
    llm_id: "int | None",
    budget_id: "int | None",
    task_id: "str | None" = None,
) -> int:
    """Enqueue summary jobs for every non-binary file in the project tree.

    Synchronous.  Intended to be called via run_in_executor from async context.
    Returns the count of new jobs enqueued (cache hits do not count).
    """
    from app.agent.file_summary_agent import enqueue_file_summary

    project_root = os.path.normpath(os.path.abspath(project_root))
    root_real = os.path.realpath(project_root)
    _archive_real = os.path.realpath(os.path.join(root_real, ".archive"))
    git_dir = os.path.join(root_real, ".git")

    enqueued = 0
    for dirpath, dirnames, filenames in os.walk(project_root):
        # Hard exclusions: known noise dirs + .git + .archive
        dirnames[:] = [
            d for d in dirnames
            if d not in TOOL_LISTING_EXCLUDED_DIRS
            and os.path.realpath(os.path.join(dirpath, d)) not in (_archive_real, git_dir)
        ]

        # Batch gitignore check for both dirs and files in this level.
        abs_dirs  = [os.path.join(dirpath, d) for d in dirnames]
        abs_files = [os.path.join(dirpath, f) for f in filenames]
        ignored   = _is_git_ignored(abs_dirs + abs_files, root_real)

        # Prune ignored dirs so os.walk never descends into them.
        dirnames[:] = [d for d, p in zip(dirnames, abs_dirs) if p not in ignored]

        for fname in filenames:
            abs_path = os.path.join(dirpath, fname)
            if abs_path in ignored:
                continue
            if _is_symlink_escaping(abs_path, root_real):
                continue
            try:
                with open(abs_path, "rb") as fh:
                    if b"\x00" in fh.read(8192):
                        continue
            except OSError:
                continue
            try:
                key, _, _ = enqueue_file_summary(
                    abs_path, task_id=task_id, llm_id=llm_id,
                    budget_id=budget_id, priority=-1.0,
                )
                if key:
                    enqueued += 1
            except Exception as exc:
                logger.debug("prewarm skip %s: %s", abs_path, exc)

    logger.info("prewarm: %d new jobs enqueued for %s", enqueued, project_root)
    return enqueued


# ---------------------------------------------------------------------------
# Snapshot with inline summaries
# ---------------------------------------------------------------------------

def build_snapshot_with_summaries(
    project_root: str | None = None,
    max_depth: int | None = None,
) -> str:
    """Like build_project_snapshot() but appends a cached summary to each file line.

    Uses the same cache key as build_project_snapshot so they share TTL/invalidation.
    Summary text is the first sentence from the DB cache (get_file_summary_by_path).
    Cache miss → file line emitted without summary (no placeholder noise for the LLM).
    """
    if project_root is None:
        project_root = PROJECT_ROOT
    if max_depth is None:
        max_depth = _snapshot_max_depth()

    project_root = os.path.normpath(os.path.abspath(project_root))

    cache_ttl = _snapshot_cache_ttl()
    cached = _snapshot_cache.get(project_root)
    if cached is not None:
        ts, snapshot = cached
        if time.time() - ts < cache_ttl:
            return snapshot

    excluded = TOOL_LISTING_EXCLUDED_DIRS
    lines: list[str] = ["== PROJECT STRUCTURE =="]
    max_tokens = _snapshot_max_tokens()

    try:
        from app.database import get_file_summary_by_path as _get_summary
    except Exception:
        _get_summary = None

    def _inline_summary(abs_path: str) -> str:
        if _get_summary is None:
            return ""
        try:
            row = _get_summary(abs_path)
            if row and row.summary:
                first = row.summary.split("\n")[0].strip()
                return f" — {first}"
        except Exception:
            pass
        return ""

    def _walk(dir_path: str, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return

        dirs: list[str] = []
        files: list[str] = []
        for entry in entries:
            if entry.startswith(".") and entry not in (".env.example",):
                continue
            full = os.path.join(dir_path, entry)
            if os.path.isdir(full):
                if entry not in excluded:
                    dirs.append(entry)
            elif os.path.isfile(full):
                files.append(entry)

        for fname in files:
            full = os.path.join(dir_path, fname)
            summary_suffix = _inline_summary(full)
            if fname.endswith(".py"):
                lc, cc, fc = _analyze_py_file(full)
                if cc >= 0:
                    lines.append(f"{prefix}{fname} — {lc} lines, {cc} classes, {fc} functions{summary_suffix}")
                else:
                    lines.append(f"{prefix}{fname} — {lc} lines{summary_suffix}")
            else:
                try:
                    sz = os.path.getsize(full)
                    lines.append(f"{prefix}{fname} — {_format_size(sz)}{summary_suffix}")
                except OSError:
                    lines.append(f"{prefix}{fname}{summary_suffix}")

        for dname in dirs:
            lines.append(f"{prefix}{dname}/")
            _walk(os.path.join(dir_path, dname), prefix + "  ", depth + 1)

    _walk(project_root, "  ", 0)

    result = "\n".join(lines)
    estimated_tokens = len(result) // 4
    if estimated_tokens > max_tokens:
        truncated: list[str] = [lines[0]]
        budget_chars = max_tokens * 4
        used = len(truncated[0])
        for line in lines[1:]:
            if used + len(line) + 1 > budget_chars:
                truncated.append("  ... (truncated — project has more files)")
                break
            truncated.append(line)
            used += len(line) + 1
        result = "\n".join(truncated)

    _snapshot_cache[project_root] = (time.time(), result)
    return result
