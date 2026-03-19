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

import glob as _glob
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

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
)

# ---------------------------------------------------------------------------
# Safety helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Directory exclusions for listing tools
# ---------------------------------------------------------------------------
# Directories named in this set are hidden from list_directory, find_files,
# and search_files. Add any folder you never want agents to browse or search.
# The project-root .archive folder (ARCHIVE_DIR) is always excluded regardless
# of this list; .git is always excluded regardless of this list.
# These are matched against the *basename* of each directory entry.

LISTING_EXCLUDED_DIRS: set[str] = {
    ".archive",        # soft-delete holding area (also excluded by absolute path)
    ".git",            # git internals — agents use git_* tools instead
    "venv",            # Python virtual environment
    ".venv",           # alternate venv name
    "__pycache__",     # compiled bytecode
    "node_modules",    # JS dependencies
    ".mypy_cache",     # mypy type-check cache
    ".pytest_cache",   # pytest cache
    ".ruff_cache",     # ruff linter cache
    "dist",            # build output
    "build",           # build output
    ".eggs",           # setuptools eggs
}

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
    Resolve path and assert it stays inside PROJECT_ROOT.
    Returns the absolute resolved path string.
    Raises ValueError if the path escapes the project root.
    """
    resolved = os.path.realpath(os.path.abspath(path))
    root = os.path.realpath(PROJECT_ROOT)
    if not resolved.startswith(root):
        raise ValueError(
            f"Path '{path}' resolves to '{resolved}' which is outside "
            f"the project root '{root}'. Access denied."
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
        return True, f"Command contains blocked pattern: '{match.group()}'"
    return False, ""


# ---------------------------------------------------------------------------
# File tools
# ---------------------------------------------------------------------------

def read_file(path: str) -> str:
    """Read and return a file's text content."""
    safe_path = _assert_safe_path(path)
    if not os.path.isfile(safe_path):
        return f"ERROR: '{path}' is not a file or does not exist."
    try:
        with open(safe_path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except OSError as exc:
        return f"ERROR reading '{path}': {exc}"


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
        _git_run(["git", "add", safe_path], cwd=PROJECT_ROOT)
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
        _git_run(["git", "add", safe_path], cwd=PROJECT_ROOT)
        return f"OK: appended {len(content)} chars to '{path}'."
    except OSError as exc:
        return f"ERROR appending to '{path}': {exc}"


def list_directory(path: str = ".") -> str:
    """
    List files and directories at the given path.
    Directories named in LISTING_EXCLUDED_DIRS are hidden automatically.
    The project-root archive folder and .git are always hidden regardless of
    the exclusion set.
    """
    safe_path = _assert_safe_path(path)
    if not os.path.isdir(safe_path):
        return f"ERROR: '{path}' is not a directory."

    _archive_real = os.path.realpath(ARCHIVE_DIR)
    root_real = os.path.realpath(PROJECT_ROOT)
    git_dir = os.path.join(root_real, ".git")

    entries = sorted(os.listdir(safe_path))
    lines: list[str] = []
    hidden = 0
    for entry in entries:
        full = os.path.join(safe_path, entry)
        full_real = os.path.realpath(full)
        # Always hide: project-root archive dir and .git
        if full_real == _archive_real or full_real == git_dir:
            hidden += 1
            continue
        # Hide any directory whose basename is in the exclusion set
        if os.path.isdir(full) and entry in LISTING_EXCLUDED_DIRS:
            hidden += 1
            continue
        kind = "DIR " if os.path.isdir(full) else "FILE"
        lines.append(f"{kind}  {entry}")

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
                            if len(results) >= 200:
                                results.append("... (truncated at 200 results)")
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
    lines = [os.path.relpath(m, safe_dir) for m in sorted(filtered)[:200]]
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

    shutil.move(safe_path, dest)

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

def _git_run(args: list[str], cwd: str = PROJECT_ROOT) -> tuple[int, str, str]:
    """Internal helper — run a git command, return (returncode, stdout, stderr)."""
    try:
        result = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
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
    max_count = min(max(1, max_count), 100)  # clamp to [1, 100]
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
        # Convert to relative path from PROJECT_ROOT for git
        rel_path = os.path.relpath(safe_path, PROJECT_ROOT).replace("\\", "/")
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
    Produce a structured markdown architecture document.
    Stays in agent context — NOT written to disk.
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

    return "\n".join(lines)


def generate_mermaid_diagram(diagram_type: str, definition: str) -> str:
    """
    Validate and format a Mermaid diagram definition.
    Returns the formatted mermaid markup as a string.
    """
    valid_types = {"flowchart", "sequence", "class", "er", "gantt", "stateDiagram", "pie"}
    # Normalize common aliases
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

    # Check if definition already starts with a diagram directive
    stripped = definition.strip()
    if any(stripped.startswith(d) for d in type_map.values()):
        return f"```mermaid\n{stripped}\n```"

    return f"```mermaid\n{normalized}\n{stripped}\n```"


def generate_interface_contract(component_name: str, provides: list, consumes: list) -> str:
    """
    Define the API surface / interface contract for a component.
    Returns a structured JSON string describing what this component provides and consumes.
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

    return _json.dumps(contract, indent=2)


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
# Tool registry + schemas
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "read_file": read_file,
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
    "generate_architecture_doc": generate_architecture_doc,
    "generate_mermaid_diagram": generate_mermaid_diagram,
    "generate_interface_contract": generate_interface_contract,
    "spawn_research_agent": spawn_research_agent,
    "run_shell_security": _run_shell_security_wrapper,
    "run_shell_review": _run_shell_review_wrapper,
}

TOOL_SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read the full text content of a file within the project.",
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
            "name": "run_shell",
            "description": (
                "Execute a shell command in the project directory. "
                "Destructive commands (rm -rf, del, format, etc.) are blocked. "
                "30-second hard timeout."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute."},
                    "working_dir": {"type": "string", "description": "Working directory for the command.", "default": "."},
                },
                "required": ["command"],
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
]


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
    try:
        result = func(**arguments)
        # Ensure the result is always a string
        if not isinstance(result, str):
            import json
            result = json.dumps(result, default=str)
        return result
    except TypeError as exc:
        return f"ERROR: Bad arguments for tool '{name}': {exc}"
    except ValueError as exc:
        return f"ERROR: {exc}"
    except Exception as exc:
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

    # All other tools — synchronous
    return dispatch_tool(name, arguments)
