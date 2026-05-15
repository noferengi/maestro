# Phase 6 — Workspace Isolation & Arch Category CRUD

> **Status:** Not started — requires Phase 1  
> **Depends on:** Phase 1 (archived_files table, pipeline_arch_categories table);  
>   Phase 2 recommended (stage_config available) but not strictly blocking  
> **Estimated effort:** 3 days  
> **Goal:** Add deletion-protection and audit-trail to the workspace layer; make
> arch categories CRUD-able per pipeline template; remove the hardcoded category
> list from `kanban.js`.

---

## Part A: Workspace Scratch Pads & Deletion Protection

### Background

`app/agent/worktree.py` already creates per-task git worktrees at
`.maestro-worktrees/{task_id}/`. This is domain-agnostic and works for any file-
writing agent. The gap is: deleting a file inside a worktree permanently removes it
with no recovery path.

### `workspace.py` — new module

```python
# app/agent/workspace.py

def delete_file(task_id: str, path: str) -> ArchivedFileRecord:
    """
    Move `path` (relative to the task's worktree root) into the project's
    .archive/ directory with a timestamped, collision-safe name.
    Insert an archived_files DB record.
    Returns the record so the agent can report the archive_id to the user.
    """

def undelete_file(archive_id: int, restore_path: str | None = None) -> str:
    """
    Move the archived file back to its original path (or restore_path if provided).
    Update archived_files.restored_at.
    Raises FileExistsError if the target path already exists and restore_path is None.
    Returns the final restored path.
    """

def rename_file(task_id: str, src: str, dst: str) -> None:
    """
    Rename src to dst within the task's worktree. If dst already exists,
    raises FileExistsError — no silent overwrites.
    """

def write_file(task_id: str, path: str, content: str) -> None:
    """
    Write content to path in the task's worktree. Creates intermediate
    directories. Does NOT use deletion protection (writes are not archived).
    """

def read_file(task_id: str, path: str) -> str:
    """Read file content from the task's worktree."""

def list_dir(task_id: str, path: str = "") -> list[str]:
    """List files and directories relative to the task's worktree root."""
```

### Archive path scheme

```
{project_root}/.archive/
  {YYYY-MM-DD_HH-MM-SS}/
    {task_id}/
      {original_relative_path}   # exact structure preserved
```

Collision resolution: if the timestamp folder already contains a file at the same
path, append `_{n}` to the folder name (`2026-05-15_23-00-00_1/`, `_2/`, etc.).
The `archived_files.archive_path` stores the full absolute archive path so
`undelete_file` can find it without filesystem scanning.

### `archived_files` DB record

| Column | Value |
|---|---|
| `task_id` | The card that deleted the file |
| `original_path` | Absolute path before deletion |
| `archive_path` | Absolute path in `.archive/` |
| `deleted_at` | Timestamp of deletion |
| `restored_at` | NULL until undeleted |

### Agent tool surface changes

The existing `delete_file` tool available to agents (in `config.py:build_tool_schemas`)
is replaced by the `workspace.delete_file()` wrapper. The agent's view: call
`delete_file(path)` → file is gone from their worktree, archive record created, `archive_id`
returned in the tool result. The agent can pass `archive_id` to the user in its output
so a human can reverse the deletion if needed.

### API endpoints

```
GET  /api/tasks/{task_id}/archived-files   — list archived files for this task
POST /api/tasks/{task_id}/undelete         body: {archive_id, restore_path?}
     — restore a file; returns the final restored path
```

---

## Part B: Arch Category CRUD

### Background

`kanban.js` has a hardcoded `ARCH_CATEGORY_COLORS` object with 14 fixed category
keys. Arch cards are filtered and colored based on this. It cannot be changed without
editing JS source.

After this phase, categories come from `pipeline_arch_categories` rows for the
project's assigned template.

### Backend changes

Phase 3 already defines:
```
GET/POST/PUT/DELETE /api/pipelines/{id}/arch-categories
```

Add two more endpoints:

```
GET /api/projects/{name}/arch-categories
    — returns the arch categories for the project's assigned pipeline template.
      Used by kanban.js on load. Falls back to a hardcoded default set if the
      project has no template assigned (transition period).
```

### Frontend changes (`kanban.js`)

1. In `loadTasksFromDatabase()`, add a fetch to
   `GET /api/projects/{name}/arch-categories`.
2. Replace `ARCH_CATEGORY_COLORS` (hardcoded const) with `archCategoryMap`
   (populated from the API response).
3. `renderArchBar()` reads from `archCategoryMap` instead of the hardcoded const.
4. The arch bar header gets an edit button (⚙) that opens an arch category
   management panel — add, rename, recolor, reorder, delete categories.
5. Deleting a category that has existing arch cards: cards are reassigned to a
   "General" fallback category, or the user selects a replacement.

### Arch category management panel

This is a simple modal, not a canvas. Rows in a sortable list:

```
┌─────────────────────────────────────────────────────────┐
│ Arch Categories — Novel Writing Pipeline          [Close] │
├─────────────────────────────────────────────────────────┤
│ ≡  Characters    [████ #7c3aed]  [Rename] [Delete]      │
│ ≡  Themes        [████ #1e40af]  [Rename] [Delete]      │
│ ≡  Plot          [████ #065f46]  [Rename] [Delete]      │
│ ≡  World         [████ #92400e]  [Rename] [Delete]      │
│                                                         │
│                                          [+ Add Category]│
└─────────────────────────────────────────────────────────┘
```

Drag handles (≡) reorder categories. Color picker opens inline.

### Per-stage arch category visibility

`pipeline_stages.config` already includes `arch_category_keys` (a list of category
keys whose arch cards are injected into the agent's system prompt). The property panel
in Phase 4 shows these as checkboxes. No new backend work needed — the stage config
already carries this.

The agent loop reads `stage_config.arch_category_keys`, fetches the corresponding
arch cards from the DB, and prepends them to the system prompt as a context block:

```
=== Project Knowledge ===
[Characters]
- Elara Voss: protagonist, cartographer, late 30s, stoic
- Brennan Cole: antagonist, merchant guild leader

[Themes]
- The cost of progress
- Inherited obligation
========================
```

---

## Test Criteria

**Workspace:**
- Agent deletes a file via `workspace.delete_file()` → file absent from worktree,
  present in `.archive/`, `archived_files` row created
- `POST /api/tasks/{id}/undelete` with valid `archive_id` → file restored to
  original path, `restored_at` populated
- Two deletions of files with identical relative paths in the same second → both
  archived without collision (timestamp folder disambiguated with `_1`, `_2`)

**Arch categories:**
- Create a new pipeline template, add 3 custom categories → `GET /api/projects/{name}/arch-categories` returns them
- Delete a category that has arch cards → cards reassigned to General (or user-chosen replacement), no orphan arch cards
- `kanban.js` renders arch bar using dynamic categories; removing the hardcoded constant causes no JS errors

---

## Risk Factors

**`.archive/` in git** — the project's `.gitignore` must exclude `.archive/` so
archived files don't appear as untracked changes in the agent's worktree view. Add
`.archive/` to the `.gitignore` auto-update logic in `worktree.py` (same place where
`.maestro-worktrees/` is currently excluded).

**Cross-platform paths in `archived_files`** — `archive_path` stores absolute paths.
On Windows these contain drive letters (`D:\workspace\...`). If the project is moved
to a different drive, `undelete_file` will fail to find the archive. Store paths
relative to the project root instead, and resolve at runtime.
