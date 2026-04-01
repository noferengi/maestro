# app/web — Frontend Overview

All files in this directory are served as static files by FastAPI from the `/static/` route
(configured in `app/main.py`). No build step — plain HTML, CSS, and vanilla JS.

---

## Pages

### Board (`index.html` + `kanban.js` + `style.css`)

The main Kanban board. Layout: a **`#arch-bar`** horizontal band spanning full width at the top, followed by eight pipeline columns: IDEAS → PLANNING → INDEV → CONCEPTUAL_REVIEW → OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED. Tasks are draggable within a column to reorder. Column transitions are gated by the backend intake pipeline. Clicking a column header opens the **Column Map View**.

**`index.html`** — Board shell. Project tabs, the `#arch-bar` architecture bar, eight pipeline column containers, the Column Map overlay (`#column-map-container`), nine modals (task create/edit, new project, edit project, transition, LLM endpoints, budgets, tools, compute nodes). Both New Project and Edit Project modals have **Default LLM** and **Budget** dropdowns. The task create/edit modal has shared `#arch-category` and `#arch-priority` selects for architecture cards (shown/hidden by `showArchContentFields()`).

**`kanban.js`** — All board behaviour. Key globals:
- `taskData`, `allTasks`, `currentProject` — task state
- `allLlms`, `allBudgets`, `allProjects` — endpoint/budget/project caches; `allProjects` is `[{name, path, description, llm_id, budget_id}]`
- `ARCH_CATEGORY_COLORS` — `{category: hexColor}` map for the 14 architecture card category badges
- `_archBarCollapsed` — boolean persisted in `localStorage`; drives `#arch-bar.collapsed` CSS class
- `transitionCache`, `transitionPollers` — intake pipeline polling
- `columnMapActive`, `columnMapType` — Column Map View active flag and which column
- `_mapCurrentEdges`, `_mapCurrentNodePositions`, `_mapCurrentColor`, `_mapOffsetX/Y` — shared map render state
- `_mapNodeDrag` — drag state (`{active, nodeId, startX, startY, origPositions}`)
- `_viewChildrenState`, `_childrenPollerTimer` — subdivision View Children / regen polling state
- `currentBigIdeaFilter`, `breadcrumbStack`, `descendantIndex` — Big Idea zoom navigation state
- `_modalMousedownTarget` — drag-close fix (global mousedown listener, all modals)

Key patterns:
- `loadTasksFromDatabase()` — re-fetches and fully rebuilds on project switch
- `renderTasksFromDatabase()` — groups pipeline tasks by type, sorts by `position`, appends cards; then calls `renderArchBar()`. Architecture tasks (`type='architecture'`) are **not** in the pipeline columns array and never rendered there.
- `renderArchBar()` — rebuilds `.arch-card` elements in `#arch-cards` from arch tasks in `taskData`, sorted by priority then position
- `toggleArchBar()` — flips `_archBarCollapsed`, saves to `localStorage`, toggles `#arch-bar.collapsed`
- `reconcile()` — 5-second auto-refresh; skips DOM when `columnMapActive` is true; arch tasks are tracked via `fingerprintCache` but not added to `cardCache`; calls `renderArchBar()` when any arch fingerprint changes
- `deleteTask()` — detects `task.type === 'architecture'` and calls `renderArchBar()` instead of searching for a `.task-card` DOM element
- `showArchContentFields(targetStatus)` — shows/hides `#arch-category`/`#arch-priority` selects and hides LLM/budget/owner/tags for architecture type; relabels description field
- Drag-and-drop POSTs to `/api/tasks/{id}/reorder`, then re-fetches before re-rendering
- New task default LLM is pre-populated from the current project's `llm_id`
- `populateProjectLlmSelect(elementId, selectedId)` and `populateProjectBudgetSelect(elementId, selectedId)` — shared helpers for project-level dropdowns

#### Architecture Bar (`#arch-bar`)

A dark navy band spanning the full board width above the pipeline columns. Architecture cards are stored as `type='architecture'` tasks in the DB and rendered **only** here — never in kanban columns.

