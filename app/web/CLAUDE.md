# app/web — Frontend Overview

All files in this directory are served as static files by FastAPI from the `/static/` route
(configured in `app/main.py`). No build step — plain HTML, CSS, and vanilla JS.

---

## Pages

### Board (`index.html` + `kanban.js` + `style.css`)

The main Kanban board. Five columns: IDEA → PLANNING → INDEV → CONCEPTUAL_REVIEW →
OPTIMIZATION → SECURITY → FULL_REVIEW → COMPLETED. Tasks are draggable within a column
to reorder. Column transitions are gated by the backend intake pipeline.

**`index.html`** — Board shell. Project tabs, five column containers, seven modals (task
create/edit, new project, edit project, LLM endpoints, budgets, tools).

**`kanban.js`** — All board behaviour (~2 000 lines, monolithic). Key globals:
- `taskData`, `allTasks`, `currentProject` — task state
- `allLlms`, `allBudgets` — endpoint/budget dropdowns
- `transitionCache`, `transitionPollers` — intake pipeline polling
- `_modalMousedownTarget` — drag-close fix (global mousedown listener, all 7 modals)

Key patterns:
- `loadTasksFromDatabase()` — re-fetches and fully rebuilds on project switch
- `renderTasksFromDatabase()` — groups by type, sorts by `position`, appends cards
- Drag-and-drop POSTs to `/api/tasks/{id}/reorder`, then re-fetches before re-rendering
- 5-second auto-refresh via `setInterval`

**`style.css`** — All board styles (~900 lines, monolithic).

If you need to modify the board, edit `kanban.js`. If you need to modify board styles,
edit `style.css`.

---

### Diagnostics (`diagnostics.html` + `diag-*.js` + `diagnostics.css`)

A standalone three-panel LLM conversation viewer at `/diagnostics`. Shows every LLM call
recorded in `budget_entries` grouped by task and session.

**`diagnostics.html`** — Page shell. Three panels: task list (left), entry timeline
(middle), conversation detail (right). Loads the five `diag-*.js` files in order.

**`diagnostics.css`** — All diagnostics styles. Edit here for layout, colours, new
CSS classes on the diagnostics page.

---

## Diagnostics JS — File Map

The original `diagnostics.js` monolith was split into five files. They share global state
defined in `diag-utils.js`. Load order matters — each file depends on the ones before it.

```
diag-utils.js       ← load first
diag-tasks.js       ← depends on diag-utils.js
diag-entries.js     ← depends on diag-utils.js, diag-tasks.js
diag-session.js     ← depends on diag-utils.js, diag-entries.js
diag-render.js      ← depends on diag-utils.js, diag-session.js  ← load last
```

### `diag-utils.js` — Shared state and pure helpers

All global `let` variables live here. Every other file reads/writes them.

| Symbol | Purpose |
|---|---|
| `selectedTaskId`, `selectedEntryId` | Currently selected task / entry |
| `allDiagTasks` | Task list from `GET /api/diagnostics/tasks` |
| `allDiagLlms` | `id → {name, max_context}` map from `GET /api/llms` |
| `currentEntries` | Lightweight entries for selected task (ascending) |
| `currentSessions` | Output of `detectSessions()` — array of entry groups |
| `cachedSession` | `{ groupKey, fullEntries, boundaries }` — avoids re-fetching |
| `renderedSessionKey` | `groupKey` of what is currently rendered in the DOM |

Pure utility functions (no DOM access):
- `escapeHtml(str)` — XSS-safe HTML escaping
- `fmtTokens(n)` — 1024-based formatting (K/M)
- `formatTimestamp(isoStr)` — locale-formatted date/time
- `labelEntry(systemContent)` — classify entry type from first system message text

**Edit this file when:** adding a new global, changing token formatting, or changing
entry type classification keywords.

---

### `diag-tasks.js` — Left panel: task list

Populates the left panel with tasks that have LLM activity.

| Function | What it does |
|---|---|
| `loadTasks()` | Fetches `/api/diagnostics/tasks` and `/api/llms`; populates `allDiagTasks` and `allDiagLlms`; calls `renderTaskList()` |
| `renderTaskList(tasks)` | Renders task cards with title, type badge, call count, token total |
| `filterTasks(query)` | Filters `allDiagTasks` by title/id; called by the search input `oninput` |

**Edit this file when:** changing task card appearance, adding columns to the task list,
or changing what data is fetched on page load.

---

### `diag-entries.js` — Middle panel: entry timeline and task summary

Populates the middle panel when a task is selected, and the right panel task summary view.

| Function | What it does |
|---|---|
| `selectTask(taskId)` | Fetches `/api/budget-entries?task_id=…`; calls `detectSessions()`, `renderEntryList()`, `renderTaskSummary()` |
| `renderTaskSummary(taskId)` | Renders per-session aggregate table in right panel (initial view before an entry is selected) |
| `detectSessions(entries)` | Groups ascending entries into sessions: new session when `prompt_cost` drops or time gap > 5 min |
| `renderEntryList(sessions)` | Renders session groups with per-entry dots, token counts, tool call badges |

