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
    """List files and directories at the given path."""
    safe_path = _assert_safe_path(path)
    if not os.path.isdir(safe_path):
        return f"ERROR: '{path}' is not a directory."
    entries = sorted(os.listdir(safe_path))
    lines: list[str] = []
    for entry in entries:
        full = os.path.join(safe_path, entry)
        kind = "DIR " if os.path.isdir(full) else "FILE"
        lines.append(f"{kind}  {entry}")
    return "\n".join(lines) if lines else "(empty directory)"


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

    for root, dirs, files in os.walk(safe_dir):
        # Skip hidden dirs and venv
        dirs[:] = [
            d for d in dirs
            if not d.startswith(".") and d not in ("venv", "__pycache__", "node_modules")
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


def find_files(glob_pattern: str, directory: str = ".") -> str:
    """
    Find files matching a glob pattern under directory.
    Returns one path per line (up to 200 results).
    """
    safe_dir = _assert_safe_path(directory)
    full_pattern = os.path.join(safe_dir, "**", glob_pattern)
    matches = _glob.glob(full_pattern, recursive=True)
    # Filter out venv / __pycache__
    filtered = [
        m for m in matches
        if "venv" not in m.split(os.sep)
        and "__pycache__" not in m.split(os.sep)
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
    Safely 'delete' a file by moving it into .archive/<timestamp>/<original_path>/.
    A _reason.txt sidecar is written alongside if reason is provided.
    NEVER calls shutil.rmtree, os.remove, or any destructive primitive.
    Returns the archive destination path on success.
    """
    safe_path = _assert_safe_path(path)
    if not os.path.exists(safe_path):
        return f"ERROR: '{path}' does not exist — nothing to archive."

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    # Preserve relative structure inside the archive
    rel_path = os.path.relpath(safe_path, PROJECT_ROOT)
    dest = os.path.join(ARCHIVE_DIR, timestamp, rel_path)
    os.makedirs(os.path.dirname(dest), exist_ok=True)

    shutil.move(safe_path, dest)

    if reason:
        reason_file = dest + "._reason.txt"
        with open(reason_file, "w", encoding="utf-8") as fh:
            fh.write(reason)

    return f"OK: archived '{path}' → '{dest}'."


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
# Tool registry + schemas
# ---------------------------------------------------------------------------

TOOL_REGISTRY: dict[str, Any] = {
    "read_file": read_file,
    "write_file": write_file,
    "append_file": append_file,
    "list_directory": list_directory,
    "search_files": search_files,
    "find_files": find_files,
    "archive_file": archive_file,
    "run_shell": run_shell,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_create_branch": git_create_branch,
    "git_commit": git_commit,
    "git_checkout": git_checkout,
    "get_task": get_task,
    "update_task_status": update_task_status,
    "append_task_history": append_task_history,
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
