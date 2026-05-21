# Gap 5 — Controlled self-modification

**Status:** Complete — all 9 phases implemented and 906 tests green (2026-05-19)  
**Effort:** Large  
**Priority:** Low until gaps 1-4 are stable — highest risk item

---

## Problem

`_assert_safe_write_path()` explicitly blocks writes outside `effective_root`.
Agents are isolated to user project directories by design. Maestro cannot edit its own
source code, pipelines defined in Python, or agent system prompts that live in code
rather than the DB. Self-repair beyond what `update_pipeline_stage` covers (DB-stored
configs) requires human edits.

---

## Design decisions (settled)

| Decision | Resolution |
|---|---|
| **Project designation** | Reserved project name `_maestro_self` (hard-coded check in path guard) AND `[maestro_capabilities] can_self_modify = false` toggle — both must be true before any write is allowed. Documented in `INSTRUCTIONS.md`. |
| **Writable paths** | Hardcoded allowlist file at `app/agent/self_modification_allowlist.py`. The allowlist file itself appears on the list; comments inside explicitly warn that edits to this file are significant and will appear in every diff. No directory wildcards — file-level granularity only. |
| **Test gate** | Full test suite (`pytest app/tests/ -q`) must pass. Two separate toggles in `[maestro_capabilities]`: `can_auto_merge_human_review` (general human_review auto-merge) and `can_auto_merge_self_modification` (self-modification PRs, only active if the first is also true). |
| **Revert mechanism** | `vote_to_revert(reason)` tool available to all agents after server restart. Votes logged to `revert_votes` table. When vote count meets threshold, system automatically runs `git revert` of the merge commit + creates a PIP card with the full vote log as description. |
| **Branch strategy** | Self-modification changes accumulate on `maestro/self-improvement` integration branch. Human merges to main manually. Main is always exactly one human action away from any self-modification landing. |

---

## Key codebase facts (verified before implementation)

- `_assert_safe_write_path()` lives in **`app/agent/tools.py`** (~line 436), NOT `workspace.py`.
  `workspace.py` is the deletion-audit module — the path guard exemption goes in `tools.py`.
- `_task_project_name: ContextVar[str | None]` already exists in `tools.py` (~line 69).
  The path guard reads this to check for `_maestro_self` without any new plumbing.
- `MAESTRO_CAPABILITIES` singleton is loaded at startup from `config.py`.
  The path guard just reads `MAESTRO_CAPABILITIES.can_self_modify` — no new parameter passing.
- The agent loop file is **`loop.py`**, not `maestro_loop.py`. The scheduler is `scheduler.py`.
  Integration branch logic belongs in `loop.py` (self-mod task routing) and `scheduler.py`
  (dispatch guard for self-mod tasks).
- Latest migration is **0089** → new migrations are **0090** (revert_votes) and **0091** (self_mod_merge_log).
- `maestro/self-improvement` starts with `maestro/` so the existing `git_checkout` branch
  prefix guard already allows it without changes.

---

## Implementation plan

### Phase 1 — Config (`config.py` + `maestro.ini`)

**`maestro.ini`** — extend `[maestro_capabilities]` section:

```ini
[maestro_capabilities]
# ... existing keys from Gap 4 ...

# Allow _maestro_self project to write to TheMaestro source tree.
# Default: false. Both this flag AND the project name must match.
can_self_modify = false

# Allow Maestro to auto-merge cards that reach human_review stage.
# Default: false.
can_auto_merge_human_review = false

# Allow Maestro to auto-merge self-modification PRs specifically.
# Only effective when can_auto_merge_human_review = true.
# Default: false.
can_auto_merge_self_modification = false
```

**`app/agent/config.py`** — extend `MaestroCapabilities` dataclass:

```python
@dataclass
class MaestroCapabilities:
    can_create_objectives: bool = False
    can_complete_objectives: bool = True
    can_create_cards: bool = True
    max_objectives_per_tick: int = 2
    can_self_modify: bool = False
    can_auto_merge_human_review: bool = False
    can_auto_merge_self_modification: bool = False
```

