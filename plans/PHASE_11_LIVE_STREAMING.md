# Phase 11 — Live Streaming, Physics Kanban, and Math Proof Trees

> Phase 11 picks up after the malleable pipeline system (Phase 10) and adds
> three distinct capability tiers: real-time observability, spatial visualization,
> and rigorous mathematical decomposition.

---

## Feature 2 — Physics-Based Parent/Child Kanban

**Goal:** Make the Column Map feel alive. Cards should settle into natural
clusters driven by relationship forces, not deterministic BFS placement.

### What exists
- Column Map: SVG arrows + DOM card nodes, BFS `placeSubtree()` layout,
  positions persisted to `task.map_x / map_y`.
- `descendantIndex` and `childIndex` fully built on load.
- Pan/zoom via `mapTransform` CSS translate+scale.

### What to build

**Verlet spring simulation (runs in `requestAnimationFrame`):**
- Parent → child: spring attraction, rest length ∝ depth level
- Siblings: soft repulsion (inverse-square) to prevent overlap
- Stage attractor: cards in the same pipeline stage are pulled toward a
  "stage centroid" so the layout stays readable without hard column constraints
- Completed/cancelled nodes: pinned (zero mass), don't participate in simulation
- Toggle: `[⚛ Physics]` button next to existing `[Map]` toggle; saves last mode
  in `localStorage`

**Visual polish:**
- Arrow thickness scales with parent's child count (thicker = more children)
- Card opacity fades for `completed` / `cancelled` cards (0.45)
- Leaf nodes (no children, still active) pulse with a soft CSS animation
- "Heat" coloring: tokens-used drives a subtle warm tint (cool blue → warm amber)

**Position persistence:** On simulation settle (velocity < threshold for 2 s),
auto-save positions to `PATCH /api/tasks/map-positions` — same endpoint as today.

**Implementation files:**
- `app/web/kanban.js` — add `PhysicsSimulation` class (~200 lines),
  `startPhysics()` / `stopPhysics()`, integrate with existing `renderColumnMap()`
- `app/web/style.css` — physics toggle button, pulse animation, heat gradient

---

## Feature 3 — Math Proof Tree Pipeline

**Goal:** Given a mathematical goal (e.g. "explore GPY sieve improvements toward
bounded prime gaps"), decompose it into a DAG of sub-claims, each provable by a
sub-agent, leaf nodes dispatched first, results composed bottom-up.

### What exists
- Math tools: `search_arxiv`, `search_oeis`, `search_mathlib` (in `tools_math.py`)
- Subdivision system: parent/child task DAG, prerequisite edges
- `verifiers.py`: SymPy and Lean 4 stubs (`python_sympy` verifier works;
  `lean4` stub needs implementation)
- GAP 12 plan: `plans/GAP_12_LEAN4_PROOF_DEPTH.md`

### What to build

**1. Math Subdivision Agent** (`app/agent/math_subdivide.py`)
- Replaces generic `SubdivisionAgent` for math tasks
- Output schema: `{claim: str, type: "lemma"|"theorem"|"computation",
  dependencies: [claim_id], verifier: "sympy"|"lean4"|"none"}`
- Ensures dependency edges are DAG-valid before committing child tasks
- Sets prerequisite links in DB so the scheduler dispatches leaf claims first

**2. Lean 4 Verifier** (`app/agent/verifiers.py` — stub → working)
- Calls `lake env lean --stdin` in the `sympy-lean4-sandbox` Docker container
  (already on arcbox, image `sympy-lean4-sandbox:latest`)
- Parses Lean output for `sorry`-free success vs error
- Timeout: 60 s per verification attempt

**3. Proof Tree Pipeline Template**
- New built-in template: "Mathematics / Proof Exploration" with stages:
  - `GOAL_DECOMPOSITION` → MathSubdivisionAgent
  - `CLAIM_RESEARCH` → ResearchAgent (arXiv + Mathlib lookup)
  - `FORMALIZATION` → CustomLLMAgent (Lean 4 statement writing)
  - `VERIFICATION` → SymPy / Lean4 verifier gate
  - `COMPOSITION` → CustomLLMAgent (compose proved lemmas into theorem)
  - `HUMAN_REVIEW`
- Seeded via migration alongside existing templates

**4. Proof-Tree Kanban View**
- Column Map variant: "Proof Tree" mode
- Nodes colored by proof status: `⬜ unproven`, `🟡 in-progress`, `✅ proved`, `❌ failed`
- Dependency arrows styled differently from parent/child arrows (dashed vs solid)
- Tooltip shows Lean/SymPy output for verification nodes

### Twin Prime Conjecture example workflow

```
Goal: "Make progress toward bounded prime gaps (post-Zhang 2013 direction)"
 ├── CLAIM: Reproduce GPY sieve lower bound for prime gaps (computation, SymPy)
 ├── CLAIM: Verify Maynard weights formula for k-tuples (computation, SymPy)
 ├── CLAIM: State and check Zhang's 70M gap result is in Mathlib (research)
 │    └── CLAIM: Confirm `Mathlib.NumberTheory.PrimeCounting` coverage (Lean4)
 ├── CLAIM: Identify tightest published admissible k-tuple (research, arXiv)
 └── COMPOSITION: Summarize frontier, flag open sub-problems, propose next steps
```

This won't prove the conjecture — but it produces a structured, verified map of
the known landscape and identifies the tractable frontier.

---

## Feature 4 — Ambitious Math Goal Interface

**Goal:** Let a user state a high-level mathematical ambition and get back a
living, self-updating proof-tree kanban that tracks progress toward it.

### What to build

**"Goal" card type:**
- Special task variant with `task_type = "math_goal"` and a `goal_statement` field
- Creating a Goal card triggers `MathSubdivisionAgent` automatically (no manual
  intake votes needed — goals bypass the 4-stage intake and go straight to
  decomposition)

**Autopilot integration:**
- When autopilot is enabled and a `math_goal` task reaches `GOAL_DECOMPOSITION`,
  the system self-dispatches the subdivision and sets all leaf claims to
  `CLAIM_RESEARCH` without human input

**Progress rollup:**
- Parent Goal card shows: `N/M claims proved`, token spend, % verified
- Board UI: Goal cards get a special header chip "🎯 Goal" and a progress bar

**Iteration:**
- After `COMPOSITION`, agent proposes 2-3 follow-on claims for the next iteration
- User approves → new child tasks appended → proof tree grows

### Scope note
Feature 4 depends on Feature 3 being stable. The physics kanban (Feature 2)
makes the growing proof tree readable at scale. Build order: 3 → 2 → 4.

---

## Implementation order

| Order | Feature | Effort |
|-------|---------|--------|
| ✅ **Done** | Feature 1 — Live token streaming peek | ~1 day |
| 2 | Feature 2 — Physics kanban | ~1 week |
| 3 | Feature 3 — Math proof tree pipeline | ~2 weeks |
| 4 | Feature 4 — Ambitious goal interface | ~1 week (post-3) |
