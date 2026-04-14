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

# Lazy imports for snapshot config - avoids circular import issues
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

# ("basic"|"summaries", project_root, effective_max_tokens) -> (timestamp, snapshot_str)
# The max_tokens dimension lets agents with different context windows cache independently.
_snapshot_cache: dict[tuple[str, str, int], tuple[float, str]] = {}

# BUG: TODO: we use this _file_summary_cache inconsistently, sometimes with 3 params, sometimes 4!

# (abs_path, mtime, size) OR ("llm", abs_path, mtime, size) -> summary_str
# Structural summaries use a 3-tuple; LLM-enriched session summaries use a "llm" prefix.
# _file_summary_cache: dict[tuple[Any, ...], str] = {}
# DO NOT DO IT THIS WAY, FIX THE INCONSISTENCIES! /TODO

# (abs_path, mtime, size) -> summary_str
_file_summary_cache: dict[tuple[str, float, int], str] = {}


def clear_snapshot_cache() -> None:
    """Clear all cached snapshots and file summaries."""
    _snapshot_cache.clear()
    _file_summary_cache.clear()


# ---------------------------------------------------------------------------
# Architecture context - project-wide constraints injected into agent prompts
# ---------------------------------------------------------------------------

# Categories shown on architecture cards (must match the frontend select options).
# Agents use this as documentation; the actual filtering is set-membership below.
ARCH_CATEGORIES = [
    'Platform', 'Design', 'Testing', 'Security', 'Performance', 'API',
    'Tooling', 'Data', 'UX', 'Accessibility', 'Compliance', 'Deployment',
    'Observability', 'General',
]

# Per-agent category relevance filter.
# None  → include all categories (no filtering).
# set   → include only cards whose category is in the set.
#
# Rationale:
#   file_summary   - only needs to know Platform/Tooling/Data/General to contextualise
#                    what kind of codebase/files it is summarising.
#   subdivision    - needs Design/Testing/Performance/API/Data/Platform/Tooling/General
#                    to decompose tasks that stay within architectural constraints.
#   conceptual_review - audits design decisions, so Design/API/Data/Security/
#                    Accessibility/Compliance/General matter most.
#   security       - focused audit: Security/Compliance/API/Data/Platform/General.
#   optimization   - focused audit: Performance/Platform/Data/Observability/Tooling/General.
#   research/loop/intake/full_review → None (all categories, full context needed).
ARCH_CATEGORY_RELEVANCE: dict[str, set[str] | None] = {
    'file_summary':      {'Platform', 'Tooling', 'Data', 'General'},
    'subdivision':       {'Platform', 'Design', 'Testing', 'Performance',
                          'API', 'Data', 'Tooling', 'General'},
    'conceptual_review': {'Design', 'API', 'Data', 'Security',
                          'Accessibility', 'Compliance', 'General'},
    'security':          {'Security', 'Compliance', 'API', 'Data', 'Platform', 'General'},
    'optimization':      {'Performance', 'Platform', 'Data', 'Observability',
                          'Tooling', 'General'},
    'planning':         None,  # all categories — needs full platform/design context
    # research, loop, intake, full_review → all categories (key absent = None)
}

# Priority sort order for injection (critical first)
_ARCH_PRIORITY_ORDER = {'critical': 0, 'high': 1, 'normal': 2, 'low': 3}


