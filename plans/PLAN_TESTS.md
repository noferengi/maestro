# Test Resolution Plan

This document outlines the strategy for resolving the current test failures and errors in Project Maestro.

## Summary of Test Results
- **Passed**: 814
- **Skipped**: 1
- **Failed**: 3
- **Errors**: 12
- **Total**: 830

## Error 1: Missing `sample_all_tasks` Fixture
**Symptoms**: 12 errors in `tests/test_intake_pipeline.py`.
**Root Cause**: The fixture `sample_all_tasks` is used by many tests in `tests/test_intake_pipeline.py` but is not defined in the file or in `tests/conftest.py`.
**Resolution**: Define the `sample_all_tasks` fixture in `tests/conftest.py`. It should provide a representative list of tasks that the `IntakePipeline` can use for conflict detection.

**Implementation Plan**:
- Add the following fixture to `tests/conftest.py`:
```python
@pytest.fixture
def sample_all_tasks():
    """A sample list of tasks for conflict detection tests."""
    return [
        {
            "id": "task-existing-1",
            "title": "Database Schema Design",
            "type": "planning",
            "description": "Designing the core database schema for the kanban board.",
            "project": "Maestro"
        },
        {
            "id": "task-existing-2",
            "title": "React Dashboard UI",
            "type": "indev",
            "description": "Implementing the main dashboard view with React components.",
            "project": "Maestro"
        },
        {
            "id": "task-existing-3",
            "title": "Authentication API",
            "type": "completed",
            "description": "User authentication endpoints and middleware.",
            "project": "Maestro"
        }
    ]
```

## Failure 1: Migration 0057 `down` method
**Symptoms**: `sqlite3.OperationalError: no such column: intake_exhausted` in `tests/test_migrations.py`.
**Root Cause**: Migration `0057_add_acceptance_criteria_to_tasks.py` has a bug in its `down` method. It tries to use a column named `intake_exhausted` (INTEGER), but the actual column added in migration `0047` and used throughout the codebase is `intake_exhausted_at` (TEXT).
**Resolution**: Update `app/migrations/versions/0057_add_acceptance_criteria_to_tasks.py` to use the correct column name and type.

**Implementation Plan**:
- Replace `intake_exhausted INTEGER DEFAULT 0` with `intake_exhausted_at TEXT` in the `CREATE TABLE` and `INSERT` statements of the `down` method.
- Also check for `intake_rejection_count` which might be missing or wrongly named (though migration `0047` didn't add it, I should verify where it comes from).

## Failure 2: Pipeline Routing Handler Mismatch
**Symptoms**: `AssertionError: assert '_run_optimiz...n_pipeline_bg' == '_advance_to_optimization'` in `app/tests/test_pipeline_routing.py`.
**Root Cause**: `app/main.py` updated `ADVANCE_HANDLERS["conceptual_review"]` to `_run_optimization_pipeline_bg`, but the test still expects the old value `_advance_to_optimization`.
**Resolution**: Update the test to match the current implementation in `app/main.py`.

**Implementation Plan**:
- Update `app/tests/test_pipeline_routing.py::TestAdvanceHandlersMap::test_correct_handlers` to assert against `_run_optimization_pipeline_bg`.

## Failure 3: Advance Endpoint Validation (400 vs 200)
**Symptoms**: `assert 400 == 200` in `app/tests/test_pipeline_routing.py::test_200_pipeline_started_idea_task`.
**Root Cause**: `app/main.py` now enforces a "clarification gate" for `idea` tasks. They must have `clarification_status` set to `approved` or `skipped` to advance. The test task has the default `none` status.
**Resolution**: Update the test helper or the test itself to set a valid `clarification_status`.

**Implementation Plan**:
- Modify `_make_task` in `app/tests/test_pipeline_routing.py` to accept an optional `clarification_status` (defaulting to `approved` for tests that need to advance).
- Or explicitly set it in the failing test cases.

## Action Items
1. [ ] Update `tests/conftest.py` with `sample_all_tasks` fixture.
2. [ ] Fix migration `0057_add_acceptance_criteria_to_tasks.py` column names in `down`.
3. [ ] Update `app/tests/test_pipeline_routing.py` to match new handler names.
4. [ ] Update `app/tests/test_pipeline_routing.py` to satisfy the clarification gate.
5. [ ] Run tests again to verify all fixes.