**Reserved project name constant** — add to `app/agent/config.py`:

```python
SELF_MODIFICATION_PROJECT: str = "_maestro_self"
SELF_MOD_INTEGRATION_BRANCH: str = "maestro/self-improvement"
SELF_MOD_REVERT_VOTE_THRESHOLD: int = _getint("maestro_capabilities", "revert_vote_threshold", None, 3)
```

**`INSTRUCTIONS.md`** — add a section documenting:
- How to create a `_maestro_self` project and enable `can_self_modify`.
- Warning: this is an advanced feature. Maestro will write to its own source tree.
- Link to the allowlist file so users know what is in scope.

---

### Phase 2 — Self-modification allowlist (new file)

**`app/agent/self_modification_allowlist.py`**:

```python
"""
Paths the _maestro_self project is allowed to write to.

THIS FILE ITSELF IS ON THE LIST. Edits to this file are significant:
they change what Maestro can write to. Every such edit will appear
prominently in git diffs and human review. Edit with care.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent.parent  # repo root

def _p(*parts: str) -> str:
    return str((_ROOT / Path(*parts)).resolve())

ALLOWED_PATHS: frozenset[str] = frozenset({
    # This file — explicitly self-referential.
    _p("app/agent/self_modification_allowlist.py"),

    # Agent system
    _p("app/agent/tools.py"),
    _p("app/agent/tools_math.py"),
    _p("app/agent/consult_agent.py"),
    _p("app/agent/loop.py"),
    _p("app/agent/config.py"),
    _p("app/agent/verifiers.py"),
    _p("app/agent/pipeline_router.py"),
    _p("app/agent/agent_registry.py"),
    _p("app/agent/custom_llm_agent.py"),
    _p("app/agent/doc_store.py"),
    _p("app/agent/card_factory.py"),
    _p("app/agent/factory_sources.py"),

    # Database layer
    _p("app/database/crud_autopilot.py"),
    _p("app/database/crud_tasks.py"),
    _p("app/database/crud_pipeline.py"),
    _p("app/database/crud_malleable.py"),
    _p("app/database/crud_documents.py"),

    # Frontend
    _p("app/web/kanban.js"),
    _p("app/web/index.html"),
    _p("app/web/style.css"),
    _p("app/web/pipeline_editor.js"),
    _p("app/web/pipeline_editor.html"),
    _p("app/web/pipeline_editor.css"),

    # Tests (agents may add tests for their own changes)
    _p("app/tests/test_self_modification.py"),
    _p("app/tests/test_consult_maestro.py"),
    _p("app/tests/test_autopilot_objectives.py"),
    _p("app/tests/test_math_tools.py"),
    _p("app/tests/test_objective_hierarchy.py"),
})

# Permanently off-limits regardless of the allowlist above.
HARD_BLOCKED: frozenset[str] = frozenset({
    _p("app/agent/tools.py"),           # contains _assert_safe_write_path itself — can't be removed from guard
    _p("app/agent/workspace.py"),       # deletion audit module
    _p("app/migrations"),               # never auto-generate migrations
    _p(".env"),
    _p("maestro.ini"),                  # config must remain human-controlled
    _p("app/tests/conftest.py"),
})
```

**Note on HARD_BLOCKED vs ALLOWED_PATHS interaction:**
- `app/agent/tools.py` appears in `ALLOWED_PATHS` (agents may improve tooling) but also in `HARD_BLOCKED`.
- The HARD_BLOCKED check fires AFTER the allowlist check. Wait — actually re-read the design:
  `HARD_BLOCKED` means "can never be removed from ALLOWED_PATHS". This is a different check.
  The original intent: `self_modification_allowlist.py` is in `ALLOWED_PATHS` (agents can edit
  the allowlist) but is also in `HARD_BLOCKED` to ensure agents can't remove it from the list.
  For the path guard, `_assert_on_allowlist` simply checks `path in ALLOWED_PATHS`.
  The HARD_BLOCKED set is a meta-constraint: a future edit to the allowlist that would remove
  a HARD_BLOCKED path should itself be blocked. This can be checked dynamically when the
  allowlist file is being written.

  **Simplification for MVP**: In Phase 2, `HARD_BLOCKED` is enforced at write-time only:
  if the write path is in `HARD_BLOCKED`, raise even if the project is `_maestro_self` with
  capability enabled. `tools.py` should appear in `HARD_BLOCKED` but NOT `ALLOWED_PATHS` so
  agents cannot rewrite the safety guard.