**Edit this file when:** changing session detection heuristics, changing the entry list
card layout, or changing what the task-level summary table shows.

---

### `diag-session.js` — Entry selection, turn summary table, DOM navigation

Handles the three fetch paths when a user clicks an entry, and the per-turn summary table
that appears above the conversation.

| Function | What it does |
|---|---|
| `groupMessages(messages)` | Collapses `[assistant + tool…]` runs into `tool_group` objects for grouped rendering |
| `renderToolGroup(msgs, startIndex, highlighted)` | Wraps a tool call + results in `.diag-tool-group` |
| `buildSessionSummary(anchorEntryId)` | Builds the sticky per-turn table (# / Entry / Finish / LLM / Calls / Prompt / Δ Prompt / Ctx% / Generated / Total / Cache / PP$ / TG$ / Total$) |
| `selectEntry(entryId)` | Three-path fetch logic: Path 1 = DOM-only jump (accumulating), Path 2 = re-render from cache, Path 3 = full fetch |
| `jumpToEntry(entryId, sessionGroup)` | DOM-only: re-highlights messages, swaps anchor divider, scrolls. No fetch. Only called from Path 1. |

**Edit this file when:** adding columns to the turn summary table, changing session fetch
logic, or changing how DOM-only navigation works.

**To add a column to the turn summary table:** edit `buildSessionSummary()` — add the
`<th>` to `<thead>`, the `<td>` to the row template, and update `colspan` in `<tfoot>`.

---

### `diag-render.js` — Right panel: conversation and message rendering

Renders the full conversation view in the right panel. Also contains the context-window
usage bar, macOS Dock-style magnification, UI toggle handlers, and the `DOMContentLoaded`
init call.

| Function | What it does |
|---|---|
| `_msgCharLen(msg)` | Estimate character length of a message object (for proportioning token deltas by content type) |
| `buildCtxBar(entryId)` | Build a context-window usage bar for a turn divider. Flat flex segments (base/asst/tool/free) with `flex-grow` = token count. Each coloured segment carries a `data-label` attribute shown on hover via CSS `::after` overlay (entry ID, call tokens, delta, pp, tg, cost). |
| `renderConversation(entry, highlightFrom, anchorEntryId, selectedFull, sessionBoundaries)` | Main render: builds the conversation header, calls `buildSessionSummary()`, iterates grouped messages with turn dividers, appends `[RESPONSE]` block |
| `renderMessage(msg, index, highlighted)` | Renders a single message bubble (system/user/assistant/tool). Tool results are collapsible. |
| `renderToolCall(tc)` | Renders a single tool call block with name, args, collapsible call ID |
| `renderSystemWarning(content)` | Renders `[SYSTEM]` injected messages as coloured warning banners |
| `toggleToolResult(bodyId, header)` | Expand/collapse tool result body |
| `toggleReasoning(el)` | Expand/collapse reasoning block |
| `_initDockZoom()` (IIFE) | macOS Dock-style magnification on `.ctx-bar` segments. Uses event delegation (`mousemove` on `document`). Cosine falloff: 5× peak scale at cursor, decaying to 1× over 24px (~¼ inch). `transform-origin: bottom center` — segments grow upward. z-index tracks scale. Bar gets `.dock-zooming` class (overflow visible) during interaction. |

**Edit this file when:** changing how individual messages look, adding new message role
types, changing the conversation header layout, changing the `[RESPONSE]` block, or
tuning the Dock zoom parameters (`MAX_SCALE`, `INFLUENCE_PX`).

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
| `maestro_loop` | `#198754` (green) |
| `unknown` | `#6c757d` (gray) |

### System warning banners

Applied to `.diag-system-warn`:
- `.warn-turns` — yellow, turn exhaustion warnings
- `.warn-critical` — red, forced/critical events
- `.warn-context` — orange, context window warnings

### Context bar (`.ctx-bar` + `.ctx-seg`)

- `.ctx-bar` — flex container representing the full context window; `align-items: flex-end`
- `.ctx-bar.dock-zooming` — added during Dock magnification; `overflow: visible`, `z-index: 10`
- `.ctx-seg` — individual segment; `flex-grow` = token count; `transform-origin: bottom center`
- `.ctx-seg-base` — gray, initial system/task context (turn 0)
- `.ctx-seg-asst` — purple, assistant generation tokens carried forward
- `.ctx-seg-tool` — teal, tool results / external input
- `.ctx-seg-current` — vivid variant of above for the selected turn
- `.ctx-seg-free` — transparent, remaining context capacity (excluded from hover/zoom)
- `.ctx-seg-gap` — 3px left margin on first segment of each new turn
- `.ctx-seg:hover` — 2px yellow box-shadow + `::after` overlay label from `data-label`

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
