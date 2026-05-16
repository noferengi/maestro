# Phase 4 — Litegraph Pipeline Editor

> **Status:** COMPLETE — 2026-05-15  
> **Depends on:** Phase 3 (CRUD API exists); Phase 2 recommended but not blocking  
> **Estimated effort:** 7–9 days  
> **Goal:** A full-canvas pipeline editor at `/pipelines/{id}/edit` using Litegraph.js,
> a slide-in property panel with ⚡ field generation, and a dynamically-derived kanban
> column list that replaces the hardcoded `column_order` in `maestro.ini`.

---

## Technology Decision

**Litegraph.js** — single `<script>` tag, no bundler, no React, no build step.
Fits the existing vanilla JS + static-file serving architecture. Node interiors are
canvas-painted; all rich controls (dropdowns, text areas, checkboxes) live in the
slide-in property panel, which is standard HTML.

CDN or vendored: copy `litegraph.js` and `litegraph.css` into `app/web/vendor/`
and serve statically. No external network dependency at runtime.

---

## Node Type Taxonomy

All nodes are rectangles with the same shape. Differentiation is by user-assigned
color and by port count/type. Litegraph port types are color-coded; we define:

| Port type key | Color   | Meaning |
|---------------|---------|---------|
| `task`        | #60a5fa | A card/task token flowing through the pipeline |
| `condition`   | #f59e0b | An exit condition (pass/fail/reject) |
| `data`        | #34d399 | A specific content blob key |

**Stage node** — the primary node type. Represents one pipeline stage.
- 1 input port (`task` type)
- 1–N output ports, one per outgoing edge condition (`condition` type)
- Canvas label: stage label (user-defined, truncated to ~20 chars)

**Factory node** — produces cards from an external data source.
- 0 input ports (or optional trigger port for predecessor-completion trigger)
- 1 output port (`task` type) — represents the stream of created cards
- Canvas label: "Factory: {source_type}"

**Conditional node** — branches based on a content blob key value.
- 1 input port (`task` + `data` type)
- 2–N output ports labeled with branch values
- Canvas label: "If: {key}"

**Judgment gate node** — fan-in: receives N attempts, selects the best.
- N input ports (all `task` type)
- 1 output port (`task` type)
- Canvas label: "Best of {N}"

**Fan-out node** — produces N parallel attempt cards from one input card.
- 1 input port (`task` type)
- 1 output port (`task` type, representing the N spawned cards)
- Canvas label: "×{N} Attempts"

**Human gate node** — blocks until human approval or Maestro autopilot.
- 1 input port, 2 output ports (`approve`, `reject`)
- Canvas label: "Human Gate"

All node types share the same Litegraph node base class. A `node_type` property
on the node data drives which property panel template to show.

---

## Canvas Behavior

### Edge rendering
- Forward edges (pass/always): solid line
- Back-edges (fail/reject): dashed line, curved, rendered in a distinct color
  (e.g. amber) so loops are visually distinct from forward progress wires
- Litegraph supports custom link rendering via `LGraph.prototype.onDrawLinkTooltip`
  and link color per output port — use output port color to drive edge style

### Interaction
- Double-click node → open property panel (slide in from right)
- Click empty canvas + drag → pan
- Scroll wheel → zoom
- Drag from output port → draw edge; dropping on an input port creates a transition
- Right-click node → context menu: Rename, Duplicate, Delete, Set Color
- Right-click edge → context menu: Set Condition, Set Priority, Delete
- Drag-select multiple nodes → group them (creates/assigns a `pipeline_stage_group`)
- `Delete` key → remove selected nodes or edges

### Auto-layout
A "Tidy Layout" button runs a left-to-right topological sort and repositions all
nodes with even spacing. Back-edges curve above the node row. Useful after importing
a template or building a complex graph.

### Simulation mode
A "Simulate" button (top bar) steps a ghost token through the graph using the
`pass` condition at each stage, highlighting the forward path. Useful for verifying
pipeline topology before deploying it to a project.

---

## Property Panel

Rendered as a right-side drawer (`#pipeline-property-panel`) in standard HTML.
Panels are template-based: a different `<template>` element per node type.

### Stage node panel

```
┌──────────────────────────────────────────────────┐
│ Stage Properties                          [Close] │
├──────────────────────────────────────────────────┤
│ Stage key (read-only):  outline                  │
│                                                  │
│ Display label           [Outline        ] ⚡     │
│ Agent type              [planning_agent ▼] ⚡    │
│ Color                   [#1e40af ████   ]        │
│                                                  │
│ Intent                                           │
│ ┌────────────────────────────────────────┐ ⚡   │
│ │ Produce a chapter outline from idea    │       │
│ └────────────────────────────────────────┘       │
│                                                  │
│ System prompt                                    │
│ ┌────────────────────────────────────────┐ ⚡   │
│ │ You are an expert story planner...     │       │
│ └────────────────────────────────────────┘       │
│                                                  │
│ Gate type    [llm_judge ▼]                       │
│ Max retries  [3          ]                       │
│ Verifier     [none       ▼]                      │
│                                                  │
│ Tools allowed                                    │
│   ☑ read_file     ☑ write_file   ☐ web_search   │
│   ☐ run_pytest    ☐ run_math_kernel              │
│                                                  │
│ Required input keys  [outline, premise   ] ⚡   │
│ Output keys          [outline             ] ⚡   │
│                                                  │
│ Upstream task gate   [None               ▼]      │
│ Arch category context                            │
│   ☑ characters   ☑ themes   ☐ plot              │
│                                                  │
│                              [Save]  [Revert]    │
└──────────────────────────────────────────────────┘
```

### Factory node panel (separate template, see Phase 9)