---

### Phase 3 — Path guard exemption (`tools.py`)

**`_assert_safe_write_path`** — add a self-modification exemption block immediately after
the `_assert_safe_path(path)` call and before the containment check:

```python
from app.agent.config import (
    MAESTRO_CAPABILITIES, SELF_MODIFICATION_PROJECT, MAESTRO_GIT_ROOT
)
from app.agent.self_modification_allowlist import ALLOWED_PATHS, HARD_BLOCKED

def _assert_on_allowlist(resolved: str) -> None:
    if resolved in HARD_BLOCKED:
        raise ValueError(
            f"WRITE REJECTED: '{resolved}' is permanently off-limits for self-modification. "
            "This path cannot be modified by agents even with all toggles enabled."
        )
    if resolved not in ALLOWED_PATHS:
        raise ValueError(
            f"WRITE REJECTED: '{resolved}' is not on the self-modification allowlist. "
            "Add it to app/agent/self_modification_allowlist.py ALLOWED_PATHS to permit this write."
        )

def _assert_safe_write_path(path: str) -> str:
    resolved = _assert_safe_path(path)   # Layer 0: blocks .git + .archive
    effective_root = _task_git_cwd.get() or PROJECT_ROOT
    root = os.path.realpath(effective_root)

    # Self-modification exemption: _maestro_self project with can_self_modify enabled
    # may write to the Maestro source tree, but only to allowlisted paths.
    if MAESTRO_GIT_ROOT and resolved.startswith(MAESTRO_GIT_ROOT + os.sep):
        project_name = _task_project_name.get() or ""
        if project_name == SELF_MODIFICATION_PROJECT and MAESTRO_CAPABILITIES.can_self_modify:
            _assert_on_allowlist(resolved)
            return resolved
        raise ValueError(
            f"WRITE REJECTED: '{path}' is inside the Maestro source tree. "
            f"Only the '{SELF_MODIFICATION_PROJECT}' project with can_self_modify=true "
            "may write here, and only to allowlisted paths."
        )

    # Existing checks: containment, segment blocklist, gitignore
    if not resolved.startswith(root + os.sep) and resolved != root:
        raise ValueError(...)
    ...
```

**Important**: `MAESTRO_GIT_ROOT` is already computed in `config.py` (line 155) via
`_resolve_git_root(PROJECT_ROOT)` and exported. It's the normalised absolute path of the
repo root. Import it in `tools.py` (it may already be imported — verify).

---

### Phase 4 — DB migrations + ORM models

**Migration 0090** — `app/migrations/versions/0090_revert_votes.py`:

```sql
CREATE TABLE revert_votes (
    id           SERIAL PRIMARY KEY,
    task_id      INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    merge_commit TEXT    NOT NULL,
    reason       TEXT    NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON revert_votes (merge_commit);
GRANT SELECT, INSERT ON revert_votes TO maestro_app;
GRANT USAGE, SELECT ON SEQUENCE revert_votes_id_seq TO maestro_app;
```

**Migration 0091** — `app/migrations/versions/0091_self_mod_merge_log.py`:

```sql
CREATE TABLE self_mod_merge_log (
    id           SERIAL PRIMARY KEY,
    merge_commit TEXT    NOT NULL UNIQUE,
    task_id      INTEGER NOT NULL REFERENCES tasks(id),
    reverted     BOOLEAN NOT NULL DEFAULT false,
    reverted_at  TIMESTAMPTZ NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
GRANT SELECT, INSERT, UPDATE ON self_mod_merge_log TO maestro_app;
GRANT USAGE, SELECT ON SEQUENCE self_mod_merge_log_id_seq TO maestro_app;
```

