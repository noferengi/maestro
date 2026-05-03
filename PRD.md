# TheMaestro — Product Requirements Document

> **Living document.** Edit in-place. Update status badges as work ships. Add new sections at the bottom of each theme; never reorder shipped items. Last substantive revision: 2026-05-03.

---

## Immediate Backlog

*Items identified as high-value and not yet planned elsewhere. Pick up from here before starting new themes.*

### B.1 Operational Runbook — `RUNBOOK.md`

`PLANNED` · Priority: **P1**

A lookup table of the ten most common stuck-card patterns: symptom → likely cause → fix command. The MCP `diagnose_task` surfaces the data; knowing what to do with it requires experience that currently lives only in the operator's head. Format: a short markdown table plus one paragraph per pattern. Target: any new session should be able to resolve any common failure in under 2 minutes without prior context.

Patterns to cover at minimum:
- `activity_status: idle` on a session marked running → zombie after server restart
- `finish_reason: length` on a planning budget entry → max_tokens too low for reasoning model
- Planning gate hard-fail "CREATE targets exist" → stale plan, file created since plan was written
- Task stuck in `subdividing` type → subdivision agent exited without writing children
- `rapid_cycling` monitor flag → PIP loop, resolution failing and re-demoting
- Gate soft-fail on interface completeness → fuzzy match missing, needs `patch_planning_fields`
- Research job stuck in `running` → server restarted mid-research, needs manual reset to `pending`
- `tool_call_storms` monitor flag → agent in a read loop, needs `stop_agent` + demotion
- Budget exhausted mid-run → task in error state, budget needs top-up or reallocation
- Orphaned worktree after crash → `prune_orphaned_worktrees` doesn't fire, manual `git worktree prune`

---

### B.2 `preview_dispatch()` MCP Tool

`PLANNED` · Priority: **P2**

Dry-run the scheduler tick without dispatching anything. Returns what *would* be dispatched: task ID, title, target LLM, estimated token cost, and why each other ready task was skipped (capacity, cooldown, PIP gate, etc.).

Useful for reasoning about ordering and capacity before flipping the scheduler on, and for debugging "why didn't my card dispatch?" without staring at logs.

Implementation: replicate the `_tick()` logic in read-only mode — same DAG resolution, same capacity checks, same cooldown filters — but instead of calling `_run_task()`, collect the decisions into a report. No DB writes. Lives in `mcp_tools/diagnostics.py`.

---

## Vision

**TheMaestro is an agentic software factory.** It takes human intent — expressed as IDEA cards on a Kanban board — and drives a fleet of locally-hosted LLMs through a closed-loop pipeline: Design → Implement → Test → Verify → Accept. The human sets direction and approves decisions. The machines do the work.

The north star: **a non-engineer with a clear idea and a good GPU should be able to ship production-quality software.** Not a prototype. Not a script. Software with tests, security review, a DAG of well-ordered changes, and a git history that documents every decision.

The secondary north star: **nothing gets destroyed.** Every action is reversible. No agent can corrupt the project, overwrite unrelated work, or bypass review. Safety is architectural, not advisory.

---

## How to Use This Document

- Status badges: `SHIPPED` · `IN PROGRESS` · `PLANNED` · `IDEA` · `DEFERRED`
- Items with no assignee are open for pickup.
- Specs for PLANNED items live in the **Feature Specifications** section at the bottom.
- When an item ships, mark it `SHIPPED`, add a one-line note on what was built, and do not remove it — the document is a record of decisions, not just a task list.

---

## Current State (as of 2026-05-02)

Everything listed here has working code in the repository.

### Pipeline

| Stage | What Happens | Status |
|---|---|---|
| IDEA | Human-authored card enters the queue | `SHIPPED` |
| Intake | Research agent resolves unknowns; LLM panel votes to accept or reject | `SHIPPED` |
| PLANNING | 5-stage pipeline: survey → best-of-5 design → review panel → pitfall detection → consolidation | `SHIPPED` |
| Planning Gate | 7 deterministic + 1 LLM check before plan is accepted | `SHIPPED` |
| INDEV | Component-level dev orchestrator; parallel batch execution; test-fix loop | `SHIPPED` |
| CONCEPTUAL_REVIEW | LLM panel reviews implementation quality | `SHIPPED` |
| OPTIMIZATION | LLM suggests and applies performance improvements | `SHIPPED` |
| SECURITY | Bandit + pip-audit + semgrep + LLM security reviewer | `SHIPPED` |
| FULL_REVIEW | Final LLM panel; gates on LIKELY/POSSIBLE majority | `SHIPPED` |
| COMPLETED | Task accepted; branch ready to merge | `SHIPPED` |