def build_architecture_context(
    project_name: str,
    agent_type: str | None = None,
) -> str:
    """Return a formatted block of architecture/constraint cards for agent context.

    project_name - the Maestro project name (Task.project field).
    agent_type   - when set, filters to categories relevant for that agent type
                   using ARCH_CATEGORY_RELEVANCE.  None means include all cards.

    Returns an empty string when no cards exist or are relevant.
    project_name must be non-empty.
    """
    if not project_name:
        return ""

    try:
        from app.database import get_tasks_by_project
        all_tasks = get_tasks_by_project(project_name)
    except Exception as exc:
        logger.debug("build_architecture_context: DB fetch failed for '%s': %s", project_name, exc)
        return ""

    arch_tasks = [t for t in all_tasks if getattr(t, 'type', '') == 'architecture']
    if not arch_tasks:
        return ""

    # Resolve category filter for this agent type
    category_filter: set[str] | None = ARCH_CATEGORY_RELEVANCE.get(agent_type or '', None) \
        if agent_type else None

    def _category(t) -> str:
        c = getattr(t, 'content', None) or {}
        return (c.get('category', 'General') if isinstance(c, dict) else 'General') or 'General'

    def _priority(t) -> str:
        c = getattr(t, 'content', None) or {}
        return (c.get('priority', 'normal') if isinstance(c, dict) else 'normal') or 'normal'

    # Apply category filter
    if category_filter is not None:
        arch_tasks = [t for t in arch_tasks if _category(t) in category_filter]

    if not arch_tasks:
        return ""

    # Sort: critical → high → normal → low, then by position
    arch_tasks.sort(key=lambda t: (
        _ARCH_PRIORITY_ORDER.get(_priority(t), 2),
        getattr(t, 'position', 0) or 0,
    ))

    _PRIO_LABELS = {
        'critical': ' [CRITICAL - hard constraint]',
        'high':     ' [HIGH - strong preference]',
        'normal':   '',
        'low':      ' [low priority - soft suggestion]',
    }

    lines: list[str] = ["== PROJECT ARCHITECTURE & CONSTRAINTS =="]
    if category_filter is not None and agent_type:
        relevant = ', '.join(sorted(category_filter))
        lines.append(
            f"(Showing only categories relevant to {agent_type} work: {relevant})"
        )
    lines.append(
        "These constraints apply to ALL work in this project. "
        "Treat CRITICAL items as hard requirements."
    )

    for task in arch_tasks:
        cat   = _category(task)
        prio  = _priority(task)
        label = _PRIO_LABELS.get(prio, '')
        desc  = (getattr(task, 'description', '') or '').strip()
        title = getattr(task, 'title', '') or ''
        lines.append(f"\n### {title} [{cat}]{label}")
        if desc:
            lines.append(desc)

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Project snapshot
# ---------------------------------------------------------------------------

def _is_binary_file(path: str) -> bool:
    """Return True if the file appears to be binary (null bytes in first 512 bytes)."""
    try:
        with open(path, "rb") as fh:
            return b"\x00" in fh.read(512)
    except OSError:
        return False


def _count_file_lines(path: str) -> int:
    """Count lines in a file without reading entire content into memory.

    Returns 0 for binary files (callers should call _is_binary_file first).
    """
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


def _build_snapshot_lines(
    project_root: str,
    max_depth: int,
    file_formatter,
) -> list[str]:
    """Walk project_root and return annotated tree lines.

    Shared by build_project_snapshot and build_snapshot_with_summaries so
    gitignore filtering, .archive/.git exclusion, and hidden-entry suppression
    are applied identically in both callers.

    file_formatter(prefix, fname, abs_path) -> str
        Called for each non-ignored file.  Return None to skip the line.
    """
    excluded = TOOL_LISTING_EXCLUDED_DIRS
    lines: list[str] = ["== PROJECT STRUCTURE =="]

    root_real = os.path.realpath(project_root)
    _archive_real = os.path.realpath(os.path.join(root_real, ".archive"))
    git_dir = os.path.join(root_real, ".git")

    def _walk(dir_path: str, prefix: str, depth: int) -> None:
        if depth > max_depth:
            return

        try:
            entries = sorted(os.listdir(dir_path))
        except OSError:
            return

        candidate_dirs: list[str] = []
        candidate_files: list[str] = []
        for entry in entries:
            if entry.startswith(".") and entry not in (".env.example",):
                continue
            full = os.path.join(dir_path, entry)
            full_real = os.path.realpath(full)
            if os.path.isdir(full):
                if entry not in excluded and full_real not in (_archive_real, git_dir):
                    candidate_dirs.append(entry)
            elif os.path.isfile(full):
                candidate_files.append(entry)

        # Batch gitignore check for all candidates at this level.
        abs_dirs  = [os.path.join(dir_path, d) for d in candidate_dirs]
        abs_files = [os.path.join(dir_path, f) for f in candidate_files]
        ignored   = _is_git_ignored(abs_dirs + abs_files, root_real)

        dirs  = [d for d, p in zip(candidate_dirs,  abs_dirs)  if p not in ignored]
        files = [f for f, p in zip(candidate_files, abs_files) if p not in ignored]

        for fname in files:
            abs_path = os.path.join(dir_path, fname)
            line = file_formatter(prefix, fname, abs_path)
            if line is not None:
                lines.append(line)

        for dname in dirs:
            lines.append(f"{prefix}{dname}/")
            _walk(os.path.join(dir_path, dname), prefix + "  ", depth + 1)

    _walk(project_root, "  ", 0)
    return lines