**Card schema** (`content` JSON field):
- `category` — one of 14 fixed values: `Platform`, `Design`, `Testing`, `Security`, `Performance`, `API`, `Tooling`, `Data`, `UX`, `Accessibility`, `Compliance`, `Deployment`, `Observability`, `General`
- `priority` — `critical` | `high` | `normal` | `low`; controls injection order in agent context and left-border stripe colour on the card
- Card body text is the task's `description` field

Each card shows a coloured category badge (`ARCH_CATEGORY_COLORS`), title, 3-line body excerpt, and a priority stripe (red=critical, orange=high, blue=normal, grey=low). Hover reveals Edit/Del buttons. The bar is collapsible; state persists in `localStorage`.

**Agent injection** — `build_architecture_context(project_name, agent_type)` in `project_snapshot.py` fetches arch cards and formats them as a `== PROJECT ARCHITECTURE & CONSTRAINTS ==` block. `ARCH_CATEGORY_RELEVANCE` maps agent type to a category set filter:

| Agent | Categories |
|---|---|
| `loop` (implementation) | all |
| `research` | all |
| `subdivision` | Platform, Design, Testing, Performance, API, Data, Tooling, General |
| `conceptual_review` | Design, API, Data, Security, Accessibility, Compliance, General |
| `security` | Security, Compliance, API, Data, Platform, General |
| `optimization` | Performance, Platform, Data, Observability, Tooling, General |
| `file_summary` | Platform, Tooling, Data, General |

#### Column Map View

Clicking any column header or empty column whitespace opens a full-screen 2D radial
canvas showing tasks as cards connected by thick cubic-bezier arrows.

Key functions:
- `openColumnMap(colType)` / `closeColumnMap()` — show/hide; hides `.kanban-board`, shows `#column-map-container`
- `handleColumnClick(e, colType)` / `handleTasksContainerClick(e, colType)` — click guards
- `_mapComputeLayout(tasks, colType)` — three-phase layout: (1) load saved `map_x/map_y`; (2) BFS fan-out for newly-subdivided children; (3) radial `placeSubtree()` for unpositioned nodes. IDEAS use `parent_task_id` hierarchy; all others use `prerequisites`. Architecture tasks have no column map (they live in the arch bar).
- `renderColumnMap(colType)` — computes bounding box, sets canvas dimensions, renders `.map-node` divs, calls `_mapRedrawArrows()`, saves newly-positioned nodes
- `_mapRedrawArrows()` — removes/redraws all SVG `<path>` bezier arrows; uses `_mapCardEdge()` for edge-to-edge routing
- `_mapStartNodeDrag(e, nodeId)` — group drag: parent + all descendants move by the same delta
- `_mapSavePositions(toSave)` — async fire-and-forget `PATCH /api/tasks/map-positions`
- `setupMapInteraction()` / `teardownMapInteraction()` — pan (canvas drag) + zoom (scroll) on `#column-map-scroll-wrap`

Positions are in **layout-space** (centred around 0), not canvas-space. Canvas position = layout + `(_mapOffsetX, _mapOffsetY)`. Offset is recomputed from the bounding box on each render — saved positions are stable across sessions.

#### View Children (Subdivision Sets)

"View Children" on a Big Idea task opens the transition modal showing all subdivision sets
as a paginated collection (← older · N of M · newer →).

- **Active set** — the set currently feeding child tasks. Non-active sets show **"Activate this set"** in the footer.
- **Regeneration** — "Regenerate" keeps the modal open, injects a synthetic `{status: 'generating'}` placeholder as set 1, starts `_startChildrenPoller(taskId)`. The poller (500ms) watches `GET /api/tasks/{id}/subdivision-records` until the newest record leaves `generating` status, then stops and re-renders.
- `_viewChildrenState = { taskId, records, childMap, idx }` — records sorted newest-first.
- `_childrenPollerTimer` — `setInterval` ID; stopped by `_stopChildrenPoller()`.

**`style.css`** — All board styles.

---

### Diagnostics (`diagnostics.html` + `diag-*.js` + `diagnostics.css`)