### Scheduler

- DAG-aware push scheduler; ticks every 5 s; topological sort for readiness `SHIPPED`
- Multi-level capacity enforcement: per-LLM session cap, per-compute-node model cap `SHIPPED`
- Budget pre-flight check (microcent tracking, worst-case cost estimate per dispatch) `SHIPPED`
- Cooldown throttling: 60 s on failure, 5 min on rejection, 5 min project-level `SHIPPED`
- One-LLM-at-a-time policy (llama.cpp architecture constraint) `SHIPPED`
- PIP resolution guard: blocks re-dispatch while remediation is in flight `SHIPPED`
- Planning session timeout: expires hung sessions, re-queues task `SHIPPED`
- Worktree cleanup on startup: prunes orphaned git worktrees from crashed runs `SHIPPED`

### Safety

- Worktree isolation: each task runs in `.maestro-worktrees/{task_id}/` on `maestro/task-{id}` branch `SHIPPED`
- Write-path containment: agents cannot write outside project root, venv, node_modules, .git, gitignored paths `SHIPPED`
- Archive-not-delete: `archive_file()` moves to `.archive/YYYY-MM-DD_HH-MM-SS/` — no hard deletes `SHIPPED`
- Shell blocklist: rejects rm -rf, format, dd, fork bombs, curl|bash, deep traversal `SHIPPED`
- Named shell tools: one tool per operation (run_pytest, run_mypy, run_ruff, etc.) — no raw shell for agents `SHIPPED`
- Consecutive error circuit breaker: 3 failures → REVERT_TO_DESIGN `SHIPPED`
- Git safety rails: agents cannot checkout non-maestro branches, cannot touch TheMaestro's own repo `SHIPPED`

### Observability

- Stage Journal modal: per-card view of every pipeline artifact (diffs, votes, gate checks, pitfalls, component results) `SHIPPED`
- Tabbed file diff viewer with fullscreen toggle `SHIPPED`
- Diagnostics viewer: every LLM call, prompt, response, tool use, token cost, context bar `SHIPPED`
- MCP server (`maestro` tool set): monitor, diagnose_task, get_budget_trace, find_stuck_tasks, etc. `SHIPPED`
- Budget tracking: per-task, per-session, per-LLM microcent spend with aggregated summaries `SHIPPED`

---

## Theme 1 — IDEA Card Authoring

*The quality of the output is gated by the quality of the input. Right now, a 10-word IDEA card and a 10-paragraph IDEA card go through the same pipeline. The machine does its best with what it has. This theme makes the input better.*

---

### 1.1 Intent Clarification — LLM-Assisted Description Rewrite

