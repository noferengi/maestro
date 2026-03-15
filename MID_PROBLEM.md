# Mid-Problem Summary

## Current Issue

Implementing ghost target drag-and-drop logic with task compatibility rules based on LLM/budget validation.

## What Was Attempted

1. **Added database fields** - Added `llm` (JSON) and `budget` (String) columns to the Task model in `app/database.py`
2. **Updated API layer** - Modified `app/main.py` to include llm/budget in task creation and updates
3. **Updated create_task function** - Added llm and budget parameters
4. **Updated seed_task function** - Added llm and budget parameters (completed)
5. **Attempting to update seed calls** - Need to update all `seed_task()` calls in `seed_sample_tasks()` to pass llm and budget arguments

## Current Difficulty

The `seed_task()` function signature was successfully updated to include `llm=None, budget=""` parameters, but the calls to `seed_task()` within `seed_sample_tasks()` still have the old signature (7 arguments + position) and need to be updated to include the new llm/budget parameters.

The edit tool failed because the whitespace/context in the file doesn't match what I specified. I need to:
1. Read the exact content with proper context
2. Update each seed_task call to include the llm and budget arguments

## Next Immediate Steps

1. **Create a migration script** (`migrate_llm_budget.py`) that:
   - Adds the `llm` and `budget` columns to existing database if they don't exist
   - Seeds appropriate values for existing tasks
   - Updates the seed_sample_tasks() to include llm/budget for new tasks

2. **Fix the seed_task calls** in seed_sample_tasks():
   - Architecture tasks: llm=None, budget=""
   - Planning tasks: llm=None (user needs to fill), budget="500", "750", "1000"
   - Development tasks: llm=None (user needs to fill), budget="250", "1500"
   - Review/Completed: llm=None, budget=""

3. **Implement ghost target logic** in `app/web/kanban.js`:
   - Architecture cards: Only show ghost when reordering within own column (when >1 card)
   - Planning cards: Show ghosts in IN PROGRESS only when target cards have valid "Move to IN REVIEW" buttons
   - Development cards: No ghosts (user cannot move, only LLM can)
   - Review cards: Only show ghosts in IN REVIEW column (when >1 card)

## Database Migration Required

Existing databases need the new `llm` and `budget` columns added. A migration script is needed to:
- Add columns to existing tables (SQLite ALTER TABLE pattern)
- Populate sensible defaults
- Ensure backward compatibility