A standalone three-panel LLM conversation viewer at `/diagnostics`. Shows every LLM call
recorded in `budget_entries`, grouped by task and session.

**`diagnostics.html`** — Page shell. Three panels: task list (left), entry timeline
(middle), conversation detail (right). Loads the five `diag-*.js` files in dependency order.

**`diagnostics.css`** — All diagnostics styles. Edit here for layout, colours, new CSS
classes on the diagnostics page.

---

## Diagnostics JS — File Map

The original `diagnostics.js` monolith was split into five files. They share global state
defined in `diag-utils.js`. Load order matters — each file depends on the ones before it.

```
diag-utils.js       ← load first  (globals, constants, pure helpers)
diag-tasks.js       ← depends on diag-utils.js
diag-entries.js     ← depends on diag-utils.js, diag-tasks.js
diag-session.js     ← depends on diag-utils.js, diag-entries.js
diag-render.js      ← depends on diag-utils.js, diag-session.js  ← load last
```

---

### `diag-utils.js` — Shared state, constants, and pure helpers

All global `let` variables live here. Every other file reads/writes them.

| Symbol | Purpose |
|---|---|
| `selectedTaskId`, `selectedEntryId` | Currently selected task / entry |
| `allDiagTasks` | Task list from `GET /api/diagnostics/tasks` |
| `allDiagLlms` | `id → {name, max_context}` map from `GET /api/llms` |
| `currentEntries` | Lightweight entries for selected task (ascending) |
| `currentSessions` | Output of `detectSessions()` — array of session groups |
| `cachedSession` | `{ groupKey, fullEntries, boundaries }` — avoids re-fetching |
| `renderedSessionKey` | `groupKey` of what is currently rendered in the DOM |

Shared constants (all other files read these — do not redeclare):

| Constant | Purpose |
|---|---|
| `TYPE_COLORS` | Agent type → hex colour (surveyor, designer, judge, reviewer, research, pitfall, security, optimization, subdivision, web_agent, maestro_loop, file_summary, unknown) |
| `TOOL_COLORS` | Tool category → hex colour (read, write, list, search, git, shell, task, plan, web, other) |
| `TOOL_CATEGORY_MAP` | Tool function name → category string (e.g. `read_file` → `'read'`) |

Pure utility functions (no DOM access):

| Function | What it does |
|---|---|
| `escapeHtml(str)` | XSS-safe HTML escaping |
| `fmtTokens(n)` | 1024-based formatting (K/M) |
| `formatTimestamp(isoStr)` | Locale-formatted date/time |
| `labelEntry(systemContent)` | Classify entry type from first system message text; returns a key from `TYPE_COLORS` |
| `labelEntryFromUser(userContent)` | Fallback classifier for system-less calls (e.g. file summaries); detects `file_summary` |
| `labelTool(toolName)` | Maps a tool function name to its category via `TOOL_CATEGORY_MAP` |
| `getConceptualTurns(group)` | Builds `[SYSTEM Prompt, USER Prompt, Turn 1…N]` turn objects for a session group; each turn has `{label, entryId, type, msgIdx, entry}` |

**Edit this file when:** adding a new global, changing token formatting, adding an agent type
to `TYPE_COLORS`/`labelEntry`, adding a tool to `TOOL_CATEGORY_MAP`, or changing session-turn logic.

---

### `diag-tasks.js` — Left panel: task list

Populates the left panel with tasks that have LLM activity.

| Function | What it does |
|---|---|
| `loadTasks()` | Fetches `/api/diagnostics/tasks` and `/api/llms`; populates `allDiagTasks` and `allDiagLlms`; calls `renderTaskList()` |
| `renderTaskList(tasks)` | Renders task cards with title, type badge, call count, token total |
| `filterTasks(query)` | Filters `allDiagTasks` by title/id; called by the search input `oninput` |

The synthetic `{id: "__file_summaries__", type: "file_summary"}` row (project prewarm calls with no task) appears at the top of this list when such entries exist.

**Edit this file when:** changing task card appearance, adding columns to the task list,
or changing what data is fetched on page load.

---