`PLANNED` · Priority: **P0** · Spec: [§FS-1.1](#fs-11-intent-clarification)

When a user saves a new IDEA card (or edits an existing one), the system triggers a one-shot LLM rewrite of the description. The user sees the original and the suggested version side-by-side and chooses to accept, edit, or discard the suggestion. Only after this approval step does the card become available for pipeline dispatch.

**Why this matters:** The planning pipeline's 5-stage design process, the gate checks, and the implementation agent all depend on the description as their source of truth. A vague description produces a vague plan. A plan that doesn't match the user's intent demotes and retries, burning tokens and time. Front-loading clarity is the highest-leverage improvement in the entire system.

**Title policy:** The LLM is instructed to preserve the original title verbatim. The user spent cognitive effort on that title; it's how they'll recognize the card on the board. The LLM *may* append a short clarifying subtitle in `[brackets]` if the title is ambiguous, but the original string must appear first, unchanged. If the user wants a retitle, they can do it manually.

---

### 1.2 Prerequisites UI — Visual DAG Wiring

`PLANNED` · Priority: **P1** · Spec: [§FS-1.2](#fs-12-prerequisites-ui)

No UI currently exists for setting `prerequisites`. The DAG is wired either by the subdivision agent (automatically) or not at all (human-created IDEA cards). This means human-authored work is dispatched without dependency ordering unless the user manually edits the DB.

Add a **prerequisite selector** to the task edit modal: a searchable multi-select showing all tasks in the current project. Below the selector, a mini DAG visualization (SVG, lightweight) shows the dependency chain so the user can see the effect of their selections before saving.

Also add drag-to-connect to the Column Map view: hovering over a node reveals connection handles; dragging from one node to another creates a prerequisite edge.

---

### 1.3 Acceptance Criteria Extraction

`PLANNED` · Priority: **P1** · Spec: [§FS-1.3](#fs-13-acceptance-criteria-extraction)

The Intent Clarification step (1.1) produces a rewritten description. This step goes further: it extracts a **structured acceptance criteria list** from the description and stores it as a separate field on the task record.

These criteria become:
- The seed for PIPs (Performance Improvement Plans) if the card is demoted
- The pass/fail checklist for the FULL_REVIEW stage
- Pre-hoc test stubs injected into the component loop agent's initial context

This closes the loop between what the user wanted and what the machine verifies.

---

### 1.4 Cross-Task Conflict Prediction at Card Creation

`IDEA` · Priority: **P2**

When a user creates or edits a card, run a lightweight analysis against currently active and pending cards:
1. Search file manifests of active INDEV/PLANNING tasks for overlap with likely files (inferred from description keywords)
2. If overlap found, surface a warning: *"Task #42 (currently in INDEV) is likely to modify `src/auth.py`. Consider making that task a prerequisite, or note the intended coordination."*
3. User can dismiss, add a prerequisite, or add a note to the description

This is advisory, not blocking — but it prevents the most common source of merge conflicts.

---

### 1.5 IDEA Card Templates

`IDEA` · Priority: **P3**

Certain task patterns recur: "add an API endpoint," "add a database migration," "add a UI feature," "write tests for module X." A template system stamps a group of pre-wired IDEA cards from a pattern, with prerequisites already set.

Templates are stored as JSON in the project (or globally). The "New Card" modal offers a "Use Template" option. The user fills in template variables (endpoint name, table name, etc.) and gets a ready-to-run subtree.

---

## Theme 2 — Cross-Task Coordination

*Multiple cards can be active simultaneously. Currently they are blind to each other's pending changes. This theme makes them aware.*

---

### 2.1 Pending-Change Awareness Injection

`PLANNED` · Priority: **P1** · Spec: [§FS-2.1](#fs-21-pending-change-awareness-injection)

**The gap:** Agent A is modifying `src/auth.py`. Agent B starts and also plans to modify `src/auth.py`. Neither knows about the other. Both will succeed. The conflict surfaces at merge time, not at run time.

**The fix:** At dispatch time, for each task being launched, query the DB for all tasks whose status is `completed` or `full_review` and which have a non-null `merge_commit_sha = NULL` (accepted but not yet merged). For each such task that has a file manifest overlapping with the current task's file manifest, inject a read-only summary into the agent's initial context:

```
⚠ PENDING UNMERGED CHANGES:
  Task #42 "Add OAuth middleware" (ACCEPTED, not yet merged)
  Modified files: src/auth.py, src/middleware.py, tests/test_auth.py
  To read the pending version of src/auth.py:
    read_git_show("maestro/task-42", "src/auth.py")
  Treat these as the effective current state of those files.
```

The agent already has `read_git_show()` with ref support — it can read any branch. No new tool needed. This is a ~30-line change to `loop.py` and a DB query helper.

---

### 2.2 File Claim Registry

`PLANNED` · Priority: **P2** · Spec: [§FS-2.2](#fs-22-file-claim-registry)

At planning-gate acceptance time, register all files in the task's `file_manifest` as claimed by that task in a `file_claims` DB table (`task_id`, `file_path`, `claimed_at`, `released_at`). Claims are released when the task is merged or demoted.

At dispatch time, the scheduler checks the claim registry:
- If a ready task's manifest overlaps with an active claim, inject a coordination warning (as in 2.1) — but do not block dispatch
- If the overlap is with a task that is currently INDEV (not just accepted), flag it as a soft conflict and suggest deferring

This is advisory. The DAG prerequisite system is the hard enforcement mechanism. File claims are the early-warning system.

---

### 2.3 Merge-Readiness Queue

`IDEA` · Priority: **P2**

Add a visual "merge queue" section to the board: all COMPLETED tasks whose branches have not yet been merged to main, sorted by acceptance time. Each entry shows: task title, branch name, files changed, merge conflicts (detected via `git merge-tree --write-tree` dry-run), and a "Merge" button.

When a task is merged, release its file claims (2.2) and update sibling tasks' pending-change injections.

---

### 2.4 Automatic Prerequisite Suggestion from File Overlap

`IDEA` · Priority: **P3**

When a card moves into PLANNING and the planning gate produces a `file_manifest`, automatically suggest adding any currently-active tasks that share files as prerequisites. Present as a toast: *"Task #42 is modifying 2 of the same files. Add as prerequisite?"*

---

## Theme 3 — Planning Quality

*The planning pipeline is the most important stage. A good plan makes everything downstream faster, cheaper, and more accurate.*

---

### 3.1 Spec Constraint Hardening

`SHIPPED` — Planning gate extracts binding constraints from description and injects them into design prompts. LLM feasibility check verifies implementation steps don't violate constraints.

---

### 3.2 Incremental Planning Cache

`PLANNED` · Priority: **P2**

When a card is demoted from INDEV back to PLANNING, the codebase survey (20 agent turns) and most of the design work are still valid. Only the part that caused the failure needs rethinking.

Cache the planning survey result keyed on `(project_file_tree_hash, task_description_hash)`. On re-plan, if the cache hit is valid (project files haven't changed significantly), skip the survey phase and use cached context. Estimated savings: 30–50% of re-planning token cost.

---

### 3.3 Test Stub Generation from Acceptance Criteria

`PLANNED` · Priority: **P2** · Depends on: [1.3](#13-acceptance-criteria-extraction)

Once acceptance criteria are extracted (1.3), generate test stub files before the dev loop starts. The component loop agent receives concrete tests to satisfy rather than inferring them from the description. This is the TDD model applied to the agentic pipeline.

The planning agent writes stub test files to the task's worktree as part of plan consolidation. The dev agent sees red tests on day 1 and must make them green. No more honor-system "please write tests."

---

### 3.4 Architecture Constraint Awareness in Subdivision

`SHIPPED` — Subdivision agent receives architecture card context filtered by `ARCH_CATEGORY_RELEVANCE['subdivision']`.

---

### 3.5 Adaptive Reviewer Panel Size

`IDEA` · Priority: **P3**

Currently the design review panel is hardcoded at 5 reviewers (with simple tasks skipping 2). Instead, scale the panel based on:
- Estimated complexity (file count, step count, number of external dependencies)
- Risk profile (security-sensitive files, database migrations, public API changes)
- Available LLM capacity

High-risk changes get 7 reviewers. Trivial changes (single-file test additions) get 2. This reduces token waste on simple tasks and increases scrutiny on dangerous ones.

---

## Theme 4 — Development Quality

*The dev loop is where code gets written. This theme makes the code better and the verification more trustworthy.*

---

### 4.1 Pre-hoc Test Stubs

`PLANNED` · Priority: **P2** · See [3.3](#33-test-stub-generation-from-acceptance-criteria)

---

### 4.2 Test Evidence in Stage Journal

`IN PROGRESS` · Priority: **P1**

The Stage Journal currently shows component status (done/failed/running) but not test output. A developer reviewing an accepted card cannot see which tests passed, what coverage was achieved, or what the pytest output was.

Add a **Tests** section to the Stage Journal that shows:
- Test run summary (pass/fail count, coverage %)
- Failed test names and truncated output (if any)
- Files with zero coverage (if coverage data available)
- Whether the test-fix loop was triggered, and how many attempts it took

This requires storing test output in the `component_results` table (currently only stores `status`, `turns_used`, `files_changed`). Add `test_output` (text, nullable) and `coverage_pct` (float, nullable) columns via migration.

---

### 4.3 Mandatory Test Pass Gate

`PLANNED` · Priority: **P1**

The loop currently relies on the agent's initiative to run tests before submitting. An agent under context pressure may skip this. Add a hard gate: the dev orchestrator verifies `git status` is clean and the last recorded test run returned exit code 0 before accepting a component as done. If tests have not been run since the last write, force a test run.

---

### 4.4 Component Retry on Partial Failure

`SHIPPED` — Test-fix loop retries 3× with 20-turn budget. `INDEV_TEST_FIX_MAX_RETRIES = 3`.

---

### 4.5 Cross-Component Interface Verification

`IDEA` · Priority: **P3**

After all components in a batch complete, run an automated interface contract check: verify that each component's provided interfaces match what consuming components expect, using the `interface_contracts` from the planning result as the source of truth. Flag mismatches before the review stage.

---

## Theme 5 — Observability & Transparency

*The system should be legible. A human reviewing the board should be able to understand exactly why any card is in its current state, what the agent did, and what evidence supports acceptance.*

---

### 5.1 Stage Journal Improvements — Tabbed Diff, Fullscreen, Light Transitions

`SHIPPED` — Tabbed per-file diff viewer, fullscreen toggle, light-themed transition cards with colored left-border per outcome, 600-char vote justifications.

---

### 5.2 Test Evidence Section in Stage Journal

`PLANNED` · Priority: **P1** · See [4.2](#42-test-evidence-in-stage-journal)

---

### 5.3 Timeline View

`IDEA` · Priority: **P2**

A horizontal timeline for each card showing every pipeline stage transition with timestamps, duration, and the decision that caused the transition (vote outcome, gate check result, user action). Makes it easy to see "this card spent 4 hours in planning because the gate failed twice on interface completeness."

---

### 5.4 Project Velocity Dashboard

`IDEA` · Priority: **P3**

A project-level stats page showing:
- Cards per stage (current snapshot)
- Average time per stage over last 30 days
- Token cost by stage and by LLM
- Demotion rate by stage (cards that were rejected at each gate)
- Test pass rate on first attempt vs. after fix loop

This is the operational heartbeat of the factory.

---

### 5.5 Archive Browser in UI

`PLANNED` · Priority: **P2**

The `.archive/` directory is the soft-delete safety net. Currently it's invisible in the UI. Add a panel (accessible from the project settings or a toolbar button) that lists archived files with timestamps, the task that archived them, and a one-click restore. Makes the safety guarantee visible and actionable.

---

## Theme 6 — Multi-LLM & Scale

*Today the system runs one LLM at a time on one machine. This theme opens the ceiling.*

---

### 6.1 Multi-LLM Load Balancing

`PLANNED` · Priority: **P2** · Spec: [§FS-6.1](#fs-61-multi-llm-load-balancing)

The scheduler's capacity model already tracks multiple LLMs and compute nodes. The one-LLM-at-a-time policy is a llama.cpp constraint, not an architectural one. When a stateless inference server (vLLM, TGI, Ollama parallel mode) is configured, the scheduler should dispatch to the least-loaded available LLM simultaneously rather than serializing.

Expected throughput improvement: roughly linear with the number of LLM slots, up to the DAG's parallelism ceiling.

---

### 6.2 Compute Node Management

`SHIPPED` — Compute nodes table, per-node capacity config, node-aware dispatch.

---

### 6.3 Model Card System

`PLANNED` · Priority: **P2**

LLM endpoints currently have: name, base_url, model, max_context, parallel_sessions. Add:
- `strengths`: array of tags (`["coding", "reasoning", "security"]`)
- `cost_per_1k_tokens`: for budget reporting
- `supports_tool_calls`: boolean (some smaller models can't reliably use JSON tool schemas)
- `preferred_stages`: which pipeline stages this model should be routed to

The scheduler uses `preferred_stages` to route: planning to the strongest reasoning model, security review to the model tagged "security," component dev to the fastest coding model. Better resource allocation without manual per-task LLM assignment.

---

### 6.4 Remote Inference Support

`IDEA` · Priority: **P3**

The system currently assumes local inference. For tasks that benefit from a stronger model, allow routing to a remote OpenAI-compatible API (Claude API, OpenAI, Groq, etc.) with:
- Per-project budget cap (don't accidentally spend $100 on one card)
- Fallback to local if remote is unavailable
- Clear labeling in the Stage Journal when a remote model was used

---

## Theme 7 — Safety Hardening

*The current safety model is strong. This theme closes the remaining gaps.*

---

### 7.1 Shell Allowlist

`PLANNED` · Priority: **P2**

Replace the shell blocklist with a strict allowlist. The current blocklist catches known-bad patterns but misses creative destruction (semicolon splitting, environment variable expansion, etc.). An allowlist is the only way to be truly safe.

Named shell tools (run_pytest, run_mypy, etc.) are already the primary mechanism. The generic `run_shell` command is now only available to specific pipeline stages that need it. Long-term goal: eliminate `run_shell` entirely from agent-accessible tools.

---

### 7.2 Write Journaling

`PLANNED` · Priority: **P2**

Before every `write_file()` call, snapshot the previous file content to `.journal/{task_id}/{timestamp}/{path}`. This is a git-bisect-style undo trail that survives even if the agent corrupts its branch. Journal files are append-only and live outside the agent's writable path.

Different from `.archive/`: archive is for deliberate deletes. Journal is for overwrites — the file still exists but its previous content is preserved.

---

### 7.3 Pre-Run Snapshot Tags

`PLANNED` · Priority: **P2**

Before launching any agent loop, create a lightweight git tag `maestro/pre-run/{task_id}/{timestamp}` on the current HEAD of the task's worktree branch. If the run goes sideways, `git reset --hard` to this tag restores everything without worrying about individual file recovery.

---

### 7.4 Auto-Push on Accept

`PLANNED` · Priority: **P2**

When a task reaches ACCEPTED, automatically `git push origin maestro/task-{id}`. The remote is the last line of defense. If the local machine dies between acceptance and merge, the work survives. Requires a configured remote (skipped gracefully if none).

---

### 7.5 Archive Restore Tool

`PLANNED` · Priority: **P3**

Add a `restore_file(archive_path, target_path=None)` tool that reverses `archive_file()`. Available to agents and to the UI (Archive Browser, 5.5). Currently recovery requires manual file surgery.

---

## Theme 8 — IDEA Card to Merge — Full Flow Tightening

*End-to-end UX improvements that make the system feel like a product, not a prototype.*

---

### 8.1 Card Status Indicators

`SHIPPED` — Stage footer badges on each card show pipeline substage, gate result, component counts, session activity.

---

### 8.2 One-Click Pipeline Triggers

`SHIPPED` — Run Planning, Run Review, Run Security, Run Full Review buttons on cards.

---

### 8.3 Merge Button on COMPLETED Cards

`PLANNED` · Priority: **P1**

COMPLETED cards have an accepted branch. The only remaining step is merging to main. Add a "Merge to main" button on completed cards that:
1. Runs `git merge --no-ff maestro/task-{id}` (no fast-forward so the branch boundary is visible in history)
2. Updates `merge_commit_sha` on the task record
3. Releases file claims (2.2)
4. Optionally deletes the task's worktree and branch
5. Shows a diff stat of what was merged

---

### 8.4 Demotion Reason UI

`SHIPPED` — Demotion history visible in Stage Journal transition section with colored left-border per outcome.

---

### 8.5 Card Clone & Reattempt

`SHIPPED` — Clone button creates duplicate IDEA in same project.

---

### 8.6 Bulk Operations

`IDEA` · Priority: **P3**

Checkboxes on cards to select multiple; bulk operations: move to stage, assign LLM, set budget, archive. Useful when cleaning up a stalled project or reassigning work after a model change.

---

## Feature Specifications

*Detailed specs for PLANNED items. When a feature ships, mark the spec SHIPPED and note what diverged.*

---

### FS-1.1 Intent Clarification

**Status:** `PLANNED`

**Trigger:** User clicks "Save" on a new IDEA card, or clicks "Clarify" on an existing one.

**Flow:**

1. Modal shows a loading state ("Thinking about your idea…")
2. System makes a single LLM call (no tools, no turns — just one generation) with the prompt:

   ```
   You are helping a software engineer clarify a task description before it enters
   an automated development pipeline. The pipeline works best when descriptions are:
   - Specific about what success looks like (acceptance criteria)
   - Clear about what is explicitly NOT included (scope boundaries)
   - Explicit about any technical constraints (language, framework, approach)
   - Structured so an LLM can extract a file manifest from it

   The user's original title: {title}
   The user's original description: {description}

   Rewrite the description to be clearer and more complete. Structure it as:

   **Goal:** [one sentence — what this card accomplishes]

   **Acceptance Criteria:**
   - [specific, testable condition]
   - [specific, testable condition]
   ...

   **Out of Scope:** [what this card explicitly does NOT do]

   **Constraints:** [any technical constraints, or "None"]

   Rules:
   - Do NOT change the title. If the title needs disambiguation, append [clarification]
     after the original title, but reproduce the original verbatim first.
   - Do NOT invent requirements. Only make explicit what is implicit in the description.
   - If the description is already clear and specific, you may return it unchanged.
   - Keep the rewrite concise. Do not pad.
   ```

3. Modal shows the original and the rewritten description side by side, or stacked on mobile.
4. User has three actions:
   - **Accept** — saves the rewritten description to the task record
   - **Edit** — opens the rewritten description in an editable textarea; user tweaks and saves
   - **Keep Original** — discards the suggestion, saves the original description
5. Optionally: a "Suggest a title" link (collapsed by default) that reveals a title suggestion. The original title is pre-filled and the suggestion is shown as a diff. The user chooses which to keep. Title changes do not auto-apply.

**Card enters the dispatch queue only after this step completes.** If the user closes the modal without choosing, the card is saved as a draft (not dispatchable) with a badge "Needs clarification."

**Model selection:** Uses the project's default LLM, or the cheapest/fastest configured LLM tagged for `intake` stage. One call, ~500 tokens. This is cheap enough to always run.

**Fallback:** If the LLM call fails, the user is shown the original description with a "Clarification failed — save anyway?" prompt. Always recoverable.

**New field required:** `description_original` (text, nullable) — stores the user's raw input before clarification. Allows diffing "what the user typed" vs "what the pipeline saw" if a card goes wrong. Migration required.

**New field required:** `clarification_status` (text, default `'none'`) — values: `none` (no clarification run), `pending` (clarification triggered, not yet approved), `approved` (rewrite accepted), `kept_original` (user kept original), `skipped` (user bypassed). Used by the scheduler to gate dispatch.

---

### FS-1.2 Prerequisites UI

**Status:** `PLANNED`

**In create/edit modal:**
- New "Prerequisites" section below description
- Searchable multi-select: type to filter tasks by title or ID; shows pipeline stage badge next to each result
- Selected prerequisites shown as chips with a remove button
- Below the chips: a static SVG mini-graph (4-column compact layout) showing the dependency chain — what the selected tasks depend on, and what currently depends on them
- Warning displayed if a circular dependency would be created (client-side check: walk the existing prerequisite graph)

**In Column Map:**
- Hover a node → show 4 connection handles (N/S/E/W)
- Drag from a handle to another node → creates prerequisite edge (target ← source)
- Drag from a handle to empty space → cancels
- Right-click an existing arrow → context menu with "Remove prerequisite"
- Changes persist immediately via `PUT /api/tasks/{id}` with updated `prerequisites` array

---

### FS-1.3 Acceptance Criteria Extraction

**Status:** `PLANNED`

**When:** During the Intent Clarification step (FS-1.1), after the user approves the rewrite.

**How:** Parse the approved description for the `**Acceptance Criteria:**` section (produced by the rewrite prompt). Store as a JSON array of strings in a new `acceptance_criteria` column on the task record.

**If the description was not rewritten** (user kept original, or no clarification ran): run a second lightweight LLM call that only extracts acceptance criteria from the raw description. This call is fire-and-forget; it doesn't block the UI.

**Downstream use:**
1. Planning gate: inject acceptance criteria as required behaviors the plan must address
2. Full review stage: reviewers are given the criteria and asked to verify each one is met
3. Stage Journal: "Acceptance Criteria" section shows each criterion with a pass/fail badge populated from the full review votes

---

### FS-2.1 Pending-Change Awareness Injection

**Status:** `PLANNED`

**Where:** `app/agent/loop.py`, in `_build_initial_context()` (or equivalent init function).

**Query:** At dispatch time, before building the initial message:

```python
def _get_pending_sibling_changes(task_id: str, project_id: int, file_manifest: list[str]) -> list[dict]:
    """
    Returns accepted-but-unmerged sibling tasks whose file manifests overlap
    with the current task's file manifest.
    """
    # Query: tasks in same project, type in (completed, full_review),
    #        merge_commit_sha IS NULL, id != task_id
    # For each: check if file_manifest overlap with current task's manifest
    # Return: [{task_id, title, branch, overlapping_files}]
```

**Injection format** (added to the initial system message):

```
⚠ PENDING UNMERGED CHANGES IN THIS PROJECT:
The following tasks have been accepted but not yet merged to main.
Their changes are on separate branches. Treat them as the effective
current state of those files — do not overwrite their work.

• Task #42 "Add OAuth middleware" (branch: maestro/task-42)
  Pending modifications: src/auth.py, src/middleware.py, tests/test_auth.py
  → read_git_show("maestro/task-42", "src/auth.py") to read the pending version

• Task #38 "Refactor user model" (branch: maestro/task-38)
  Pending modifications: app/models/user.py, app/database/migrations/0052_user_refactor.py
  → read_git_show("maestro/task-38", "app/models/user.py") to read the pending version

If you modify any of these files, coordinate with the pending changes above.
```

**No new tools needed.** `read_git_show` already accepts arbitrary refs.

**Gating:** Only inject if overlapping files are found. Zero-overhead for tasks with no sibling conflicts.

---

### FS-2.2 File Claim Registry

**Status:** `PLANNED`

**New table: `file_claims`**

```sql
CREATE TABLE file_claims (
    id          INTEGER PRIMARY KEY,
    task_id     TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    claimed_at  TEXT NOT NULL,  -- ISO timestamp
    released_at TEXT,           -- NULL = still active
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);
CREATE INDEX idx_file_claims_active ON file_claims(task_id, released_at)
    WHERE released_at IS NULL;
```

**Claim creation:** When a planning result is gate-approved, insert one row per file in `file_manifest`. Done in `planning_gate.py` after all checks pass.

**Claim release:** When a task is merged (merge_commit_sha set) or demoted below INDEV (plan invalidated), delete or set `released_at` on all active claims for that task.

**Scheduler integration:** In the readiness check, add a soft-conflict warning (not a hard block) when a ready task's manifest overlaps with active claims. Log the warning; inject via FS-2.1 mechanism if the task is dispatched.

---

### FS-6.1 Multi-LLM Load Balancing

**Status:** `PLANNED`

**Precondition:** A stateless inference backend configured that can handle concurrent requests (vLLM, TGI, Ollama with `--parallel` flag, or multiple llama.cpp instances on different ports).

**Scheduler change:** Replace the one-LLM-at-a-time gate with a routing policy:

```python
def _select_llm_for_task(task, available_llms):
    """
    Returns the best available LLM for this task, or None if all are at capacity.
    Priority: task.llm_id > project.llm_id > stage-preferred LLM > least-loaded LLM
    """
```

The existing per-LLM `parallel_sessions` cap and per-node `max_parallel_sessions` cap are already enforced. The only change is removing the "one model loaded at a time" serialization that was forced by llama.cpp's single-model limitation.

**No DB schema changes required.** The LLM table already has everything needed.

---

## Architecture Constraints

*Known limits that shape every decision. Update this section when a constraint is lifted.*

| Constraint | Root Cause | Workaround | Resolution Path |
|---|---|---|---|
| One LLM active at a time | llama.cpp loads one model into VRAM | Configure multiple llama.cpp instances on separate ports | Switch to vLLM or Ollama parallel mode (FS-6.1) |
| SQLite single-writer | SQLite WAL mode, no replication | All DB writes go through the FastAPI server (single process) | Acceptable until >10 concurrent agents; Postgres migration path exists |
| Local inference only | No remote API integration | Manual per-task LLM override | Theme 6 remote inference (6.4) |
| No cross-project isolation | File tools restricted by path only | Agents stay on their project root by convention | Capability-based security model (future) |
| Worktree branch not stored in DB | Branch derived from task_id at runtime | Always `maestro/task-{id}` | Not a problem; keep derived |

---

## Success Metrics

*How we know the system is getting better, not just bigger.*

| Metric | Current Baseline | Target |
|---|---|---|
| Cards accepted on first INDEV attempt (no demotion) | ~40% (estimated) | >65% |
| Average planning gate passes on first attempt | ~60% (estimated) | >80% |
| Token cost per accepted card | Unknown (not tracked) | <500K tokens for medium complexity |
| Time from IDEA → COMPLETED (calendar time) | ~2-4 hours | <90 min for medium complexity |
| Merge conflicts on branch merge | Unknown | Zero (via file claims + pending awareness) |
| User-edited clarification rate (1.1) | N/A (not built) | >40% (cards with at least one edit) |
| Stage Journal open rate per accepted card | Unknown | >70% (humans reviewing machine work) |

---

## Decision Log

*Decisions made, and why. Reference this before reopening closed questions.*

| Date | Decision | Rationale |
|---|---|---|
| 2026-03 | Named shell tools replace generic `run_shell` for agents | Blocklist approach misses creative destruction; per-operation tools are the only safe design |
| 2026-03 | Worktree isolation per task (not per session) | Prevents agent crashes from corrupting the main working tree; enables parallel tasks |
| 2026-03 | One-LLM-at-a-time as initial policy | llama.cpp constraint; scheduler model already supports multi-LLM for when the constraint is lifted |
| 2026-04 | Planning gate is deterministic + 1 LLM call (not all-LLM) | Deterministic checks (interface completeness, circular deps) are cheaper and more reliable than LLM checks for structural properties |
| 2026-04 | PIPs are post-hoc (generated on demotion) not pre-hoc | Acceptance criteria extraction (1.3) will migrate these to pre-hoc; PIPs then become the enforcement mechanism for pre-stated criteria |
| 2026-05 | Title preservation is policy in Intent Clarification (1.1) | Users use titles for identification; silent retitles break mental models; suggestions are opt-in |
| 2026-05 | Pending-change awareness is injection, not locking | Locking creates deadlocks; injection + agent reasoning is more robust and already has the tool infrastructure (`read_git_show`) |