def _apply_token_budget(lines: list[str], effective_max_tokens: int) -> str:
    """Join lines; truncate with a notice if the estimate exceeds the token budget."""
    result = "\n".join(lines)
    # using 3 chars per token to more accurately reflect code
    if len(result) // 3 <= effective_max_tokens:
        return result
    truncated: list[str] = [lines[0]]
    budget_chars = effective_max_tokens * 3  # consistent with // 3 estimation above
    used = len(truncated[0])
    for line in lines[1:]:
        if used + len(line) + 1 > budget_chars:
            truncated.append("  ... (truncated - project has more files)")
            break
        truncated.append(line)
        used += len(line) + 1
    return "\n".join(truncated)


def build_project_snapshot(
    project_root: str,
    max_depth: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Build an indented directory tree with file annotations.

    For .py files: shows line count, class count, function count.
    For other files: shows file size.

    Respects .gitignore, TOOL_LISTING_EXCLUDED_DIRS, and skips hidden entries.
    Truncates if estimated token count exceeds the configured budget.

    max_tokens overrides the config default; pass int(llm_max_context * SNAPSHOT_CONTEXT_RATIO)
    from call sites that know the LLM's context window.

    project_root must be an explicit path - there is no default fallback.
    """
    if max_depth is None:
        max_depth = _snapshot_max_depth()
    effective_max_tokens = max_tokens if max_tokens is not None else _snapshot_max_tokens()

    project_root = os.path.normpath(os.path.abspath(project_root))

    cache_ttl = _snapshot_cache_ttl()
    cache_key = ("basic", project_root, effective_max_tokens)
    cached = _snapshot_cache.get(cache_key)
    if cached is not None:
        ts, snapshot = cached
        if time.time() - ts < cache_ttl:
            return snapshot

    def _fmt(prefix: str, fname: str, abs_path: str) -> str:
        if fname.endswith(".py"):
            lc, cc, fc = _analyze_py_file(abs_path)
            if cc >= 0:
                return f"{prefix}{fname} - {lc} lines, {cc} classes, {fc} functions"
            return f"{prefix}{fname} - {lc} lines"
        try:
            sz = os.path.getsize(abs_path)
            return f"{prefix}{fname} - {_format_size(sz)}"
        except OSError:
            return f"{prefix}{fname}"

    lines = _build_snapshot_lines(project_root, max_depth, _fmt)
    result = _apply_token_budget(lines, effective_max_tokens)
    _snapshot_cache[cache_key] = (time.time(), result)
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

    # Reject binary files before any further processing.  Binary data cannot
    # be meaningfully summarised as text — callers must treat this as a hard
    # no-op, not an error to retry.
    if _is_binary_file(abs_path):
        return (
            f"BINARY: '{path}' is a binary file (contains null bytes). "
            "Binary files cannot be summarised as text. "
            "Skipping — no summary available."
        )

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
                    parts.append(f"  {cls.name}{bases_str} - lines {cls.line_start}-{cls.line_end}")
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
                    parts.append(f"  {async_prefix}{func.name}({params_str}) - lines {func.line_start}-{func.line_end}")

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
            parts.append("\n(tree-sitter analysis unavailable - showing metadata only)")

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
    abs_path_early = os.path.normpath(os.path.abspath(path))
    if _is_binary_file(abs_path_early):
        return (
            f"BINARY: '{path}' is a binary file. "
            "Binary files cannot be summarised as text — skipping."
        )

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
            # Cache miss - wait for scheduler to dispatch and complete the job.
            # Timeout must exceed LLM_TIMEOUT_SECONDS + realistic queue wait.
            from app.agent.scheduler import wait_for_completion
            from app.agent.config import FILE_SUMMARY_WAIT_TIMEOUT
            loop = _asyncio.get_event_loop()
            completed = await loop.run_in_executor(
                None, wait_for_completion, completion_key, FILE_SUMMARY_WAIT_TIMEOUT
            )
            if not completed:
                logger.warning("file_summary timed out for %s - using structural only", path)
                return structural

        cached = get_file_summary(sha1, filesize)
        if cached:
            short = (getattr(cached, 'short_summary', None) or "").strip()
            header = f"## Summary\n{cached.summary}"
            if short:
                header = f"## Short Summary\n{short}\n\n## Full Summary\n{cached.summary}"
            result = f"{header}\n\n{structural}"
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
    project_root: str,
    max_depth: int | None = None,
    max_tokens: int | None = None,
) -> str:
    """Like build_project_snapshot() but appends a cached summary to each file line.

    Summary text comes from the DB cache (get_file_summary_by_path): prefers
    short_summary (2 sentences, purpose-built for listings), falls back to first
    line of the full summary for older rows.
    Cache miss → file line emitted without summary (no placeholder noise for the LLM).

    Respects .gitignore, TOOL_LISTING_EXCLUDED_DIRS, and skips hidden entries —
    identical filtering to build_project_snapshot via the shared _build_snapshot_lines
    helper.

    max_tokens overrides the config default; pass int(llm_max_context * SNAPSHOT_CONTEXT_RATIO)
    from call sites that know the LLM's context window.  Different max_tokens values
    cache independently so a 100k-context agent and a 200k-context agent each get
    the right sized snapshot.

    project_root must be an explicit path - there is no default fallback.
    """
    if max_depth is None:
        max_depth = _snapshot_max_depth()
    effective_max_tokens = max_tokens if max_tokens is not None else _snapshot_max_tokens()

    project_root = os.path.normpath(os.path.abspath(project_root))

    cache_ttl = _snapshot_cache_ttl()
    cache_key = ("summaries", project_root, effective_max_tokens)
    cached = _snapshot_cache.get(cache_key)
    if cached is not None:
        ts, snapshot = cached
        if time.time() - ts < cache_ttl:
            return snapshot

    try:
        from app.database import get_file_summary_by_path as _get_summary
    except Exception:
        _get_summary = None

    def _inline_summary(abs_path: str) -> str:
        if _get_summary is None:
            return ""
        try:
            row = _get_summary(abs_path)
            if row:
                text = (getattr(row, 'short_summary', None) or "").strip() \
                    or (row.summary or "").split("\n")[0].strip()
                if text:
                    return f" - {text}"
        except Exception:
            pass
        return ""

    def _fmt(prefix: str, fname: str, abs_path: str) -> str:
        summary_suffix = _inline_summary(abs_path)
        if fname.endswith(".py"):
            lc, cc, fc = _analyze_py_file(abs_path)
            if cc >= 0:
                return f"{prefix}{fname} - {lc} lines, {cc} classes, {fc} functions{summary_suffix}"
            return f"{prefix}{fname} - {lc} lines{summary_suffix}"
        try:
            sz = os.path.getsize(abs_path)
            return f"{prefix}{fname} - {_format_size(sz)}{summary_suffix}"
        except OSError:
            return f"{prefix}{fname}{summary_suffix}"

    lines = _build_snapshot_lines(project_root, max_depth, _fmt)
    result = _apply_token_budget(lines, effective_max_tokens)
    _snapshot_cache[cache_key] = (time.time(), result)
    return result
