# STAGES Implementation Progress

## Status: IN PROGRESS — Context window exhausted, pick up from here

## COMPLETED Files

### Phase 1: Foundation
- [x] `maestro.ini` — 9 new config sections appended (planning, planning_gate, indev, conceptual_review, optimization, security_review, full_review, merge)
- [x] `app/agent/config.py` — All new config constants exported (PLANNING_*, INDEV_*, CONCEPTUAL_REVIEW_*, OPTIMIZATION_*, SECURITY_REVIEW_*, FULL_REVIEW_*, MERGE_*)
- [x] `app/migrations/versions/0012_planning_results.py`
- [x] `app/migrations/versions/0013_component_results.py`
- [x] `app/migrations/versions/0014_optimization_results.py`
- [x] `app/migrations/versions/0015_security_review_merge.py` — 3 tables: security_review_results, full_review_results, merge_records
- [x] `app/migrations/versions/0016_task_demotion_tracking.py` — review_notes, demotion_count, demotion_history on tasks
- [x] `app/database.py` — 6 new models (PlanningResult, ComponentResult, OptimizationResult, SecurityReviewResult, FullReviewResult, MergeRecord) + 3 new Task columns (review_notes, demotion_count, demotion_history) + 14 CRUD functions
- [x] `app/agent/dag.py` — `_TYPE_ORDER` updated to 9 columns, `get_ready_tasks()` skip list updated
- [x] `app/agent/verdicts.py` — Added `CONDITIONAL_PASS` verdict