**`app/database/models.py`** — add two ORM models:

```python
class RevertVote(Base):
    __tablename__ = "revert_votes"
    id           = Column(Integer, primary_key=True)
    task_id      = Column(Integer, ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    merge_commit = Column(Text, nullable=False)
    reason       = Column(Text, nullable=False)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

class SelfModMergeLog(Base):
    __tablename__ = "self_mod_merge_log"
    id           = Column(Integer, primary_key=True)
    merge_commit = Column(Text, nullable=False, unique=True)
    task_id      = Column(Integer, ForeignKey("tasks.id"), nullable=False)
    reverted     = Column(Boolean, nullable=False, default=False)
    reverted_at  = Column(DateTime(timezone=True), nullable=True)
    created_at   = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
```

---

### Phase 5 — CRUD functions (`crud_autopilot.py`)

Add to `app/database/crud_autopilot.py`:

```python
# --- Revert votes ---

def cast_revert_vote(task_id: int, merge_commit: str, reason: str) -> int:
    """Insert a vote and return the total vote count for this merge_commit."""
    db = SessionLocal()
    try:
        db.add(RevertVote(task_id=task_id, merge_commit=merge_commit, reason=reason))
        db.commit()
        count = db.query(RevertVote).filter(RevertVote.merge_commit == merge_commit).count()
        return count
    finally:
        db.close()

def get_revert_votes(merge_commit: str) -> list[dict]:
    db = SessionLocal()
    try:
        rows = db.query(RevertVote).filter(
            RevertVote.merge_commit == merge_commit
        ).order_by(RevertVote.created_at).all()
        return [{"task_id": r.task_id, "reason": r.reason, "created_at": r.created_at} for r in rows]
    finally:
        db.close()

# --- Self-mod merge log ---

def record_self_mod_merge(task_id: int, merge_commit: str) -> None:
    db = SessionLocal()
    try:
        db.add(SelfModMergeLog(task_id=task_id, merge_commit=merge_commit))
        db.commit()
    finally:
        db.close()

def get_latest_self_mod_merge() -> "str | None":
    db = SessionLocal()
    try:
        row = db.query(SelfModMergeLog).filter(
            SelfModMergeLog.reverted == False
        ).order_by(SelfModMergeLog.created_at.desc()).first()
        return row.merge_commit if row else None
    finally:
        db.close()

def mark_self_mod_reverted(merge_commit: str) -> None:
    from datetime import datetime, timezone
    db = SessionLocal()
    try:
        db.query(SelfModMergeLog).filter(
            SelfModMergeLog.merge_commit == merge_commit
        ).update({"reverted": True, "reverted_at": datetime.now(timezone.utc)})
        db.commit()
    finally:
        db.close()
```

---

### Phase 6 — `vote_to_revert` tool (`tools.py`)

Register in TOOL_REGISTRY and TOOL_SCHEMAS. The tool is project-scoped: only available
when `_task_project_name.get() == SELF_MODIFICATION_PROJECT`. Enforce at dispatch time
(raise if wrong project) rather than by allowlist exclusion — simpler.

**Handler (`handle_vote_to_revert`)**:

1. Check `_task_project_name.get() == SELF_MODIFICATION_PROJECT` — error if not.
2. Get `merge_commit = get_latest_self_mod_merge()` — error if None (no merge recorded yet).
3. Get `task_id` from `_task_id_var.get()`.
4. Call `cast_revert_vote(task_id, merge_commit, reason)` → `vote_count`.
5. `threshold = SELF_MOD_REVERT_VOTE_THRESHOLD` (default 3).
6. If `vote_count >= threshold`:
   a. Run `git revert {merge_commit} --no-edit` on `SELF_MOD_INTEGRATION_BRANCH`.
   b. Create a PIP card on `_maestro_self` project. Title: `AUTO-REVERT: {merge_commit[:8]}`.
      Description: format all votes from `get_revert_votes(merge_commit)`.
   c. Call `mark_self_mod_reverted(merge_commit)`.
   d. Return confirmation string.
