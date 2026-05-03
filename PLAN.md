# Plan: Tooling Sprint — Migration Script, MCP Orientation Tools, Frontend Docs

Three tasks in series. Check off each item as it ships.

---

## Task 1 — Migration Scaffolding Script

**Goal:** `python scripts/create_migration.py <name>` generates the next numbered migration file with correct boilerplate. No more manual numbering or copying.

### Steps
- [x] Read `app/migrations/versions/` to understand naming convention (NNNN_description.py)
- [x] Write `scripts/create_migration.py`:
  - Scan versions dir for highest NNNN prefix (zero-pad to 4 digits)
  - Slugify the name arg (spaces → underscores, lowercase)
  - Write `NNNN+1_<name>.py` with `up(conn)`, `down(conn)`, `description` stubs
  - Print the created path
- [x] Test: run it, verify the file looks right, verify numbering is correct
- [x] Update CLAUDE.md migration section to mention the script

---

## Task 2 — Three MCP Orientation Tools

**Goal:** Add `get_capacity_status`, `get_project_health`, and `list_pending_merges` to the MCP server so the first 30 seconds of any session are instant.

### Steps

**`get_capacity_status()`**
- [x] Read `mcp_tools/diagnostics.py` and `mcp_tools/helpers.py` to understand existing tool pattern
- [x] Query: all compute nodes → their LLMs → active session counts from `agent_sessions`
- [x] Format: per-node table with used/free/total slots and per-LLM breakdown
- [x] Register tool in MCP server

**`list_pending_merges(project=None)`**
- [x] Query: tasks where `type = 'completed'` and no merge record with non-null `merge_commit_sha`
- [x] Return: task_id, title, project, branch name (`maestro/task-{id}`), accepted_at
- [x] Optional `project` filter

**`get_project_health(project=None)`**
- [x] Stage distribution: count of active tasks per `type` for the project
- [x] Active sessions: tasks currently in `agent_sessions` with status='running'
- [x] Recent demotions: tasks with `demotion_count > 0` updated in last 24 h
- [x] Budget spend: sum of `total_cost_microcents` from `expenses` in last 7 days (for project's tasks)
- [x] Pending merges: count from `list_pending_merges` logic
- [x] Format as a compact human-readable summary
- [x] Register tool in MCP server

**Registration + docs**
- [x] Add all three tools to MCP server (check registration pattern in existing mcp_tools files)
- [x] Add entries to the MCP "when to use which tool" table in `CLAUDE.md`

---

## Task 3 — Update `app/web/CLAUDE.md` for Stage Journal Changes

**Goal:** Document the tabbed diff viewer, fullscreen toggle, and light-themed transitions we shipped, so the next frontend session doesn't start blind.

### Sections to add / update
- [x] In the `kanban.js` key patterns section: document `_parseDiffFiles()`, `_renderDiff()` (now returns tab HTML for multi-file), `_sjSelectDiffTab()`, `_sjToggleFullscreen()`
- [x] In the Stage Journal modal description: document tabbed diff UX, fullscreen button (`#sj-expand-btn`, `#sj-modal-inner`), `sj-fullscreen` CSS class
- [x] In the CSS quick-reference: add `.sj-diff-tabs`, `.sj-diff-tab`, `.sj-diff-panel`, `.sj-diff-panels`, `.sj-fullscreen`, `.sj-expand-btn`
- [x] In the transition section: document `.sj-txn-run--accepted/passed/rejected` color variant classes and light background change

---

## Done Conditions

- [x] `python scripts/create_migration.py test_migration` creates a correctly numbered file
- [x] All three MCP tools return results without error when called via Claude Code
- [x] `app/web/CLAUDE.md` accurately describes the current diff viewer and transition section behavior
- [x] CLAUDE.md MCP table updated with the three new tools
- [x] Everything committed

---

*Sprint complete.*
