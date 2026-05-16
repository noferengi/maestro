# Maestro — User Manual

> **Version:** Malleable Pipelines (May 2026)  
> **Server:** `http://localhost:8000/`

---

## Table of Contents

1. [The Big Picture](#1-the-big-picture)
2. [The Kanban Board](#2-the-kanban-board)
3. [The Pipeline Gallery](#3-the-pipeline-gallery)
4. [The Pipeline Editor](#4-the-pipeline-editor)
5. [The Agent Registry & Custom Agent Designer](#5-the-agent-registry--custom-agent-designer)
6. [The Document Store](#6-the-document-store)
7. [The Card Factory](#7-the-card-factory)
8. [Autopilot & Mission System](#8-autopilot--mission-system)
9. [Workspace Isolation & File Recovery](#9-workspace-isolation--file-recovery)
10. [Arch Categories](#10-arch-categories)
11. [Quick Reference — URLs](#11-quick-reference--urls)

---

## 1. The Big Picture

Maestro is an **AI orchestration platform**. You give it work; it dispatches the right AI agent at each step; each agent produces output that the next agent consumes. The lifecycle of a piece of work (a *card*) is controlled by a **pipeline template** — a directed graph of stages you define.

Before May 2026, the pipeline was hardcoded: every project ran through `IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FINAL_REVIEW → HUMAN_REVIEW → COMPLETED`. That constraint is gone. **Any workflow is now first-class.**

```
Novel Writing:    IDEA → Outline → Chapter Draft → Continuity Check → Line Edit → Human Review → PUBLISHED
Research Report:  IDEA → Topic → Research → Outline → Draft → Fact Check → Format → Human Review → PUBLISHED
Software Dev:     IDEA → Planning → Implementation → Review → [Security || Optimization] → Final Review → DONE
Mathematics:      IDEA → Problem Statement → Approach Factory → Proof Attempt → Peer Review → Synthesis → ACCEPTED
Bug Triage:       BUG REPORT → Reproduce → Root Cause → Fix → Regression Test → Human Review → RESOLVED
```

These are all built-in templates you can use today, clone and modify, or use as inspiration for your own.

---

## 2. The Kanban Board

**URL:** `http://localhost:8000/`

The main board. Cards are tasks flowing through a pipeline. Columns represent pipeline stages.

### Pipeline Switcher Bar

Directly above the kanban columns you'll see a thin bar:

```
Pipeline  [Software Development ▼]   Edit ↗
```

- The **dropdown** lists every pipeline template available. Selecting one immediately reassigns the current project to that template and redraws the board with the new columns. Cards whose `stage_key` doesn't exist in the new template disappear from view (they're still in the DB — they just belong to a stage that's hidden in this view). Switch back and they reappear.
- **Edit ↗** opens the Pipeline Editor for the currently selected template in a new tab.

### Dynamic Columns

Columns are now fully derived from the active pipeline template. There are no hardcoded Software-Dev column names in the UI. Switch to "Novel Writing" and you get chapter-writing columns. Switch to "Mathematics" and you get proof-exploration columns. The board rebuilds itself on every project/template switch.

Stages that belong to a **stage group** are rendered inside a bracket group. In Software Dev, `Conceptual Review`, `Optimization`, `Security`, and `Final Review` are grouped under an "AI Review" bracket — both can run in parallel.

### Adding a Card

Click **+ Add Idea** in the first column. Fill in the title and description and press Enter. The card enters the pipeline at the entry stage.

### Autopilot Toggle

Top-right of the board:

```
⚡ Leave it to the Maestro    ← autopilot is OFF
⏸ Human in the Loop          ← autopilot is ON
```

See [Section 8](#8-autopilot--mission-system) for full detail.

### Arch Bar

The horizontal bar above the pipeline switcher bar shows **architecture cards** — global project knowledge that agents inject into their context. Click any category chip to expand. Click the ⚙ icon to manage categories. See [Section 10](#10-arch-categories).

### Documents Button

A **📄 Docs** button in the project header opens the Document Store modal. See [Section 6](#6-the-document-store).

---

## 3. The Pipeline Gallery

**URL:** `http://localhost:8000/pipelines`

The gallery shows every pipeline template: built-in templates shipped with Maestro, plus any you've created or imported.

```
┌────────────────────┐  ┌────────────────────┐  ┌────────────────────┐
│ 📋 Software Dev    │  │ ✍ Novel Writing    │  │ 🔬 Research Report │
│ 10 stages          │  │ 8 stages           │  │ 9 stages           │
│ Built-in           │  │ Built-in           │  │ Built-in           │
│ [Use][Clone][Edit] │  │ [Use][Clone][Edit] │  │ [Use][Clone][Edit] │
└────────────────────┘  └────────────────────┘  └────────────────────┘
```

### Actions

| Button | What it does |
|---|---|
| **Use** | Assigns the template to the current project. Tasks already in the project are remapped to the new template's stages (or stay put if their `stage_key` exists in the new template too). |
| **Clone** | Creates a private editable copy under a new name. Opens immediately in the editor. |
| **Edit** | Opens the Litegraph editor for this template. Built-in templates show a warning banner but editing is still allowed. |
| **Export** | Downloads the template as a JSON file you can share or back up. |
| **Delete** | Only available for non-built-in templates. Blocked if any project is currently using the template. |

### Creating a New Template

Click **+ New Template** (top-right). Gives you a blank canvas with a single `IDEA` stage. Start drawing from there.

### Importing

Click **Import JSON** and paste or upload a previously exported template JSON. A new template is created (existing templates are never overwritten by import).

### Navigating to Custom Agents

From the gallery topbar, click **Agent Definitions →** to go to the custom agent management page.

---

## 4. The Pipeline Editor

**URL:** `http://localhost:8000/pipelines/{id}/edit`

A full-canvas node editor powered by **Litegraph.js**. You draw nodes, wire them together with directed edges, and each edge carries a *condition* (`pass`, `fail`, `reject`, `always`, `skip`). The resulting graph is what the scheduler reads to decide where a card goes next.

### Canvas Controls

| Action | How |
|---|---|
| Pan | Click empty canvas + drag |
| Zoom | Scroll wheel |
| Select multiple | Drag a selection rectangle |
| Open stage properties | Double-click a node |
| Draw a new edge | Drag from an output port to another node's input port |
| Set edge condition | Right-click an edge → Set Condition |
| Delete node or edge | Select it + press `Delete` |
| Group stages | Select multiple nodes → right-click → Group |
| Auto-layout | Click **Tidy Layout** in the top bar |
| Simulate forward pass | Click **Simulate** — a ghost token steps through `pass`-condition edges |

### Edge Rendering

- **Solid lines** — forward progress edges (`pass`, `always`)
- **Dashed amber lines** — back-edges (`fail`, `reject`) — loops that send a card backward

### Node Types

| Type | Shape / icon | Purpose |
|---|---|---|
| **Stage** | Rectangle | Runs an agent; primary building block of any pipeline |
| **Factory** | Rectangle + factory icon | Ingests external data (folder, CSV, database) and batch-creates cards |
| **Conditional** | Rectangle | Branches based on a content field value |
| **Voting Panel** | Rectangle | Runs N LLM voters and tallies their verdicts |
| **Fan-Out + Judge** | Rectangle | Spawns N parallel agent attempts; judge picks the best |
| **Human Gate** | Rectangle | Pauses until a human approves or Maestro autopilot handles it |

### The Property Panel

Double-click any node to open the slide-in panel on the right.

```
Stage Properties
────────────────────────────────────
Stage key (read-only):  chapter_draft

Display label      [ Chapter Draft        ] ⚡
Agent type         [ writing_agent       ▼] ⚡
Color              [ #1e40af ████          ]

Intent
┌───────────────────────────────────┐ ⚡
│ Draft a chapter from the outline  │
└───────────────────────────────────┘

System prompt
┌───────────────────────────────────┐ ⚡
│ You are a novelist...             │
└───────────────────────────────────┘

Gate type     [ llm_judge ▼ ]
Max retries   [ 3            ]
Verifier      [ none         ▼ ]

Tools allowed:
  ☑ read_file   ☑ write_file   ☐ web_search

Required input keys  [ outline        ] ⚡
Output keys          [ chapter_draft  ] ⚡

                            [Save]  [Revert]
```

#### The ⚡ Lightning Button

Every field with a ⚡ button can be AI-generated. Click it and Maestro sends the current panel state — agent type, intent, predecessor stage labels, successor stage labels, edge conditions — to its field-generation LLM. The field populates progressively. You can:

- Fill in the **Intent** first (one sentence describing what this stage does), then click ⚡ on **System prompt** to get a complete, specific system prompt in seconds.
- Click ⚡ on **Label**, **Gate type**, or **Tool allowlist** to get sensible defaults based on the intent alone.
- Type a partial value in any field and click ⚡ — the generator treats whatever you typed as a directional hint rather than starting from scratch.

The saved panel content is exactly what the agent receives at runtime. There is no inference-at-dispatch-time magic.

#### The Intent Field

This is the primary authoring surface. Write one sentence describing what this stage should accomplish — in plain English, from the perspective of the pipeline designer, not the LLM. Then use ⚡ to turn it into a full system prompt.

### Saving

Click **Save** in the top bar. The full graph (all nodes, edges, groups, and stage configs) is written to the database. Any project currently using this template immediately sees the new stage definitions on their kanban board.

### Stage Deletion Safety

You cannot delete a stage that has cards currently assigned to it without choosing a redirect stage first. The editor blocks deletion and prompts you to pick a replacement. No card ever ends up in a null stage.

---

## 5. The Agent Registry & Custom Agent Designer

### What is an Agent?

Each pipeline stage runs an *agent* — a piece of code that receives the task context, calls the LLM (possibly many times with tool calls), and produces an output. Maestro ships with built-in agents for planning, implementation, writing, research, security review, and more. You can also define your own entirely from configuration — no Python required.

### Built-in Agent Types

| Key | Name | Best for |
|---|---|---|
| `planning_agent` | Planning | Decomposing a task into an implementation plan |
| `implementation_agent` | Implementation | Writing code, running tests, iterating |
| `review_agent` | Conceptual Review | Critiquing an approach against the plan |
| `optimization_agent` | Optimization | Improving performance and code quality |
| `security_agent` | Security Review | Finding vulnerabilities and risks |
| `final_review_agent` | Final Review | Holistic pre-ship review |
| `writing_agent` | Writing | Drafting, revising, editing prose |
| `research_agent` | Research | Web search and synthesis |
| `custom_llm_agent` | Custom | Defined by a `custom_agent_definitions` row |
| `generic_stage` | Generic LLM Stage | Configured entirely from `stage.config` — no DB row needed |
| `circuit_breaker` | Circuit Breaker | Counts attempts; parks a card when a limit is reached |
| `voting_panel` | Voting Panel | N LLM voters tally; majority/threshold decides outcome |
| `fan_out_judge` | Fan-Out + Judge | N parallel proposals; judge picks the best one |
| `human_gate` | Human Gate | Blocks until human approves (or autopilot handles it) |
| `factory_node` | Card Factory | Ingests external data and batch-creates cards |
| `intake_agent` | Intake | IDEA stage processing |
| `terminal` | Terminal | Marks the end of the pipeline; no dispatch |

### Custom Agent Designer

**URL:** `http://localhost:8000/agents`

The agent definitions gallery lists every custom agent definition in the database. From here you can create, edit, and delete custom agent definitions.

Click **+ New Agent** to create one and be taken directly to the editor.

**URL:** `http://localhost:8000/agents/{id}/edit`

The editor has six sections:

#### Section 1 — Identity

- **Name** — a machine-readable slug (e.g. `continuity_checker`). This is the key the pipeline editor uses in its agent-type dropdown.
- **Display name** — the human-readable label shown in the dropdown (e.g. `Continuity Checker`).
- **Description** — short summary shown in the gallery card.
- **Intent** — one sentence describing what this agent should accomplish. Used by ⚡ generation.

#### Section 2 — System Prompt

The full instruction set the LLM receives at the start of every session. Write it by hand, or:

1. Fill in **Display name** and **Intent**
2. Click **⚡ Generate**
3. The textarea fills progressively with a complete, specific system prompt you can then edit

#### Section 3 — User Prompt Template

By default the agent receives: *Task ID, title, and description.* If you need more control — for example, to inject a specific content key from the task's data blob — enable the custom template:

```
Task: {task_title}
Description: {task_description}
Genre: {content_genre}
Outline: {content_outline}
```

Available variables:
- `{task_id}`, `{task_title}`, `{task_description}`, `{task_stage}`, `{task_project}`
- `{content_<key>}` — any key from the task's content JSON (e.g. `{content_chapter_number}`)

#### Section 4 — Tool Access

Checkboxes grouped by category. Tools your agent can call:

| Category | Examples |
|---|---|
| Files | `read_file`, `write_file`, `append_file`, `patch_file`, `list_directory`, `find_files` |
| Code Analysis | `find_symbol`, `find_callers`, `find_imports_of` |
| Git | `read_git_status`, `read_git_diff`, `write_git_commit`, `write_git_branch` |
| Tasks | `get_task`, `list_tasks`, `batch_create_cards` |
| Planning | `write_arch_doc`, `write_mermaid`, `write_interface_contract` |
| Testing | `run_test_pytest`, `run_test_npm`, `run_test_cargo` |
| Web | `web_search`, `web_fetch` |
| Documents | `store_document`, `get_document`, `search_documents`, `list_documents` |
| Workspace | `workspace_delete_file`, `workspace_rename_file` |

**Preset buttons:** None / Files Only / Development / Full Access — set the checkboxes to common configurations in one click.

`submit_work` is always enabled and locked — it's how the agent signals completion to the gate.

#### Section 5 — Session Limits

- **Max turns** — how many LLM round-trips before the session times out and the card is sent to the `fail` transition. Leave blank to use the stage config value or the system default (20 turns).
- **Max tokens** — total token budget for the session. Leave blank for unlimited.

#### Section 6 — Conclusion Gate

- **Gate type** — how the agent's output is evaluated:
  - `llm_judge` — the agent explicitly calls `submit_work({"verdict": "ACCEPTED|REJECTED", ...})`
  - `single_pass` — always passes regardless of output
  - `none` — unconditional pass (same as single_pass)
- **Verifier** — optional formal verification after the LLM gate:
  - `none` — no verification
  - `python_sympy` — runs a SymPy proof script from `task.content.sympy_proof_code`; returns exit code 0 = pass
  - `lean4` — runs Lean 4 on `task.content.lean_proof` (requires `lean` on PATH)
  - `coq` — runs Coq on `task.content.coq_proof` (requires Coq installed)
  - `custom_script` — runs `verifier_cmd` as a shell command with task content as stdin; exit 0 = pass
- **Verifier command** — only shown when `verifier = custom_script`

---

## 6. The Document Store

Each project has a **shared document store** — a key/value store where agents can write named artifacts and other agents can read them back. This is the cross-card coordination layer for multi-card workflows.

### Accessing the Store

On the kanban board, click **📄 Docs** in the project header bar. A modal appears showing all documents for the current project:

```
┌──────────────────────────────────────────────────────┐
│ Project Documents          [Search key…]              │
├────────────────┬──────────┬──────────────┬───────────┤
│ Key            │ Tags     │ Written by   │ Updated   │
├────────────────┼──────────┼──────────────┼───────────┤
│ proofs/lemma_1 │ math     │ card #42     │ 2 hr ago  │
│ approach/angle │ strategy │ card #38     │ 3 hr ago  │
└────────────────┴──────────┴──────────────┴───────────┘
```

Click a row to view the full document content.

### How Agents Use It

Any agent whose stage config includes `store_document`, `get_document`, `search_documents`, or `list_documents` in its tool allowlist can read and write the store.

**Writing:**
```
store_document(key="proofs/lemma_3", content="...", tags=["math"])
```

**Reading (exact key):**
```
get_document(key="proofs/lemma_3")
```

**Fuzzy key search** (for when you're not sure of the exact key):
```
search_documents(query="proofs/lemma")
```
Returns the closest-matching keys using PostgreSQL trigram similarity — no embeddings, no vector database.

**Listing:**
```
list_documents(tag="math")
```

### Key Design Decisions

- **Last write wins** — writing to the same key twice updates the document in place. Use unique keys for distinct artifacts (`proofs/attempt_1`, `proofs/attempt_2`).
- **Keys are case-normalized** — `Characters/Elara` and `characters/elara` are the same key.
- **Project-scoped** — documents are per-project; agents in different projects cannot see each other's stores.
- **Provenance** — every document records which card wrote it.
- **Soft delete** — deleted documents are hidden but not erased.

### REST API

```
GET    /api/projects/{name}/documents          — list all (metadata only)
GET    /api/projects/{name}/documents/{key}    — get one document (full content)
PUT    /api/projects/{name}/documents/{key}    — create or update (human edit)
DELETE /api/projects/{name}/documents/{key}    — soft-delete
```

---

## 7. The Card Factory

A **factory node** in a pipeline ingests external data and batch-creates cards — one card per item, or LLM-segmented (the agent decides how many cards and what they contain).

### Adding a Factory to a Pipeline

In the Pipeline Editor, drag a **Factory** node onto the canvas. Wire it into the pipeline like any other stage — its output feeds cards into the next stage.

Double-click the factory node to configure it in the property panel:

```
Factory Node Properties
────────────────────────────────────
Label         [ Research Loader   ] ⚡
Source type   [ folder           ▼]
Folder path   [ /data/papers/     ]
File glob     [ *.pdf             ]
☐ Recursive

Segmentation  [ Mechanical (1 per file) ▼]
Entry stage   [ ingest            ▼]

Card title    [ Process: {filename} ] ⚡
Card desc.    [ Summarize {filepath} ] ⚡

Triggers
  ☑ Manual button   ☑ Predecessor complete
  ☐ Cron schedule   [                   ]

              [Run Now] [Save] [Revert]
```

### Data Source Types

| Source type | What it reads |
|---|---|
| `folder` | One card per file matching a glob pattern (e.g. `*.pdf`) |
| `file_list` | A text file where each line is a file path |
| `csv` | One card per row; column names become `{column}` template variables |
| `json_array` | One card per element of a JSON array file |
| `sqlite_query` | Runs a SQL query on an external SQLite data file; one card per row |
| `manual_prompt` | No data source; LLM decides how to segment based on the trigger card's description |
| `maestro_cards` | Cards already in the project at a specified stage |

### Segmentation Modes

**Mechanical (1:1)** — one card per data item. Title and description are built by interpolating template strings with the item's fields:
- `{filename}`, `{filepath}`, `{extension}`, `{size_bytes}` for folder sources
- `{column_name}` for CSV sources

**LLM-segmented** — the factory dispatches an agent that reads the data source, decides how to group the work into cards, and calls `batch_create_cards` with its decision. The system prompt and intent fields on the factory node control this agent's behavior.

### Trigger Mechanisms

| Trigger | When it fires |
|---|---|
| **Manual** | Click "Run Now" in the property panel, or the "Run Factory" button in the task list |
| **Predecessor complete** | Whenever a card in the immediately preceding stage reaches `completed` |
| **Cron** | On a cron schedule (e.g. `0 23 * * *` = 11 PM daily) |

### Template Variables

Use `{variable}` in card title and description templates. Missing variables are left as `{variable}` literals (no crash). For CSV sources, any column name works: `{author}`, `{publication_date}`, etc.

---

## 8. Autopilot & Mission System

Autopilot is the "YOLO mode" toggle. When on, Maestro dispatches agents without waiting for human approval at each stage — including `human_gate` stages, which are handled by the Maestro orchestrator automatically.

### The Toggle

```
⚡ Leave it to the Maestro    ← click to engage autopilot
⏸ Human in the Loop          ← click to pause immediately
```

Clicking **Leave it to the Maestro** opens the **Mission Dialog** before engaging.

Clicking **Human in the Loop** pauses immediately: all running agent sessions receive a graceful stop signal, no new tasks are dispatched.

### Mission Dialog

```
Leave it to the Maestro
────────────────────────────────────────────
Stop when any of these conditions is met:

☑  Time limit      [ 8    ] hours
☑  Token budget    [ 500k ] tokens
☐  Card count      [      ] cards completed
☐  Goal card       [ Select card...    ▼  ]

Scheduled hours (optional)
  Active from [ 23:00 ] to [ 07:00 ]
  ☐ Apply schedule to all future sessions

                  [Start Maestro]  [Cancel]
```

- **Time limit** — wall-clock duration from the moment you click Start.
- **Token budget** — total LLM tokens used across all tasks in this mission.
- **Card count** — stop when N cards reach COMPLETED.
- **Goal card** — stop when a specific card reaches COMPLETED.

Any condition fires first wins ("first-breach-wins"). When a condition fires, autopilot turns off, running sessions stop gracefully, and a **Mission Report** arch card is created with stats (duration, cards completed, tokens used, termination reason).

Mission settings are **remembered in browser localStorage** — next time you open the dialog, it pre-fills with the last-used values.

### Scheduled Hours

Configure the hours during which autopilot is allowed to dispatch. For an overnight run:
- Active from `23:00` to `07:00`

Maestro will not dispatch tasks outside this window even if autopilot is toggled on. The schedule persists in the database; individual sessions can override it.

### Per-Project Override

Individual projects can opt out of global autopilot or opt in even when it's globally off. Set via `GET/POST /api/projects/{name}/settings` with `autopilot_override: inherit | force_on | force_off`.

### Server Restart Safety

If the server restarts while autopilot is on, it detects the orphaned state on startup and resets autopilot to `off`. You must re-engage the mission manually after a restart.

---

## 9. Workspace Isolation & File Recovery

Every card gets its own **scratch pad** — an isolated git worktree at `.maestro-worktrees/{task_id}/`. Agents read and write files there without affecting the main repo or other cards' worktrees.

### Deletion Protection

When an agent deletes a file (via `workspace_delete_file`), it doesn't actually delete the file. Instead it:

1. Moves the file to `.archive/YYYY-MM-DD_HH-MM-SS/{task_id}/{original_path}`
2. Creates an `archived_files` database record with the original path, archive path, and task ID
3. Returns an `archive_id` that the agent can report in its output

This means you can always recover a deleted file — even days later.

### Restoring Files

**Via the UI:**

For any card, look in the card's diagnostics panel. Find the "Archived Files" section. Click **Restore** next to any entry to put the file back at its original path (or choose a new path if the original is occupied).

**Via the API:**
```
GET  /api/tasks/{task_id}/archived-files    — list all archived files for this card
POST /api/tasks/{task_id}/undelete          body: {archive_id, restore_path?}
```

### Agent Tools for File Operations

Agents with these tools in their allowlist get deletion-safe file operations:

| Tool | What it does |
|---|---|
| `workspace_delete_file(path, reason)` | Archives the file; returns `archive_id` |
| `workspace_rename_file(src, dst)` | Renames within the worktree; fails if `dst` exists |
| `read_file`, `write_file` | Standard read/write (writes are not archived — only deletes are) |

---

## 10. Arch Categories

**Architecture cards** are a parallel knowledge system. While regular cards flow through pipeline stages, arch cards live in a permanent categorized sidebar (the arch bar at the top of the board). Agents inject selected arch card categories into their system prompt context — this is how you give an agent access to "what the project knows" about characters, known theorems, API contracts, etc.

### Categories Per Template

Arch categories are **per pipeline template**, not global. Software Development ships with: Platform, Design, Testing, Performance, API, Data, Tooling, Security, DevOps, Documentation, Quality, Cost, Scalability, General.

Novel Writing ships with: Characters, Themes, Plot, World Building, Timeline, Voice/Style, Research Notes, Continuity Log.

Mathematics ships with: Known Theorems, Definitions, Conjectures, Failed Approaches, Partial Results, Open Sub-Problems.

### Managing Categories

Click the ⚙ icon in the arch bar. A modal appears:

```
Arch Categories — Novel Writing Pipeline
─────────────────────────────────────────
≡  Characters   [████ #7c3aed]  [Rename] [Delete]
≡  Themes       [████ #1e40af]  [Rename] [Delete]
≡  Plot         [████ #065f46]  [Rename] [Delete]

                               [+ Add Category]
```

Drag the ≡ handles to reorder. Click a color swatch to pick a new color.

Deleting a category that has existing arch cards reassigns those cards to "General" (or you choose a replacement).

### Per-Stage Category Context

In the Pipeline Editor's stage property panel, the **Arch category context** section shows checkboxes for all categories in the template. Check the ones this stage's agent should see. Checked categories are injected into the agent's system prompt as a structured knowledge block:

```
=== Project Knowledge ===
[Characters]
- Elara Voss: protagonist, cartographer, late 30s, stoic
- Brennan Cole: antagonist, merchant guild leader

[Timeline]
- Chapter 1: Year 847, the mapping expedition begins
- Chapter 3: The guild discovers the map
========================
```

### REST API

```
GET/POST/PUT/DELETE /api/pipelines/{id}/arch-categories[/{c_id}]
GET                 /api/projects/{name}/arch-categories   — active template's categories
```

---

## 11. Quick Reference — URLs

| URL | What it is |
|---|---|
| `http://localhost:8000/` | Kanban board |
| `http://localhost:8000/pipelines` | Pipeline template gallery |
| `http://localhost:8000/pipelines/{id}/edit` | Pipeline editor (Litegraph canvas) |
| `http://localhost:8000/pipelines/new` | Create a new blank template (redirects to editor) |
| `http://localhost:8000/agents` | Custom agent definitions gallery |
| `http://localhost:8000/agents/new` | Create a new agent definition (redirects to editor) |
| `http://localhost:8000/agents/{id}/edit` | Custom agent definition editor |

### Key API Endpoints

```
# Pipeline CRUD
GET/POST           /api/pipelines
GET/PUT/DELETE     /api/pipelines/{id}
GET/POST/PUT/DELETE /api/pipelines/{id}/stages[/{stage_id}]
GET/POST/PUT/DELETE /api/pipelines/{id}/transitions[/{t_id}]
GET/POST/PUT/DELETE /api/pipelines/{id}/groups[/{g_id}]
GET/POST/PUT/DELETE /api/pipelines/{id}/arch-categories[/{c_id}]
GET                /api/pipelines/{id}/export
POST               /api/pipelines/import
POST               /api/pipelines/generate-field           # ⚡
GET                /api/pipelines/agent-types              # registry listing

# Custom agent definitions
GET/POST/PUT/DELETE /api/agent-definitions[/{id}]
GET                 /api/agent-definitions/tool-manifest   # tool list with categories

# Assign template to project
POST               /api/projects/{name}/pipeline           # body: {template_id}
POST               /api/projects/{name}/use-template       # alias

# Document store
GET/PUT/DELETE     /api/projects/{name}/documents[/{key}]

# Card factory
POST               /api/pipelines/stages/{id}/trigger-factory

# File recovery
GET                /api/tasks/{id}/archived-files
POST               /api/tasks/{id}/undelete

# Autopilot
GET/POST           /api/settings/autopilot
GET/POST           /api/projects/{name}/settings
```

---

*Maestro — build it once, let it think forever.*
