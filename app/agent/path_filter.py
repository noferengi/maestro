"""
app/agent/path_filter.py
------------------------
Central authority for path filtering and exclusion logic.

Consolidates .gitignore-based filtering and built-in directory exclusions
(TOOL_LISTING_EXCLUDED_DIRS) into a single, efficient, and consistent API.
Used by snapshots, tools, research agents, and the intake pipeline to ensure
consistent codebase visibility.
"""

from __future__ import annotations

import logging
import os
import subprocess
from functools import lru_cache
from typing import Iterable, Set

from app.agent.config import TOOL_LISTING_EXCLUDED_DIRS

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Core Filter API
# ---------------------------------------------------------------------------

def is_ignored(path: str, project_root: str) -> bool:
    """Return True if path should be excluded from processing.

    A path is ignored if:
      1. Any of its segments match TOOL_LISTING_EXCLUDED_DIRS (built-in).
      2. It is a hidden file/dir (starts with '.') and not explicitly allowed.
      3. It matches a pattern in the project's .gitignore.
    """
    abs_path = os.path.normpath(os.path.abspath(path))
    root_abs = os.path.normpath(os.path.abspath(project_root))

    # 1. Check built-in exclusions (fast)
    rel_to_root = os.path.relpath(abs_path, root_abs)
    segments = rel_to_root.replace("\\", "/").split("/")

    for seg in segments:
        if not seg: continue
        if seg in TOOL_LISTING_EXCLUDED_DIRS:
            return True
        if seg.startswith(".") and seg not in (".env.example", ".gitignore", ".geminiignore"):
            # Hidden files/dirs are ignored unless explicitly allowed.
            # We allow .gitignore itself because tools might need to read it.
            return True

    # 2. Check .gitignore (requires subprocess)
    ignored_set = get_ignored_paths([abs_path], root_abs)
    return abs_path in ignored_set


def filter_paths(paths: Iterable[str], project_root: str) -> list[str]:
    """Efficiently filter a list of paths based on exclusion rules.

    Uses batching for gitignore checks to maintain performance on large lists.
    """
    root_abs = os.path.normpath(os.path.abspath(project_root))
    candidates = []

    # Fast pass: built-in and hidden
    for p in paths:
        abs_p = os.path.normpath(os.path.abspath(p))
        rel_to_root = os.path.relpath(abs_p, root_abs)
        segments = rel_to_root.replace("\\", "/").split("/")

        is_builtin_ignored = False
        for seg in segments:
            if not seg: continue
            if seg in TOOL_LISTING_EXCLUDED_DIRS or (seg.startswith(".") and seg not in (".env.example", ".gitignore", ".geminiignore")):
                is_builtin_ignored = True
                break

        if not is_builtin_ignored:
            candidates.append(abs_p)

    if not candidates:
        return []

    # Batch pass: .gitignore
    ignored = get_ignored_paths(candidates, root_abs)
    return [p for p in candidates if p not in ignored]


# ---------------------------------------------------------------------------
# Gitignore Implementation
# ---------------------------------------------------------------------------

def get_ignored_paths(abs_paths: Iterable[str], project_root: str) -> Set[str]:
    """Batch-check which of the given absolute paths are gitignored.

    Returns a set of absolute paths that git considers ignored.
    """
    if not abs_paths:
        return set()

    try:
        # Convert to relative paths for git check-ignore
        path_list = list(abs_paths)
        rel_paths = [os.path.relpath(p, project_root) for p in path_list]

        # We use -z (null-terminated) for safety with spaces/special chars
        result = subprocess.run(
            ["git", "check-ignore", "--stdin", "-z"],
            input="\0".join(rel_paths).encode('utf-8') + b"\0",
            capture_output=True, cwd=project_root, timeout=10,
        )

        if result.returncode not in (0, 1):
            return set()

        ignored_rels = {r for r in result.stdout.decode('utf-8', errors='replace').split("\0") if r}

        # Map back to absolute paths
        return {p for p, r in zip(path_list, rel_paths) if r in ignored_rels}

    except Exception as exc:
        logger.debug("get_ignored_paths failed for %s: %s", project_root, exc)
        return set()

# ---------------------------------------------------------------------------
# OS Walk helper
# ---------------------------------------------------------------------------

def walk_safe(project_root: str):
    """A version of os.walk that respects all exclusion rules.

    Yields (dirpath, dirnames, filenames) but prunes dirnames in-place
    to prevent descending into ignored directories.
    """
    root_abs = os.path.normpath(os.path.abspath(project_root))

    for dirpath, dirnames, filenames in os.walk(root_abs):
        # 1. Built-in pruning (modifies dirnames in-place)
        dirnames[:] = [d for d in dirnames if d not in TOOL_LISTING_EXCLUDED_DIRS and not d.startswith(".")]

        # 2. .gitignore pruning for directories
        if dirnames:
            abs_dirs = [os.path.join(dirpath, d) for d in dirnames]
            ignored_dirs = get_ignored_paths(abs_dirs, root_abs)
            dirnames[:] = [d for d, abs_d in zip(dirnames, abs_dirs) if abs_d not in ignored_dirs]

        # 3. Filter filenames
        abs_files = [os.path.join(dirpath, f) for f in filenames]
        # Fast filter for hidden files first
        filenames_candidates = [f for f in filenames if not f.startswith(".") or f == ".env.example"]
        if filenames_candidates:
            abs_files_candidates = [os.path.join(dirpath, f) for f in filenames_candidates]
            ignored_files = get_ignored_paths(abs_files_candidates, root_abs)
            filenames_final = [f for f, abs_f in zip(filenames_candidates, abs_files_candidates) if abs_f not in ignored_files]
            yield dirpath, dirnames, filenames_final
        else:
            yield dirpath, dirnames, []