7. Otherwise return vote tally string.

**Schema**:

```python
"vote_to_revert": {
    "fn": handle_vote_to_revert,
    "schema": {
        "name": "vote_to_revert",
        "description": (
            "Cast a vote to revert the most recent self-modification merge commit. "
            "Use when you observe that a recent change caused regressions or broken "
            "functionality. When votes reach the threshold the system auto-reverts. "
            "Only available in _maestro_self project sessions."
        ),
        "parameters": {
            "reason": {"type": "string", "description": "Why this merge should be reverted."}
        },
        "required": ["reason"]
    }
}
```

---

### Phase 7 — Integration branch + auto-merge gate (`loop.py` + `main.py`)

**`app/agent/loop.py`** — self-modification branch routing:

For `_maestro_self` project tasks, the merge target is `maestro/self-improvement` instead
of `main`. This affects the git merge step in the COMPLETED stage handler.

Key change: when `_task_project_name.get() == SELF_MODIFICATION_PROJECT`:
- Set the integration branch target to `SELF_MOD_INTEGRATION_BRANCH`.
- Before merge attempt, run `run_test_pytest("app/tests/ -q --tb=short")` as a blocking gate.
  If tests fail, mark task as needing human review (do NOT merge).
- If both capability flags are true (`can_auto_merge_human_review` AND
  `can_auto_merge_self_modification`): auto-merge to `maestro/self-improvement`, call
  `record_self_mod_merge(task_id, sha)`.
- If auto-merge disabled: task waits at `HUMAN_REVIEW` stage.

**`app/main.py`** — new route `POST /api/tasks/{id}/self-mod-merge`:

```python
@app.post("/api/tasks/{task_id}/self-mod-merge")
async def self_mod_merge(task_id: int, db: Session = Depends(get_db)):
    task = get_task(task_id, db)
    if task.project != SELF_MODIFICATION_PROJECT:
        raise HTTPException(400, "Not a self-modification project task")
    if not MAESTRO_CAPABILITIES.can_self_modify:
        raise HTTPException(403, "can_self_modify is disabled in maestro.ini")
    # run tests (blocking, 120s timeout)
    result = subprocess.run(
        ["venv/Scripts/python.exe", "-m", "pytest", "app/tests/", "-q", "--tb=short"],
        capture_output=True, text=True, timeout=120, cwd=PROJECT_ROOT
    )
    if result.returncode != 0:
        return {"error": "Tests failed", "output": result.stdout + result.stderr}
    # merge maestro/task-{id} into maestro/self-improvement
    merge_result = _run_git_merge(task_id)
    record_self_mod_merge(task_id, merge_result["sha"])
    return {"merge_commit": merge_result["sha"]}
```

---

### Phase 8 — UI additions (`kanban.js`, `style.css`)

**Self-modification banner**: When `project.name === "_maestro_self"`, render an amber
banner in the project header: "⚠ Self-Modification Mode — writes target Maestro source tree".

**Revert vote badge**: On cards in `_maestro_self` project, show a red badge if the task's
`merge_commit` has any `revert_votes`. Fetch via new `GET /api/tasks/{id}/revert-votes` endpoint.
Clicking shows the vote log in a modal.

**Integration branch indicator**: In the `_maestro_self` project sidebar, show current
`maestro/self-improvement` HEAD SHA and how many commits it is ahead of `main`. Served
by new `GET /api/projects/_maestro_self/integration-branch-status` endpoint.

---

### Phase 9 — Tests (`app/tests/test_self_modification.py`)

New file covering:

