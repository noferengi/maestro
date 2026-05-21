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
12. [Self-Modification (Advanced)](#12-self-modification-advanced)
13. [Autopilot Objectives & Goal Hierarchy](#13-autopilot-objectives--goal-hierarchy)
14. [Model Routing — Stage-to-LLM Assignment](#14-model-routing--stage-to-llm-assignment)
15. [Episodic Memory](#15-episodic-memory)
16. [Reflection Stage](#16-reflection-stage)
17. [Math Tooling & Formal Verification](#17-math-tooling--formal-verification)
18. [Event Triggers](#18-event-triggers)
19. [Training Data Pipeline](#19-training-data-pipeline)
20. [Inter-Agent Communications](#20-inter-agent-communications)
21. [Orchestrator LLM & Maestro Escalation](#21-orchestrator-llm--maestro-escalation)

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
| `reflection_agent` | Reflection | Skeptical re-read of prior stage output; produces a structured JSON confidence report |
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

# Self-modification (Gap 5)
POST               /api/tasks/{id}/self-mod-merge
GET                /api/tasks/{id}/revert-votes
GET                /api/projects/_maestro_self/integration-branch-status
```

---

## 12. Self-Modification (Advanced)

> **Warning:** This is a high-risk feature. When enabled, Maestro agents can write to
> the Maestro source tree itself. Mistakes can corrupt the running server. Only enable
> on development instances with version control and tested backups.

### Overview

GAP 5 adds a guarded self-modification pathway. Agents running under the reserved project
`_maestro_self` can edit a whitelist of Maestro source files, subject to:

1. The `can_self_modify = true` flag in `maestro.ini`
2. File-level allowlist at `app/agent/self_modification_allowlist.py`
3. Permanent hard-block list (safety guard, deletion module, migrations, secrets)
4. Full test suite must pass before any merge to the integration branch
5. Human must manually merge `maestro/self-improvement` → `main`

### Setup

**Step 1** — Create the `_maestro_self` project pointing at the Maestro repo itself:

In the UI: New Project → Name: `_maestro_self` → Path: `D:/workspace/TheMaestro`

**Step 2** — Enable the capability in `maestro.ini`:

```ini
[maestro_capabilities]
can_self_modify = true
```

Restart the server after editing `maestro.ini`.

**Step 3** — (Optional) Enable auto-merge to the integration branch:

```ini
can_auto_merge_human_review = true
can_auto_merge_self_modification = true
```

Without these flags, tasks complete at the `human_review` stage and you merge manually
via `POST /api/tasks/{id}/self-mod-merge`.

### What agents can write to

The allowlist in `app/agent/self_modification_allowlist.py` controls which files are in
scope. By default this includes agent system files, database CRUD, frontend JS/CSS, and
test files. Migrations, config files, secrets, and the safety guard (`tools.py`) are
permanently off-limits.

To extend the allowlist, add the absolute path to `ALLOWED_PATHS` in that file.

### Integration branch

All self-modification work accumulates on `maestro/self-improvement`. This branch is
**never** automatically merged to `main` — only a human can do that. Check its status at
any time via:

```
GET /api/projects/_maestro_self/integration-branch-status
```

### Revert voting

If an agent identifies a regression from a recent self-modification merge, it can call
the `vote_to_revert` tool. When the vote count reaches `revert_vote_threshold` (default 3),
the system automatically runs `git revert` on `maestro/self-improvement` and creates a
PIP card summarizing all votes. View votes via:

```
GET /api/tasks/{id}/revert-votes
```

---

---

## 13. Autopilot Objectives & Goal Hierarchy

Autopilot objectives are long-running goals that Maestro pursues autonomously across many scheduler ticks. The basic autopilot toggle (section 8) starts/stops the engine; objectives are what direct it.

### Creating an Objective

Open **Project Settings** (⚙ gear icon) → scroll to the **Objectives** panel.

```
Objectives
─────────────────────────────────────────────────────────────
[P10] ● Explore twin prime gaps               [pause] [edit] [✕]
      [P8]  ○ Sieve to 10^9                   [maestro]
      [P8]  ○ Formalize Zhang bounds           [maestro]
      [P5]  ✓ Calibrate on Bertrand postulate  completed

                              [+ Add Objective]
```

- **P** = priority (1–10, higher is more urgent)
- **●** active · **○** child active · **✓** complete
- **[maestro]** badge — this sub-objective was created by Maestro autonomously
- **⚡ badge on kanban cards** — card was spawned by an autopilot objective

**Add Objective form fields:**
- Description (what Maestro should pursue)
- Priority (1–10)
- Time-box hours (optional — objective expires automatically after this many hours)

### Objective Lifecycle

1. **Active** — Maestro assesses progress each stall or completion event, creates IDEA cards, and records findings.
2. **Appears complete** — Maestro sets this on the first tick it's confident. It only marks complete on the *second* tick of sustained confidence (prevents premature closure).
3. **Complete** — status flipped; if this was the last active child, the parent auto-completes.
4. **Paused** — set manually (UI buttons) or automatically by spin detection (same card demoted ≥ N times from this objective).
5. **Stuck badge** — shown when spin detection fires. Requires human review to resume.

### Sub-Objectives (Hierarchy)

Objectives can be nested. When Maestro creates a sub-objective (requires `can_create_objectives = true` in `maestro.ini`), it appears indented under the parent in the UI. Completing all children propagates up to complete the parent.

```ini
[maestro_capabilities]
can_create_objectives = true    # allow Maestro to create its own sub-objectives
can_complete_objectives = true  # allow Maestro to mark objectives complete
can_create_cards = true         # allow Maestro to spawn IDEA cards
max_objectives_per_tick = 2     # max objectives assessed per tick
```

When `can_create_objectives = false`, Maestro writes suggested sub-objectives to the evidence log instead of inserting them.

### Evidence Log

Maestro maintains an evidence document per objective at key `objective:{id}:evidence` in the project document store. This is an append-only timestamped log of what was found, what failed, and what was ruled out. View it by clicking an objective row in the UI (evidence toggle panel).

Agents working on autopilot-spawned cards receive the spawning objective's description and evidence summary in their system prompt automatically.

### Autopilot Budget

To cap spending per project, set an **Autopilot Budget** in project settings (dropdown picks a budget record). Maestro suppresses itself when that budget is exhausted. Set **Max in-flight cards** to prevent board saturation.

### REST API

```
GET    /api/projects/{name}/objectives              — list (filterable by status)
POST   /api/projects/{name}/objectives              — create
PUT    /api/projects/{name}/objectives/{id}         — edit/pause/resume
DELETE /api/projects/{name}/objectives/{id}         — delete
GET    /api/projects/{name}/objectives/tree         — nested tree for UI
GET    /api/projects/{name}/objectives/{id}/evidence — full evidence log text
```

---

## 14. Model Routing — Stage-to-LLM Assignment

By default every stage in a project runs on the project's configured LLM. Model routing lets you assign a specific LLM to specific pipeline stages — use a fast cheap model for file-summary stages, a reasoning-heavy model for planning, a math-capable model for proof stages.

### LLM Capability Tags

In the LLM configuration panel (`/api/llms`), each LLM endpoint can be tagged with capability strings:

| Tag | Meaning |
|---|---|
| `reasoning` | Multi-step logical reasoning and planning |
| `code` | Code generation and debugging |
| `math` | Symbolic and formal mathematics |
| `fast` | Low latency, optimised for short tasks |
| `long_context` | Context window > 32K tokens |
| `cheap` | Low cost per token; prefer for bulk tasks |

Tags are stored in `llms.capabilities` and are visible in the routing UI. They are advisory — the routing resolution does not enforce them automatically, but they help you make correct assignments.

### Routing Table in Project Settings

Open **Project Settings** → **Model Routing** section:

```
Stage               Assigned Model          (default: Qwen 35B)
──────────────────────────────────────────────────────────────
PLANNING            [Claude Sonnet ▾]
INDEV               [Qwen 35B ▾]
SECURITY            [Claude Opus ▾]
FINAL_REVIEW        [Qwen 35B ▾]
HUMAN_REVIEW        (no model needed)
```

Each row has an LLM picker and a **Clear** button (reverts to project default). Stages of type `human_review` and `verifier` are greyed out — they don't run an LLM.

### Resolution Order

When dispatching a task, the scheduler resolves the LLM in this order:

1. **Human-pinned** — task was manually assigned a specific LLM in the task edit panel (`task.llm_pinned = true`). This always wins.
2. **Routing table entry** — `project_llm_routing[stage_key]` for the current project.
3. **Project default** — `project.llm_id`.
4. **System default** — `maestro.ini` → `[orchestration] default_llm_id`.

### Hard Routing and Blocked Tasks

Maestro uses **hard routing** — a task waits for its assigned model rather than falling back. If the assigned model is at capacity for more than `model_block_timeout_minutes` (default 30), the task is marked `blocked_on_model` and surfaced in project health for human review.

```ini
[scheduler]
model_block_timeout_minutes = 30
```

### Cost by Model

```
GET /api/projects/{name}/cost-by-model
```

Returns token and cost breakdown by model and by stage for the project.

### REST API

```
GET    /api/projects/{name}/routing
PUT    /api/projects/{name}/routing/{stage}    body: {"llm_id": N}
DELETE /api/projects/{name}/routing/{stage}    — revert stage to project default
```

---

## 15. Episodic Memory

Maestro maintains a vector database of past experiences — failures, session summaries, and document writes — and retrieves semantically similar past episodes at the start of each agent session. This prevents the system from rediscovering the same failure modes repeatedly.

### What Gets Stored

| Episode type | When it's stored |
|---|---|
| `failure` | Immediately when a task is demoted |
| `session_summary` | Asynchronously after a session ends (2–4 sentence LLM summary) |
| `document` | When a document longer than 100 chars is written to the document store |

### What Agents See

At the start of every session, the top-K most relevant episodes (by cosine similarity × recency decay) are injected under a `### Relevant past experience` block in the system prompt:

```
### Relevant past experience
- [failure | 2026-04-12] Task 'Prove twin prime gaps' demoted from PROOF_ATTEMPT.
  Lean4 syntax error on dependent type application. Standard rewrite tactic failed.
- [session_summary | 2026-04-15] Agent explored induction on gap modulus. Found
  counterexample at n=47. Pivoted to sieve approach.
```

Agents can also query episodic memory on demand with the `query_episodes` tool (when enabled in their stage's tool allowlist):

```
query_episodes(question="what approaches failed on formal proofs", k=5, episode_type="failure")
```

### Configuration

```ini
[episodic_memory]
embedding_llm_id =            ; LLM record with embedding endpoint (blank = local fallback)
decay_half_life_days = 90     ; episodes lose half their recency weight every 90 days
keepalive_extension_days = 14 ; each retrieval extends expiry by 14 days
auto_inject_k = 3             ; top-K auto-injected at session start (0 = disable)
```

### Staleness and Cleanup

Episodes that are never retrieved expire after 5 years. Episodes that remain useful (retrieved regularly) are kept alive indefinitely by the `keepalive_extension_days` mechanism. A nightly cleanup job hard-deletes expired rows.

### Requirements

The `pgvector` PostgreSQL extension must be installed. Run `/migrate` after enabling to apply migrations 0096–0097 if not already applied.

---

## 16. Reflection Stage

The **reflection** agent type is a pipeline stage that reads the prior stage's output skeptically and produces a structured JSON confidence report. Use it where logical errors, wrong assumptions, or missed edge cases are a concern — code review, proof checking, fact verification.

### Adding a Reflection Stage

In the Pipeline Editor, add a stage and set its **Agent type** to `Reflection`. Wire it between the stage you want reviewed and the next stage.

When a stage of type `reflection` is selected in the property panel, additional fields appear:

- **Reflection LLM** — which model runs the review (defaults to the orchestrator LLM; see section 21)
- **Max history turns** — how many prior LLM turns the reflection agent can inspect (default 20, max 50)

The **System prompt** field is pre-filled with a starter template you can customise:

```
You are a skeptical reviewer. Find real defects, wrong assumptions, and missed edge
cases — not vague concerns. If uncertain, state that explicitly in uncertain_about.
A high-confidence clean report is valuable. Output your structured JSON report at the end.
```

### Report Format

The reflection agent produces a JSON block at the end of its session:

```json
{
  "confidence": 0.72,
  "issues": [
    {"severity": "blocking", "finding": "Off-by-one in loop bounds..."},
    {"severity": "warning",  "finding": "Assumes input is sorted..."},
    {"severity": "note",     "finding": "Variable name x is ambiguous"}
  ],
  "uncertain_about": ["Whether the caching strategy is thread-safe..."]
}
```

Severity levels: `blocking`, `warning`, `note`. Reports are stored at `reflection:{task_id}:{stage_key}` in the document store.

### What Maestro Does With the Report

After the reflection stage, Maestro reviews the report and decides:

- **No blocking issues, confidence ≥ threshold** → advance to next stage
- **Blocking issues found** → retry the prior stage with the report injected as `[REFLECTION FEEDBACK]`, or demote, or create a PIP
- **Warnings only** → advance but surface findings in the human review stage

The decision is Maestro's judgment, not hardcoded logic. A `blocking` issue in a calibration stage may be treated differently from the same issue in final review.

### Configuration

```ini
[reflection]
confidence_threshold = 0.7    ; below this, Maestro treats as blocking regardless of issues list
max_history_turns = 20
```

### Manual Trigger

```
POST /api/tasks/{id}/trigger-reflection
```

---

## 17. Math Tooling & Formal Verification

The Mathematics / Proof Exploration pipeline template ships with nine working stages, real tool integrations, and a Docker-sandboxed execution environment.

### Requirements

**Docker Desktop must be running** before the Maestro server starts for any math tooling to work. Maestro checks on startup and logs a warning (not a crash) if Docker is unavailable. Start Docker Desktop, then restart the server.

Build the sandbox image once:

```bash
docker build -t sympy-lean4-sandbox:latest docker/sympy-lean4-sandbox/
```

The image contains Python 3.12 + SymPy/NumPy/SciPy, Lean 4 (via elan), and Coq.

### Agent Tools

| Tool | Available in stages | Purpose |
|---|---|---|
| `run_sympy` | Math stages | Execute Python/SymPy code in the Docker sandbox. Returns stdout + stderr (capped at 8 KiB each). Timeout configurable (default 120 s, max 600 s). |
| `search_arxiv` | Literature, Hypothesis, Proof Strategy | Search arXiv by query + category (e.g. `math.NT`). Returns title, authors, year, abstract (500 chars), and PDF URL. |
| `search_oeis` | Literature, Hypothesis | Search the OEIS integer sequences database. Returns sequence ID, name, first 20 values, formulas. |

**Docker isolation:** `run_sympy` runs with `--network none --memory 512m --cpus 1`. Agents cannot affect the host environment, make network requests, or consume unbounded memory.

### Stage-Gate Verifiers

Formal verification gates run automatically when a card advances past a `verifier` stage:

| Verifier key | What it runs |
|---|---|
| `python_sympy` | Runs `task.content.sympy_proof_code` in the Docker sandbox (30 s timeout). Exit 0 = pass. |
| `lean4` | Runs `task.content.lean_proof` through Lean 4 in the Docker sandbox. Full compiler stderr returned to the agent on failure — it reads the type-checker error and corrects. |
| `coq` | Runs `task.content.coq_proof` through Coq in the Docker sandbox. Degrades gracefully if Coq is absent from the image. |

Both `run_sympy` (mid-session) and the `python_sympy` verifier (gate) route through the same Docker sandbox — the host process never executes arbitrary agent code directly.

### Configuration

```ini
[math]
sandbox_memory_mb = 512
sandbox_timeout_default = 120
```

---

## 18. Event Triggers

Maestro can react to external events — a GitHub webhook, a file appearing in a watched directory, a URL whose content changes. Each event fires a Maestro autopilot tick with the event payload as context.

### Registering a Watch

Agents running in Maestro sessions can call `register_watch` to set up a watch:

```python
# Webhook watch — returns an inbound URL to configure in the external system
register_watch(
    event_type="webhook",
    label="GitHub push to main",
    source_config={"secret": "optional-hmac-secret"},
    fire_config={"cooldown_seconds": 60}
)
# Returns: {"inbound_url": "/api/events/inbound/42", "watch_id": 42}

# File system watch — fires when files appear or change
register_watch(
    event_type="file_watch",
    label="Inbox folder",
    source_config={"path": "C:/Users/mdm16/Documents/Inbox", "recursive": false},
    fire_config={"cooldown_seconds": 5}
)

# Scheduled URL poll — fires when the response content changes
register_watch(
    event_type="api_poll",
    label="arXiv twin primes",
    source_config={
        "url": "https://export.arxiv.org/api/query?search_query=ti:twin+primes&max_results=5",
        "poll_interval_seconds": 3600
    },
    fire_config={"use_content_hash": true}
)
```

### Deduplication

Each watch has independent dedup controls (set in `fire_config`):

| Key | Effect |
|---|---|
| `cooldown_seconds` | Suppress re-fire for N seconds after firing (default: 60 for webhooks, 5 for file watches) |
| `use_content_hash` | Only fire if payload differs from last firing (default on for API polls) |
| `max_fires` | Auto-expire the watch after N firings |
| `expires_at` | Hard expiry timestamp |

### Webhook Endpoint

```
POST /api/events/inbound/{watch_id}
```

Payloads are capped at 16 KiB. If the watch has a `secret` in its `source_config`, the endpoint validates the `X-Hub-Signature-256` header (GitHub webhook format).

### Managing Watches

```python
list_watches_for_project(project="Garden")  # tool available to agents
```

After 3 consecutive API poll failures, the watch is paused automatically and the errors are available via `get_watch_errors(watch_id)` (MCP tool).

---

## 19. Training Data Pipeline

Maestro accumulates training data from every agent session and exports it as Hugging Face-format JSONL for local fine-tuning.

### Opting a Project Out

In **Project Settings**, enable **Exclude from training data**. All sessions from that project are excluded from all future and retroactive export runs.

### Quality Signals

Sessions are automatically scored by a background job (hourly). A session qualifies for export if all of:
- Its task reached `completed` stage
- It has no turns truncated by context limit (`finish_reason != length`)
- It is not a mechanical session (file summaries, etc.)

Qualified sessions receive additional score bonuses:
- `+0.5` — session ended with `ACCEPTED` submit
- `+1.0` — **failure-recovery** session: ran after a task demotion and the task ultimately completed (most valuable signal — demonstrates self-correction)
- `+0.5` — contains a formally verified proof

### Export Format

Exports are Hugging Face conversational JSONL (`{"messages": [...]}`). System prompts are **stripped entirely** — only user/assistant/tool turns are exported. Tool calls are serialised as structured text blocks:

```json
{"role": "assistant", "content": "I'll check the file first.\n<tool_call>\n{\"name\": \"read_file\", ...}\n</tool_call>"}
{"role": "tool",      "content": "<tool_response>\n{file contents...}\n</tool_response>"}
```

Near-duplicate sessions (same task description fingerprint) are capped at `dedup_fingerprint_max` per export file to avoid over-representing repeated test tasks.

### Auto-Export Threshold

When the number of newly qualified unexported sessions reaches `export_threshold` (default 100), the export job runs automatically and writes a JSONL file to `export_dir`.

```ini
[training]
export_threshold = 100
export_max_per_run = 1000
export_dir = data/training_exports
dedup_fingerprint_max = 3
```

### Status and Manual Trigger

```
GET  /api/training/status    — qualified count, last export time, file list
POST /api/training/export    — manual export trigger; returns path to JSONL file
GET  /api/training/metrics   — demotion rate, completion rate, tokens-to-completion (segmented by checkpoint)
```

### Training Checkpoints

When you deploy a new fine-tuned model version, record a checkpoint so metrics can be segmented before and after:

```
POST /api/training/checkpoints    body: {"checkpoint_name": "qwen-35b-lora-2026-05-20", "model_notes": "..."}
```

`GET /api/training/metrics?after=checkpoint_id` then returns performance metrics for sessions since that checkpoint.

---

## 20. Inter-Agent Communications

Agents can ask peer agents questions at runtime and receive answers inline. The asking session blocks while the peer runs a short focused sub-session, then continues with the answer as a tool result.

### Calling `ask_agent`

```python
# First, find available sessions
sessions = list_active_sessions(project="Garden")
# [{"session_id": "abc123", "task_id": 42, "task_title": "Implement auth module", ...}]

# Ask a peer
answer = ask_agent(
    target_session_id="abc123",
    question="What authentication library did you decide to use and why?"
)
```

The peer session runs a focused LLM session (max 5 turns, read-only tools) and returns its answer. Budget for the sub-session is charged to the *calling* task.

### Depth Cap

To prevent runaway chains, each call increments an `ask_depth` counter. When `ask_depth >= ask_max_depth` (default 3), `ask_agent` returns:

```
Max inter-agent ask depth (3) reached. Make your best judgment with available information.
```

```ini
[orchestration]
ask_max_depth = 3
```

### Availability

`ask_agent` and `list_active_sessions` are available to `custom_llm`, `implementation`, `writing`, `research`, and similar worker agents. They are **not** available to Maestro (which uses `consult_maestro` instead), reflection agents (read-only review context), or mechanical agents.

### Budget Tracing

Sub-session entries appear in the calling task's budget trace tagged `agent_name="InterAgentSession"` with `ask_depth` in metadata. The full conversation is auditable via `/api/budget-entries`.

---

## 21. Orchestrator LLM & Maestro Escalation

### Orchestrator LLM (`maestro_llm_id`)

Several advanced features — autopilot objective assessment (section 13), ConsultAgent (below), reflection LLM resolution (section 16), episodic summary generation (section 15) — use a dedicated *orchestrator* LLM rather than the task's worker LLM.

Configure it in `maestro.ini`:

```ini
[orchestration]
maestro_llm_id =              ; LLM record ID to use for Maestro-mode operations
                               ; blank = fall back to project default LLM
consult_max_calls_per_session = 3
ask_max_depth = 3
```

Or set it per-project via **Project Settings → Orchestrator LLM** (the same dropdown as the worker LLM picker). Per-project setting takes priority over the ini.

### `consult_maestro` — Agent Escalation Tool

When a worker agent encounters a question that requires architectural judgment — ambiguous interface contracts, conflicting requirements, repeated tooling failures — it can escalate to Maestro mid-session:

```python
answer = consult_maestro(
    question="The task says to use REST but the arch doc specifies GraphQL. Which should I implement?"
)
```

A **ConsultAgent** spins up synchronously: it loads all arch cards and document store titles as context, can call read-only project tools (`get_document`, `list_tasks`, `get_task_description`), reasons over the question, and returns an answer. The calling agent receives the answer as a normal tool result and continues.

**Behaviour:**
- The calling session never pauses visibly — escalation and response happen within a single LLM turn
- ConsultAgent uses the orchestrator LLM (`maestro_llm_id`), not the worker LLM
- Per-session call cap: once `consult_max_calls_per_session` is reached, further calls return an error instructing the agent to use its best judgment
- Maestro cannot call `consult_maestro` itself (excluded from its tool list)
- Budget for ConsultAgent sessions is charged to the task, tagged `agent_name="ConsultAgent"`

To make `consult_maestro` available in a stage, include it in the stage's tool allowlist in the Pipeline Editor. It does not need to be listed for every stage — only stages where escalation is appropriate.

### Quick Reference — New Endpoints (Sections 13–21)

```
# Objectives
GET/POST   /api/projects/{name}/objectives
PUT/DELETE /api/projects/{name}/objectives/{id}
GET        /api/projects/{name}/objectives/tree
GET        /api/projects/{name}/objectives/{id}/evidence

# Model routing
GET        /api/projects/{name}/routing
PUT        /api/projects/{name}/routing/{stage}
DELETE     /api/projects/{name}/routing/{stage}
GET        /api/projects/{name}/cost-by-model

# Event triggers
POST       /api/events/inbound/{watch_id}

# Training
GET        /api/training/status
POST       /api/training/export
GET        /api/training/metrics
POST       /api/training/checkpoints

# Reflection
POST       /api/tasks/{id}/trigger-reflection
```

---

*Maestro — build it once, let it think forever.*
