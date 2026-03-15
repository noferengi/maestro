# **PRD - The Maestro Task List**

This document outlines the implementation requirements for **The Maestro**, an agentic orchestration system that drives a locally-hosted LLM through sandboxed Design → Implement → Test → Verify cycles. The core promise: **a dumb coding agent can run in a loop, and one bad run can never destroy the project or poison future runs.**

Every item below is grounded in the current codebase state as of 2026-03-14. Items marked `[x]` have working code. Items marked `[~]` have partial/scaffolded code. Items marked `[ ]` are not yet started.

---

## **1. The Sandbox — Nothing Gets Destroyed**

*The #1 priority. This is why the project exists. Qwen CLI deleted an entire project because it had raw shell access. That can never happen here.*

### 1.1 Tool-Only Execution (No Raw Shell for Agents)

- [x] **Tool Registry:** 16 tools with OpenAI JSON schemas in `app/agent/tools.py`. All agent actions go through `dispatch_tool()`.
- [x] **Shell Blocklist:** `run_shell()` rejects `rm -rf`, `del /s`, `format`, `dd`, fork bombs, `curl|bash`, deep `../` traversal, and 15+ other destructive patterns via compiled regex.
- [x] **Path Containment:** `_assert_safe_path()` resolves every path and rejects anything outside `PROJECT_ROOT`.
- [ ] **Allowlist Mode for Shell:** Replace the blocklist with a strict allowlist. Only permit: `python -m pytest`, `python -m pylint`, `git` (read-only subcommands), `pip list`, `cat`, `head`, `wc`. Everything else is rejected by default. The blocklist catches known bad patterns but misses creative destruction — an allowlist is the only way to be truly safe.
- [ ] **No `shell=True`:** Refactor `run_shell()` to use `subprocess.run(shlex.split(command))` instead of `shell=True`. Shell injection via semicolons, pipes, or backticks is currently possible despite the blocklist (e.g., `echo hello; rm -rf /` where the `rm` portion doesn't match the pattern because of the semicolon splitting). Splitting the command first and running without a shell eliminates this entire class of attack.
- [ ] **Filesystem Write Journaling:** Before every `write_file()` call, snapshot the file's current content to `.journal/<timestamp>/<path>` (similar to `.archive/` but for overwrites, not deletes). This creates a git-bisect-style undo trail that survives even if the agent corrupts git history on its branch.

### 1.2 Soft-Delete / Archive System

- [x] **`archive_file()`:** Moves files to `.archive/YYYY-MM-DD_HH-MM-SS/<relative_path>` with an optional `_reason.txt` sidecar. Never calls `os.remove`, `shutil.rmtree`, or any hard-delete primitive.
- [x] **Agent System Prompt:** Instructs the agent to use `archive_file` instead of any delete command.
- [ ] **Archive Restore Tool:** Add a `restore_file(archive_path)` tool so the agent (or the user via the UI) can undo an archive without manual file surgery.
- [ ] **Archive Browser in UI:** A panel in the board that lists archived files with timestamps and reasons, with a one-click restore button.

### 1.3 Git Isolation

- [x] **Branch Enforcement:** `git_create_branch()` requires the `maestro/task-` prefix. `git_checkout()` blocks anything that isn't `maestro/*`, `main`, or `master`.
- [x] **Auto-Staging:** `write_file()` and `append_file()` call `git add` after every write.
- [ ] **Worktree Isolation:** Run each agent loop in a `git worktree` so the agent operates on a physically separate copy of the repo. If the agent corrupts its worktree, the main working tree is untouched. On success, merge the worktree branch back to main. On failure, delete the worktree — no cleanup needed.
- [ ] **Force-Push Block:** Add `--force`, `-f` (in push context), and `push.*main` to the shell blocklist. The agent must never be able to rewrite shared history.
- [ ] **Auto-Push on Accept:** When a task reaches ACCEPTED, automatically `git push origin maestro/task-{id}` so the branch exists on the remote even if the local machine dies. Requires a configured remote.
- [ ] **Pre-Run Snapshot:** Before launching any agent loop, create a lightweight tag `maestro/pre-run/{task_id}/{timestamp}` on the current HEAD. This is the "last known good" state. If the run goes sideways, `git reset --hard` to this tag restores everything.

---

## **2. The Loop Engine — Run a Dumb Agent Safely**

*The Wiggum Loop exists and works. The gaps are in run isolation, context management, and connecting the loop to the board UI.*

### 2.1 Core Loop (The Wiggum)

- [x] **MaestroLoop Class:** Async Do-While in `app/agent/loop.py`. Drives the LLM→tool-call→result→LLM cycle.
- [x] **Turn Cap:** `MAX_TURNS=150` in config. Terminates runaway loops.
- [x] **Consecutive Error Circuit Breaker:** After 3 consecutive tool errors, emits `REVERT_TO_DESIGN` and halts.
- [x] **Terminal Signals:** Agent emits `{"signal": "ACCEPTED"}` or `{"signal": "REVERT_TO_DESIGN"}` as structured JSON to end the loop.
- [x] **Status Registry:** `_ACTIVE_LOOPS` and `_LOOP_STATUS` dicts power the `/api/agent/status/{task_id}` endpoint.
- [ ] **Run Isolation — One Loop, One World:** Each `MaestroLoop` run should:
  1. Create a git worktree for its branch.
  2. Set `PROJECT_ROOT` for that run to the worktree path.
  3. Run all tool calls against the worktree, not the main repo.
  4. On ACCEPTED: merge worktree branch → main, push, delete worktree.
  5. On REVERT/ERROR/MAX_TURNS: delete worktree. Main repo is untouched.
  This ensures one bad run cannot corrupt the state that the next run starts from.

### 2.2 Context Window Management

- [x] **Turn Cap:** Prevents unbounded context growth.
- [x] **System Prompt Discipline:** Tells the agent not to re-read files already in context, to summarize before large operations, and to bail early if approaching the turn limit.
- [ ] **Message Compression:** When the conversation exceeds 80% of the model's context window (estimated by token count), compress older tool results to summaries. Keep the system prompt, the last 10 messages, and all tool call names/results-as-one-liners. Discard full file contents from early reads.
- [ ] **Sliding Window with Checkpoints:** Every 25 turns, write a checkpoint summary to the task history (`append_task_history`). If context must be truncated, the agent can re-orient from the last checkpoint instead of re-reading everything.

### 2.3 Multi-Model Support

- [x] **Config-Driven Endpoint:** `LLM_BASE_URL` and `LLM_MODEL` are environment variables. Can point to any OpenAI-compatible server.
- [ ] **Per-Task Model Override:** Add an `llm` field to the task schema (JSON: `{endpoint, model, temperature}`). When set, the loop uses that model instead of the global default. This lets you run small tasks on a fast 9B model and hard tasks on an 80B model.
- [ ] **Model Roster in UI:** A settings panel listing configured LLM endpoints (localhost:8008, localhost:8009, etc.) with model name, context window size, and a "test connection" button. Tasks can be assigned to a model from a dropdown.

---

## **3. The Board — Wire the Loop to the UI**

*The Kanban board is functional for manual task management. The agent backend is scaffolded but not yet controllable from the UI.*

### 3.1 Existing Board Features

- [x] Five columns: ARCHITECTURE, PLANNING, DEVELOPMENT, REVIEW, COMPLETED.
- [x] Per-project task isolation with project tabs.
- [x] Task CRUD: create, edit, delete, move between columns.
- [x] Drag-and-drop reorder within columns (ghost placeholder UX).
- [x] Auto-refresh polling (5-second interval).
- [x] Proof-of-work timeline (task history view).
- [x] Database-backed persistence (SQLite + SQLAlchemy + custom migration runner).

### 3.2 Agent Controls in the Board

- [~] **API Endpoints Exist:** `POST /api/agent/run/{task_id}`, `GET /api/agent/status/{task_id}`, `POST /api/agent/stop/{task_id}`, `GET /api/agent/tasks/ready`. All wired in `app/main.py`.
- [ ] **"Run with Maestro" Button:** Add a button to each PLANNING/DEVELOPMENT card that calls `POST /api/agent/run/{task_id}`. Show a spinner while the loop is active.
- [ ] **Live Status Panel:** When an agent loop is running, show a collapsible panel at the bottom of the board with: current turn count, git branch, last tool call name, last tool result (truncated), and a STOP button.
- [ ] **Agent Log Stream:** WebSocket or SSE endpoint that streams tool call events in real-time. The status panel subscribes to this stream. Much better than polling for UX.
- [ ] **REVERT_TO_DESIGN Reaction:** When the loop emits REVERT, automatically move the task back to PLANNING and display the `advice` field in a toast notification so the user knows what went wrong.
- [ ] **ACCEPTED Reaction:** When the loop emits ACCEPTED, move the task to COMPLETED, display the summary, and show the git branch name with a "merge to main" button.

### 3.3 Board Improvements

- [ ] **Prerequisites Editor:** The `prerequisites` column exists in the DB schema. Add a multi-select in the task edit modal that lets users pick prerequisite task IDs. The DAGResolver already computes readiness from this field.
- [ ] **DAG Visualization:** A simple directed graph view (vis.js or d3-dag) showing task dependency arrows. Clicking a node opens the task. Ready tasks glow green.
- [ ] **Rename / Delete Projects:** Tab context menu with rename and delete (soft-delete — archive all tasks, don't drop them).
- [ ] **Cross-Column Drag:** Allow dragging cards between columns (not just reordering within a column). The reorder API already accepts a `type` field for column changes.

---

## **4. Safety Layers — Defense in Depth**

*The lesson from the Qwen incident: one layer of protection is not enough. The system needs multiple independent safety layers so that if any one fails, the others still hold.*

### Layer 1: Tool-Level Constraints (exists)
- [x] Path containment, shell blocklist, archive-not-delete, branch enforcement.
- [ ] Upgrade to shell allowlist (see 1.1).
- [ ] Upgrade to no `shell=True` (see 1.1).

### Layer 2: Git-Level Isolation (partially exists)
- [x] Agent works on `maestro/task-*` branches only.
- [ ] Worktree isolation (see 1.3).
- [ ] Pre-run snapshot tags (see 1.3).
- [ ] Auto-push on accept (see 1.3).

### Layer 3: Loop-Level Circuit Breakers (exists)
- [x] 3 consecutive errors → REVERT_TO_DESIGN.
- [x] 150-turn hard cap.
- [x] External stop via `POST /api/agent/stop/{task_id}`.

### Layer 4: Write Journaling (not started)
- [ ] Every `write_file` snapshots the previous content before overwriting.
- [ ] Every `archive_file` records what was moved and why.
- [ ] Journal is append-only and lives outside the agent's writable path.

### Layer 5: Remote Persistence (not started)
- [ ] Auto-push branches to remote on ACCEPTED.
- [ ] Periodic push of `.journal/` and `.archive/` to a separate backup remote or branch.
- [ ] The remote is the last line of defense — even if the local machine is wiped, the work survives.

---

## **5. Dual-Artifact System — Design Controls Code**

*Markdown is the ground truth. Code is derived from it. The agent cannot invent requirements.*

### 5.1 Blueprint Layer

- [x] **ARCHITECTURE.md:** Global system design (exists, manually maintained).
- [~] **AGENTS.md:** Folder-level design spec (file exists, schema not standardized).
- [ ] **AGENTS.md Schema:** Define a required structure: `## Purpose`, `## Inputs`, `## Outputs`, `## Constraints`, `## Acceptance Criteria`. The planning agent writes these; the coding agent reads them.
- [ ] **Design Validator Gate:** Before a task moves from PLANNING to DEVELOPMENT, an LLM call checks: "Does the AGENTS.md for this task's scope contain enough detail to implement?" If not, the task stays in PLANNING with feedback.

### 5.2 Implementation Guardrails

- [x] **System Prompt Rule S4:** Agent cannot modify `.md` design files unless the task type is `planning` or `architecture`.
- [ ] **CodeSync Monitor:** After a task is ACCEPTED, diff the implementation against the relevant AGENTS.md. Flag any files that were changed but not mentioned in the design doc.
- [ ] **Post-Merge Design Update:** After merging a completed task branch, auto-generate a summary of what changed and append it to AGENTS.md in the affected directories.

---

## **6. Verification & Checkpointing**

### 6.1 Commit Discipline

- [x] **Auto-stage on write:** `write_file()` calls `git add` after every write.
- [x] **`git_commit` tool:** Agent can commit with a message.
- [ ] **Commit Gate:** The loop should verify that `git status` is clean before emitting ACCEPTED. No uncommitted changes allowed in the final state.
- [ ] **Commit Message Enforcement:** Reject commit messages that don't match the format `feat(task-{id}): <what> — <why>` or `fix(task-{id}): ...`.

### 6.2 Test Verification

- [x] **`run_shell` for pytest:** Agent can run `python -m pytest -x -q`.
- [ ] **Mandatory Test Pass:** The loop should refuse to emit ACCEPTED unless the last `run_shell` call to pytest returned `EXIT_CODE: 0`. Currently this is honor-system — the system prompt asks the agent to test, but nothing enforces it.
- [ ] **Test Coverage Gate:** After tests pass, run `pytest --cov` and require > 60% coverage on files changed by the task. Store the coverage number in the task history.

### 6.3 Remote Continuity

- [ ] **Auto-Push on Accept:** `git push origin maestro/task-{id}` after ACCEPTED (see 1.3).
- [ ] **Milestone Push:** Every 5 accepted tasks, push main to remote. This is the "I can sleep at night" guarantee.
- [ ] **Backup Rotation:** Keep the last 10 `.archive/` snapshots. Older ones get pushed to a `maestro/archive` branch on the remote and pruned locally.

---

## **7. Agent Specialization (Phase 2)**

*Start with a single general-purpose coding agent. Specialize later once the loop and safety layers are proven.*

### 7.1 Phase 1: Single Agent (current)

- [x] **System Prompt:** `MAESTRO_SYSTEM_PROMPT` in `app/agent/system_prompt.py`. Covers orient → plan → implement → test → verify workflow.
- [x] **Tool Access:** All 16 tools available.
- [ ] **Tool Subsetting:** Add a `tool_profile` field to config. Profiles like `"coding"` (no .md writes), `"planning"` (no source writes), `"readonly"` (only read/search/list tools). The loop passes only the allowed tool schemas to the LLM.

### 7.2 Phase 2: Specialized Agents

- [ ] **Planning Agent:** Writes AGENTS.md, manages DAG. Tools: read all, write .md only, DAG tools.
- [ ] **Coding Agent:** Writes source and tests. Tools: read all, write source only, shell (pytest/lint only), git.
- [ ] **Debugging Agent:** Read-only. Runs tests and static analysis. Produces structured diagnostic reports.
- [ ] **Research Agent:** Read-only + web search (MCP). Produces context summaries for other agents.

### 7.3 Phase 3: Multi-Agent Orchestration

- [ ] **Agent Handoff Protocol:** When the coding agent emits REVERT, the orchestrator spawns a planning agent with the `advice` field as input. When the planning agent updates AGENTS.md, spawn a new coding agent.
- [ ] **Parallel Task Execution:** Run multiple independent tasks simultaneously (one worktree per task). The DAGResolver already identifies which tasks are ready and have no shared prerequisites.

---

## **8. LLM Engine Support**

*The system is designed for locally-hosted models via llama.cpp's OpenAI-compatible API. But it should also work with any provider that speaks the same protocol.*

- [x] **llama.cpp Integration:** Config points to `localhost:8008/v1`. Works with OmniCoder 9B (Qwen 3.5 base).
- [ ] **Model Registry:** A config file (`models.json`) listing available models with their endpoint, context window size, and capabilities (tool-call support, code-specific training, etc.).
- [ ] **Adaptive Turn Budget:** Set `MAX_TURNS` based on model capability. A 9B model gets 50 turns on simple tasks. An 80B model gets 150.
- [ ] **Qwen 3 Coder Next 80B Profile:** When running large models with strong instruction following, relax the "nudge" messages (the `[SYSTEM] You did not call any tool` messages) and increase `MAX_TOKENS_PER_TURN` to 8192.
- [ ] **Ollama / vLLM / Exo Support:** As long as the server exposes `/v1/chat/completions` with tool-call support, it should work. Document tested configurations.

---

## **Implementation Priority Order**

This is the recommended build sequence. Each phase makes the system meaningfully safer or more useful.

### Sprint 1: Make the Loop Launchable from the UI
1. "Run with Maestro" button on task cards.
2. Live status panel with turn count and stop button.
3. ACCEPTED/REVERT reactions (auto-move task, show feedback).

### Sprint 2: Harden the Sandbox
4. Shell allowlist (replace blocklist).
5. Eliminate `shell=True`.
6. Write journaling (snapshot before overwrite).
7. Force-push block in shell patterns.

### Sprint 3: Run Isolation
8. Git worktree per agent run.
9. Pre-run snapshot tags.
10. Auto-push on ACCEPTED.

### Sprint 4: Context & Multi-Model
11. Message compression at 80% context.
12. Per-task model override (llm field in schema).
13. Model roster in UI.

### Sprint 5: Design-Code Synchronization
14. AGENTS.md standardized schema.
15. Design validator gate.
16. Prerequisites editor in task modal.

### Sprint 6: Agent Specialization
17. Tool profiles (coding/planning/readonly subsets).
18. Planning agent with separate system prompt.
19. Agent handoff protocol (REVERT → planning agent → coding agent).

---

## **Notes**

* **Maestro Loop:** The system persists until all DAG nodes reach ACCEPTED.
* **Dual-Artifact Integrity:** Source Code must always be a derivative of the Markdown design.
* **Failure Protocol:** After 3 implementation failures, the system triggers REVERT_TO_DESIGN.
* **The Prime Directive:** No agent action can destroy data. Files are archived, not deleted. Writes are journaled. Branches are isolated. The remote is the last resort backup.