### Phase 2: Planning Pipeline
- [x] `app/agent/planning.py` — PlanningPipeline (5 stages: survey, best-of-N design, review panel, pitfall detection, consolidation)
- [x] `app/agent/planning_gate.py` — PlanningGate (7 checks, all deterministic except #6 LLM feasibility)

### Phase 3: Development Orchestrator
- [x] `app/agent/component_loop.py` — ComponentLoop + ComponentToolDispatcher (file write containment)
- [x] `app/agent/dev_orchestrator.py` — DevOrchestrator (batch execution, parallel components)

### Phase 4: Review Stages
- [x] `app/agent/conceptual_review.py` — ConceptualReviewPipeline (4 deterministic + 4 LLM reviewers)
- [x] `app/agent/optimization.py` — OptimizationPipeline (profile → propose → vote → implement → verify)
- [x] `app/agent/security_review.py` — SecurityPipeline (3 parallel agents with veto power, allowlisted shell)

## REMAINING Files to Create

### Phase 4 (continued)
- [ ] `app/agent/full_review.py` — FullReviewPipeline (4 parallel reviewer agents: functional, code_quality, integration, ux)
  - Functional: requirements traceability, missing features, scope creep
  - Code Quality: pytest + linting + type checking, naming, dead code
  - Integration: import graph, API breaks, migration validity, full test suite on merge sim
  - UX: accessibility, responsive, visual consistency (only if app/web/ files changed)
  - Uses `run_shell_review` (allowlisted: pytest, ruff, mypy, black --check, npm test, npm run lint)
  - Standard majority tally, research agent available
  - Demotion pathways: Functional→planning, Code Quality→development, Integration→development, UX→development

### Phase 5: Merge + Integration
- [ ] `app/agent/merge.py` — Deterministic git merge (NO LLM):
  1. Verify branch maestro/task-{id} exists
  2. Checkout main, pull latest
  3. Merge --no-ff
  4. Run full test suite (pytest, 5min timeout from MERGE_TEST_TIMEOUT config)
  5. Push to origin (if MERGE_AUTO_PUSH)
  6. Update task type to "completed"
  7. Tag branch: merged/task-{id} (if MERGE_TAG_BRANCHES)
  8. Create MergeRecord audit trail
  - On conflict → abort, demote to development
  - On test failure → reset HEAD~1, demote to development

- [ ] `app/agent/tools.py` — Add these tools:
  - `run_shell_security(command)` — Already implemented in security_review.py but should also be registered in TOOL_SCHEMAS
  - `run_shell_review(command)` — Allowlisted shell for review: pytest, ruff, mypy, black --check, npm test, npm run lint
  - Register both in TOOL_SCHEMAS and TOOL_REGISTRY
  - Optionally add: `validate_dependency_graph(graph)`, `estimate_context_tokens(text)`

- [ ] `app/main.py` — Major updates needed:
  1. Update `_COLUMN_ORDER` to 9 columns: `['architecture', 'idea', 'planning', 'indev', 'conceptual_review', 'optimization', 'security', 'full_review', 'completed']`
  2. Generalize `/api/tasks/{task_id}/advance` endpoint to detect current column and dispatch:
     ```python
     ADVANCE_HANDLERS = {
         "idea":              _run_intake_pipeline,          # existing
         "planning":          _run_planning_pipeline,        # NEW
         "indev":             _run_dev_then_review,          # NEW (dev orchestrator → auto conceptual review)
         "conceptual_review": _advance_to_optimization,      # NEW
         "optimization":      _run_security_pipeline,        # NEW
         "security":          _run_full_review_pipeline,     # NEW
         "full_review":       _execute_merge_to_completed,   # NEW
     }
     ```
  3. Add new status/query endpoints:
     - GET /api/tasks/{id}/planning-result
     - GET /api/tasks/{id}/component-status
     - GET /api/tasks/{id}/optimization-status
     - GET /api/tasks/{id}/security-status
     - GET /api/tasks/{id}/full-review-status
     - GET /api/tasks/{id}/merge-status
     - GET /api/tasks/{id}/audit-trail
  4. Import new models and CRUD functions from database.py
  5. Add new pipeline runner functions (like existing _run_intake_pipeline)
  6. Update task_to_dict() to include new fields (review_notes, demotion_count, demotion_history)
  7. Update AGENT_TOOL_ACCESS dict with new agent types

- [ ] `app/agent/scheduler.py` — Update `_tick()` to route new column types:
  - Currently only auto-dispatches "planning" and "development"
  - Add: "indev" → DevOrchestrator, others stay manual (require Advance click)

### Phase 6: Frontend
- [ ] `app/web/index.html` — Add 4 new column divs:
  - After "column-development": column-conceptual_review ("CONCEPTUAL REVIEW")
  - After that: column-optimization ("OPTIMIZATION")
  - After that: column-security ("SECURITY")
  - After that: column-full_review ("FINAL REVIEW")
  - Column IDs must match: `column-{type}`, containers: `tasks-{type}`, counts: `count-{type}`

- [ ] `app/web/kanban.js` — Updates:
  1. `WIP_LIMITS` — add entries for indev, conceptual_review, optimization, security, full_review
  2. `COLUMN_NEXT` — update progression map for 9 columns
  3. `renderTasksFromDatabase()` — update columns array to include all 9
  4. `updateTaskCounts()` — update columns array
  5. Column rendering arrays (several places use `['architecture', 'idea', 'planning', 'development', 'review', 'completed']`)
  6. Map old "development" → "indev" and "review" → columns appropriately (or keep backward compat)

- [ ] `app/web/style.css` — Add colors for new columns:
  - indev: blue (#0d6efd) — same as old development
  - conceptual_review: teal (#20c997)
  - optimization: indigo (#6610f2)
  - security: red-orange (#e83e8c)
  - full_review: dark orange (#fd7e14) — same as old review

## Column Mapping (Old → New)

Old 6 columns: architecture, idea, planning, development, review, completed
New 9 columns: architecture, idea, planning, indev, conceptual_review, optimization, security, full_review, completed

**IMPORTANT**: The old "development" type becomes "indev". The old "review" type is replaced by multiple review stages. Existing tasks with type="development" or type="review" need to be handled — either migrated or kept backward-compatible.

## Key Architecture Notes

- All pipelines follow the same pattern as IntakePipeline: async class with run() method, stores results in DB, returns dict
- All use call_llm() from llm_client.py with llm_id + budget_id for tracking
- All store votes in transition_votes/transition_results using existing CRUD
- ComponentToolDispatcher provides 5th layer of delete protection (file write containment)
- Security reviewers have VETO POWER (single reviewer blocks)
- Merge is fully deterministic (no LLM) — just git operations + test suite
- run_shell_security uses ALLOWLIST (not blocklist like run_shell)
- run_shell_review also uses ALLOWLIST