1. `_assert_safe_write_path` with `_maestro_self` + `can_self_modify=true` + allowlisted path → succeeds.
2. `_assert_safe_write_path` with `_maestro_self` + `can_self_modify=true` + non-allowlisted path → `ValueError`.
3. `_assert_safe_write_path` with `_maestro_self` + `can_self_modify=true` + `HARD_BLOCKED` path → `ValueError`.
4. `_assert_safe_write_path` with wrong project name + allowlisted path → `ValueError`.
5. `_assert_safe_write_path` with `_maestro_self` + `can_self_modify=false` → `ValueError`.
6. `cast_revert_vote` — correct count returned; second vote from same task still increments.
7. Revert threshold below → no git action; at threshold → action triggered.
8. `can_auto_merge_self_modification=true` + `can_auto_merge_human_review=false` → treated as disabled.
9. HARD_BLOCKED paths: `_maestro_self` with all toggles enabled cannot write to blocked paths.

---

## Files touched

| File | Change |
|---|---|
| `app/agent/tools.py` | `_assert_safe_write_path` exemption + `_assert_on_allowlist` + `vote_to_revert` tool |
| `app/agent/self_modification_allowlist.py` | **New** — ALLOWED_PATHS + HARD_BLOCKED |
| `app/agent/config.py` | `MaestroCapabilities` 3 new fields; `SELF_MODIFICATION_PROJECT`, `SELF_MOD_INTEGRATION_BRANCH`, `SELF_MOD_REVERT_VOTE_THRESHOLD` constants |
| `app/agent/loop.py` | Integration branch routing; test gate; auto-merge gate for `_maestro_self` |
| `app/database/crud_autopilot.py` | `cast_revert_vote`, `get_revert_votes`, `record_self_mod_merge`, `get_latest_self_mod_merge`, `mark_self_mod_reverted` |
| `app/database/models.py` | `RevertVote` + `SelfModMergeLog` ORM models |
| `app/migrations/versions/0090_revert_votes.py` | `revert_votes` table + grants |
| `app/migrations/versions/0091_self_mod_merge_log.py` | `self_mod_merge_log` table + grants |
| `app/main.py` | `POST /api/tasks/{id}/self-mod-merge` + `GET /api/tasks/{id}/revert-votes` + `GET /api/projects/_maestro_self/integration-branch-status` |
| `maestro.ini` | `can_self_modify`, `can_auto_merge_human_review`, `can_auto_merge_self_modification`, `revert_vote_threshold` keys |
| `app/web/kanban.js` + `style.css` | Self-modification banner, revert vote badge, integration branch indicator |
| `INSTRUCTIONS.md` | Setup guide for `_maestro_self`; capability warning |
| `app/tests/test_self_modification.py` | **New** — all tests for this gap |

---

## Implementation order

1. Phase 1 (config) → Phase 2 (allowlist file) → Phase 3 (path guard) → run existing tests → fix regressions
2. Phase 4 (migrations) → `/migrate` → Phase 5 (CRUD) → Phase 6 (tool)
3. Phase 7 (loop + API route) → Phase 8 (UI) → Phase 9 (new tests)
4. Update `app/database/__init__.py` to re-export new models and CRUD functions
5. Update `INSTRUCTIONS.md`

---

## Acceptance criteria

- [ ] A write to any path inside `D:/workspace/TheMaestro/` from a non-`_maestro_self` project raises `ValueError` unconditionally.
- [ ] A write to a path not on `ALLOWED_PATHS` from `_maestro_self` with `can_self_modify = true` raises `ValueError`.
- [ ] A write to any path in `HARD_BLOCKED` raises `ValueError` even with all toggles enabled.
- [ ] `vote_to_revert` accumulates votes across agents; at the threshold it calls `git revert` and creates a PIP card.
- [ ] Auto-merge only fires when both `can_auto_merge_human_review` and `can_auto_merge_self_modification` are true.
- [ ] Full test suite must pass before any merge to `maestro/self-improvement` is allowed.
- [ ] `maestro/self-improvement` is never automatically merged to `main` — only the human can do that.
- [ ] All new code passes existing test suite with no regressions.