### ⚡ button behavior

Each ⚡ button is a `<button class="lightning-btn" data-field="system_prompt">`.
Click handler:
1. Reads all current panel field values into `node_state`
2. Reads graph context from the Litegraph graph object (predecessor/successor node
   labels, edge conditions in/out)
3. POSTs to `POST /api/pipelines/generate-field`
4. Streams the response into the field's `<textarea>` / `<input>` progressively
5. On complete, marks the field as "AI-generated" with a subtle indicator (small
   ✦ icon) that the user can dismiss by editing the field

---

## Kanban Board Integration

`app/web/kanban.js` currently builds columns from the hardcoded `column_order`
in `maestro.ini`. After this phase:

1. On `loadTasksFromDatabase()`, also fetch `GET /api/pipelines/{template_id}`
   for the project's assigned template.
2. Build `columnDefs` from `pipeline_stages` ordered by `position`, grouped by
   `group_id`.
3. Stage groups render as visual brackets with the group name as a header row.
4. `columnDefs` replaces the hardcoded column list in `renderTasksFromDatabase()`.
5. `maestro.ini [pipeline] column_order` is retained as a fallback for projects
   with no `pipeline_template_id` (transition period safety net, removed in a
   future cleanup).

---

## Route

```
GET /pipelines                    — template gallery (Phase 8)
GET /pipelines/{id}/edit          — canvas editor (this phase)
GET /pipelines/new                — create new blank template, redirect to editor
```

These routes are served by FastAPI returning static HTML; the canvas and property
panel are client-side JS only. No server-side rendering.

---

## File Layout

```
app/web/
  vendor/
    litegraph.js          # vendored, pinned version
    litegraph.css
  pipeline_editor.html    # canvas page template
  pipeline_editor.js      # editor logic: graph init, node types, property panel
  pipeline_editor.css     # panel styles, edge color overrides
```

---

## Test Criteria

- Navigate to `/pipelines/{id}/edit` for the Software Development template — all
  stages render as nodes, all transitions render as edges, back-edges are dashed
- Drag a new stage node onto the canvas, fill Intent, click ⚡ on system prompt →
  field populates via the generate-field endpoint
- Draw an edge from `indev` output to `planning` input with condition `reject` →
  back-edge renders dashed in amber
- Save → `GET /api/pipelines/{id}` reflects the new edge in the DB
- Load the kanban board for a project using this template → columns match the
  template's stage list in order, with the Optimization+Security group bracketed
- Delete a stage with a redirect → tasks migrate, node disappears from canvas

---

## Risk Factors

**Litegraph serialization format** — Litegraph stores graph state as its own JSON
schema (`graph.serialize()`). This is not the same as the `pipeline_templates` DB
schema. A translation layer is required: on load, build a Litegraph graph from DB
rows; on save, parse the Litegraph graph back to DB upserts. Keep this translation
in `pipeline_editor.js`; do not let Litegraph's internal format leak into the API.

**Back-edge rendering** — Litegraph does not natively distinguish forward and
back-edges visually. Override `LLink` drawing: after graph layout, detect edges
where `to_stage.position < from_stage.position` and render them with a dashed
stroke and a different color. This requires hooking `LGraph.prototype.onAfterChange`.

**Canvas performance** — for large templates (30+ nodes), Litegraph's default
canvas renderer is fine. If performance degrades, enable Litegraph's WebGL renderer
(`graph.use_webgl = true`).

---

## Implementation Audit (2026-05-15)

### What was delivered

`pipeline_editor.html` / `pipeline_editor.js` (1371 lines) / `pipeline_editor.css`
are all present. All six node types are implemented with correct port counts and
property panel templates. Back-edge detection and dashed amber rendering works.
The property panel has ⚡ streaming generation on all specified fields. Tidy Layout
uses Kahn's algorithm with back-edge exclusion. Simulation mode steps a ghost token
through pass-condition edges. The save/load cycle round-trips correctly via the Phase 3
API. `gallery.html` (512 lines) delivers the browsable template grid with clone,
export, import, and assignment.

### Known defects and gaps

**1. Litegraph.js not vendored ✅ FIXED 2026-05-15**
`litegraph.js` (1049 KiB) downloaded to `app/web/vendor/` via `scripts/download_vendor.py`.
Both `litegraph.js` and `litegraph.css` are now self-hosted. CDN fallback in the HTML
can be removed in a cleanup pass.

**2. Kanban column list still hardcoded (high impact)**
`kanban.js` has a hardcoded `columns` array:
```javascript
const columns = ['idea','planning','indev','conceptual_review','optimization',
                 'security','final_review','human_review','completed'];
```
`applyPipelineTemplateLayout()` only changes CSS `order` on existing DOM columns;
it does not create or remove columns based on template stage topology. Tasks assigned
to stages outside this list have no visible column on the board. This is the most
significant functional gap for non-software pipelines.

**Fix:** Replace the hardcoded array with a derivation from `activePipelineTemplate.stages`
at render time, creating or removing `.kanban-column` DOM elements as needed.

**3. Gallery endpoint mismatch (runtime breakage)**
`gallery.html` line 407 calls `POST /api/projects/{name}/use-template` but the actual
route registered in `main.py` is `POST /api/projects/{name}/pipeline`. The "Use"
button on every template card will return 404 at runtime. Fix: either add a
`/use-template` alias in `main.py` or update `gallery.html` to use `/pipeline`.

**4. No visual group representation on canvas**
Stage groups are stored in the DB and rendered as bracket headers in the kanban, but
the Litegraph canvas has no group bounding boxes or swimlane coloring. Planned behavior
was "drag-select → group them." Drag-select exists but does not create a group record.
