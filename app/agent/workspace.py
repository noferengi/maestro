"""
app/agent/workspace.py — workspace file operations with deletion audit trail.

All file deletions create an ArchivedFile DB record so they can be listed via
the API and restored without filesystem scanning.  Paths in the DB are stored
relative to the project root so records survive project directory moves.

Archive layout on disk:
    {project_root}/.archive/{YYYY-MM-DD_HH-MM-SS}/{task_id}/{original_rel_path}

Collision-safety: if two deletions of the same relative path happen in the same
second the timestamp folder gets a numeric suffix (_1, _2, …).
"""

from __future__ import annotations

import os
import shutil
from datetime import datetime
from typing import Optional

import app.database as _db

_ARCHIVE_SUBDIR = ".archive"


def _collision_safe_dest(archive_root: str, timestamp: str, task_id: str, rel_path: str) -> str:
    base = os.path.join(archive_root, timestamp, task_id, rel_path)
    if not os.path.exists(base):
        return base
    n = 1
    while True:
        candidate = os.path.join(archive_root, f"{timestamp}_{n}", task_id, rel_path)
        if not os.path.exists(candidate):
            return candidate
        n += 1


def delete_file(
    task_id: str,
    path: str,
    effective_root: str,
    project_root: str,
) -> "_db.ArchivedFile":
    """Move path (relative to effective_root) to .archive/ and insert a DB record.

    Returns the ArchivedFile record (contains .id for reporting back to the user).
    Raises FileNotFoundError if path doesn't exist.
    """
    if not os.path.isabs(path):
        abs_path = os.path.normpath(os.path.join(effective_root, path))
    else:
        abs_path = os.path.normpath(path)

    if not os.path.exists(abs_path):
        raise FileNotFoundError(f"'{path}' does not exist")

    rel_from_worktree = os.path.relpath(abs_path, effective_root)
    archive_root = os.path.join(project_root, _ARCHIVE_SUBDIR)
    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    dest_abs = _collision_safe_dest(archive_root, timestamp, task_id, rel_from_worktree)
    os.makedirs(os.path.dirname(dest_abs), exist_ok=True)
    shutil.move(abs_path, dest_abs)

    # Store both paths relative to project_root for portability across moves/renames
    original_rel = os.path.relpath(abs_path, project_root)
    archive_rel = os.path.relpath(dest_abs, project_root)

    return _db.create_archived_file(task_id, original_rel, archive_rel)


def undelete_file(
    archive_id: int,
    project_root: str,
    restore_path: Optional[str] = None,
) -> str:
    """Restore an archived file to its original path (or restore_path).

    Returns the final restored absolute path.
    Raises FileNotFoundError if the archive record or the archived file is missing.
    Raises FileExistsError if the target path already exists.
    """
    record = _db.get_archived_file(archive_id)
    if record is None:
        raise FileNotFoundError(f"No archived file with id={archive_id}")

    archive_abs = os.path.normpath(os.path.join(project_root, record.archive_path))
    if not os.path.exists(archive_abs):
        raise FileNotFoundError(
            f"Archive file not found at '{archive_abs}' — "
            "the project may have been moved or the archive cleared"
        )

    if restore_path is not None:
        if not os.path.isabs(restore_path):
            restore_abs = os.path.normpath(os.path.join(project_root, restore_path))
        else:
            restore_abs = os.path.normpath(restore_path)
    else:
        restore_abs = os.path.normpath(os.path.join(project_root, record.original_path))

    if os.path.exists(restore_abs):
        raise FileExistsError(
            f"'{restore_abs}' already exists; provide a restore_path to override"
        )

    os.makedirs(os.path.dirname(restore_abs), exist_ok=True)
    shutil.move(archive_abs, restore_abs)
    _db.mark_archived_file_restored(archive_id)
    return restore_abs


def rename_file(src: str, dst: str, effective_root: str) -> None:
    """Rename src to dst within effective_root. Raises FileExistsError if dst exists."""
    src_abs = os.path.normpath(
        src if os.path.isabs(src) else os.path.join(effective_root, src)
    )
    dst_abs = os.path.normpath(
        dst if os.path.isabs(dst) else os.path.join(effective_root, dst)
    )
    if not os.path.exists(src_abs):
        raise FileNotFoundError(f"'{src}' does not exist")
    if os.path.exists(dst_abs):
        raise FileExistsError(f"'{dst}' already exists — no silent overwrites")
    os.makedirs(os.path.dirname(dst_abs), exist_ok=True)
    shutil.move(src_abs, dst_abs)


def write_file(path: str, content: str, effective_root: str) -> None:
    """Write content to path (relative to effective_root). Creates intermediate dirs."""
    abs_path = os.path.normpath(
        path if os.path.isabs(path) else os.path.join(effective_root, path)
    )
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as fh:
        fh.write(content)


def read_file(path: str, effective_root: str) -> str:
    """Read file content (path relative to effective_root)."""
    abs_path = os.path.normpath(
        path if os.path.isabs(path) else os.path.join(effective_root, path)
    )
    with open(abs_path, "r", encoding="utf-8") as fh:
        return fh.read()


def list_dir(path: str, effective_root: str) -> list[str]:
    """List immediate entries (dirs end with '/') relative to effective_root."""
    abs_path = os.path.normpath(
        os.path.join(effective_root, path) if path else effective_root
    )
    if not os.path.isdir(abs_path):
        return []
    entries = []
    with os.scandir(abs_path) as it:
        for entry in sorted(it, key=lambda e: (not e.is_dir(), e.name)):
            entries.append(entry.name + ("/" if entry.is_dir() else ""))
    return entries