### `diag-entries.js` — Middle panel: entry timeline and task summary

Populates the middle panel when a task is selected, and the initial task summary in the right panel.

| Function | What it does |
|---|---|
| `selectTask(taskId)` | Fetches budget entries (uses `task_id=__file_summaries__` for the synthetic task); calls `detectSessions()`, `renderEntryList()`, `renderTaskSummary()` |
| `renderTaskSummary(taskId)` | Renders per-session aggregate table in the right panel |
| `detectSessions(entries)` | Groups ascending entries into sessions: new session when context drops > 15% OR time gap > 10 minutes |
| `renderEntryList(sessions)` | Renders session groups using `getConceptualTurns()` for turn labels; each turn is a clickable row |

**Edit this file when:** changing session detection heuristics, changing the entry list
card layout, or changing what the task-level summary table shows.

---

### `diag-session.js` — Entry selection, turn summary table, DOM navigation

Handles the three fetch paths when a user clicks an entry, and the per-turn summary table.

| Function | What it does |
|---|---|
| `groupMessages(messages, allBoundaries)` | Collapses `[assistant + tool…]` runs into `tool_group` objects; breaks at every conceptual turn boundary |
| `renderToolGroup(msgs, startIndex, highlighted)` | Wraps a tool call + results in `.diag-tool-group` |
| `buildSessionSummary(anchorEntryId)` | Builds the sticky per-turn table (# / Entry / Finish / LLM / Calls / Prompt / Δ Prompt / Ctx% / Generated / Total / Cache / PP$ / TG$ / Total$) |
| `selectEntry(entryId, targetMsgIdx)` | Three-path fetch logic: Path 1 = DOM-only jump (accumulating), Path 2 = re-render from cache, Path 3 = full fetch |
| `jumpToEntry(entryId, sessionGroup)` | DOM-only: re-highlights messages, swaps anchor divider, scrolls. Only called from Path 1. |

**Edit this file when:** adding columns to the turn summary table, changing session fetch
logic, or changing how DOM-only navigation works.

**To add a column to the turn summary table:** edit `buildSessionSummary()` — add the
`<th>` to `<thead>`, the `<td>` to the row template, and update `colspan` in `<tfoot>`.

---

### `diag-render.js` — Right panel: conversation and message rendering

Renders the full conversation view. Contains the context-window usage bar, JS tooltip,
UI toggle handlers, and the `DOMContentLoaded` init call.

> **Note:** The macOS Dock-style `_initDockZoom()` IIFE (cosine-falloff neighbor
> magnification, `MAX_SCALE`, `INFLUENCE_PX`, `.dock-zooming`) **has been removed**.
> Segment hover interaction is now handled by CSS + a separate JS tooltip IIFE.

| Function | What it does |
|---|---|
| `_msgCharLen(msg)` | Estimates character length of a message for proportioning token deltas |
| `buildCtxBar(entryId)` | Builds the context-window usage bar for a turn divider. Turn 0 is a single merged setup segment (`.ctx-seg-merged-setup`); turns 1…N are individually coloured by **tool category** (inline `background-color` from `TOOL_COLORS`). Free-space segment shows remaining tokens. |
| `renderConversation(entry, highlightFrom, anchorEntryId, selectedFull, sessionBoundaries, targetMsgIdx)` | Main render: conversation header, `buildSessionSummary()`, grouped messages with turn dividers, `[RESPONSE]` block |
| `renderMessage(msg, index, highlighted)` | Renders a single message bubble (system/user/assistant/tool). Tool results are collapsible. |
| `renderToolCall(tc)` | Renders a single tool call block with name, args, collapsible call ID |
| `renderSystemWarning(content)` | Renders `[SYSTEM]` injected messages as coloured warning banners |
| `toggleToolResult(bodyId, header)` | Expand/collapse tool result body |
| `toggleReasoning(el)` | Expand/collapse reasoning block |
| `_initCtxTooltip()` (IIFE) | Hover tooltip for context-bar segments. Uses event delegation (`mousemove` on `document`). Shows a fixed JS-positioned panel above the cursor: agent-type badge (colour from `TYPE_COLORS`), context % used, tool call name and args. Segments are also **clickable** — clicking a segment calls `selectEntry(fe.id)` to jump to that turn. |

**Edit this file when:** changing how individual messages look, adding new message role
types, changing the conversation header, changing the `[RESPONSE]` block, or modifying
tooltip content.

**Context-bar colours** come from `TOOL_COLORS` in `diag-utils.js` and are set as inline
`background-color` on each segment. To add a new tool-category colour, edit `TOOL_COLORS`
and `TOOL_CATEGORY_MAP` in `diag-utils.js`.

---

## CSS classes — quick reference

### Entry type colours (dot + label badge)

Applied as `type-{name}` on `.diag-entry-dot` and `.diag-conv-type-label`:

| Type | Colour |
|---|---|
| `surveyor` | `#0d6efd` (blue) |
| `designer` | `#6f42c1` (purple) |
| `reviewer` | `#20c997` (teal) |
| `judge` | `#fd7e14` (orange) |
| `research` | `#ffc107` (yellow, dark text) |
| `pitfall` | `#e83e8c` (pink) |
| `security` | `#dc3545` (red) |
| `optimization` | `#fd7e14` (orange) |
| `subdivision` | `#6610f2` (indigo) |
| `maestro_loop` | `#198754` (green) |
| `file_summary` | `#17a2b8` (cyan) |
| `web_agent` | `#d946ef` (fuchsia) |
| `unknown` | `#6c757d` (gray) |

### System warning banners

Applied to `.diag-system-warn`:
- `.warn-turns` — yellow, turn exhaustion warnings
- `.warn-critical` — red, forced/critical events
- `.warn-context` — orange, context window warnings

### Context bar (`.ctx-bar` + `.ctx-seg`)

The bar is a flat flexbox representing the full `max_context` window. Segments are
proportional to token counts via `flex-grow`. The bar scrolls horizontally (`overflow-x: auto`)
rather than squeezing when turns are too narrow.

| Class | Purpose |
|---|---|
| `.ctx-bar` | Flex container; `height: 32px`; `overflow-x: auto`; scrollbar hidden |
| `.ctx-seg` | Individual segment; `min-width: 12px`; `flex-basis: 0`; colour set by inline `background-color` |
| `.ctx-seg:not(.ctx-seg-free):hover` | Pure CSS hover: `scaleY(1.15)` upward pop + `box-shadow: 0 0 0 2px #ffc107`. No JS scaling. |
| `.ctx-seg-merged-setup` | Turn 0 merged system+user prompt segment (contains two inner divs for proportional colouring) |
| `.ctx-seg-current` | Applied to the segment matching the selected turn |
| `.ctx-seg-free` | White, transparent-ish; remaining context capacity; not hoverable; contains `.ctx-free-label` |
| `.ctx-seg-gap` | `margin-left: 0` — segments are flush; class kept for legacy compatibility |
| `#ctx-tooltip` | Fixed-position JS-driven tooltip; dark panel, yellow border; shown on mousemove over segments |
| `.ctx-tip-agent` | Coloured agent-type badge inside tooltip |
| `.ctx-tip-stats` | Muted token count line in tooltip |
| `.ctx-tip-tool` | Tool call name/args lines in tooltip |
| `.ctx-tip-arg` | Indented arg lines within the tooltip |

> **Removed:** `.ctx-bar.dock-zooming` (overflow-visible class during Dock magnification),
> `MAX_SCALE`, `INFLUENCE_PX` JS constants, all cosine-falloff scaling logic.

### Turn table helpers

- `.col-r` — right-align cell
- `.col-dim` — muted gray text (secondary data)
- `.col-bold` — bold (totals)
- `.col-llm` — truncated LLM name column
- `.diag-turn-anchor-row` — blue-tinted row for the selected turn

---

## Versioning

HTML loads scripts and CSS with `?v=N` cache-busting query strings. When changing a file,
increment its version in `diagnostics.html` (or `index.html` for board files) so browsers
pick up the update. The backend serves files verbatim — there is no asset pipeline.
