import sys
import os
import logging

# Add app directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Configure logging before anything else imports the logging module
# ---------------------------------------------------------------------------
from app.logging_config import configure_logging
from app.agent.config import LOG_LEVEL, LOG_FILE, LOG_MAX_BYTES, LOG_BACKUP_COUNT

configure_logging(
    level=LOG_LEVEL,
    log_file=LOG_FILE or None,
    max_bytes=LOG_MAX_BYTES,
    backup_count=LOG_BACKUP_COUNT,
)

logger = logging.getLogger(__name__)

from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse, HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List, Optional
import asyncio
import json
import threading
from pydantic import BaseModel
from database import (
    init_db, get_db, create_task, get_task, get_tasks_by_type,
    update_task, delete_task, get_all_tasks, get_task_history, append_task_history, reorder_tasks, seed_sample_tasks,
    get_tasks_by_project,
    Project, get_all_projects, get_project, upsert_project, rename_project, delete_project,
    Task, LLM, Budget, BudgetEntry, SubdivisionRecord, SessionLocal,
    get_all_llms, get_llm, create_llm, update_llm, delete_llm,
    get_all_budgets, get_budget, create_budget, update_budget, delete_budget,
    ComputeNode, get_all_compute_nodes, get_compute_node,
    create_compute_node, update_compute_node, delete_compute_node,
    TransitionVote, TransitionResult,
    create_transition_vote, get_transition_votes,
    create_transition_result, get_transition_results,
    get_budget_entries, get_budget_summary,
    create_subdivision_record, get_subdivision_records,
    get_child_tasks, get_active_child_tasks, count_total_sub_ideas,
    update_subdivision_record,
    get_descendant_tree, set_big_idea_flag, batch_reorder_tasks,
    batch_update_map_positions,
)
from database import (
    PlanningResult, ComponentResult, OptimizationResult,
    SecurityReviewResult, FinalReviewResult, MergeRecord,
    create_planning_result, get_planning_result,
    create_component_result, get_component_results,
    create_optimization_result, get_optimization_result,
    create_security_review_result, get_security_review_results,
    create_final_review_result, get_final_review_results,
    create_merge_record, get_merge_record,
    get_research_jobs_for_task, get_research_job,
    create_research_job, update_research_job,
    get_optimization_benchmarks,
)
from database import (
    create_inbox_message, get_inbox_messages, get_inbox_message,
    mark_inbox_read, mark_all_inbox_read, delete_inbox_message, count_unread_inbox,
)
from database import (
    get_intake_draft, create_intake_draft, update_intake_draft,
    append_conversation_message, intake_draft_to_dict,
)

from app.agent.config import PIPELINE_COLUMN_ORDER, PIPELINE_DONE_STATUSES
from app.agent.llm_client import ShutdownError, PipelineAbortedError, TaskDeactivatedError, invalidate_llm_cache

@asynccontextmanager
async def lifespan(app: FastAPI):
    # --- startup ---
    init_db()
    from app.database.crud_sessions import close_zombie_sessions
    n = close_zombie_sessions()
    if n:
        logger.info("Closed %d zombie agent_sessions on startup.", n)
    seed_sample_tasks()
    # Ensure TheMaestro always has a project record (migration backfill covers
    # existing names, but a fresh DB after reset needs it too).
    upsert_project("TheMaestro")
    from app.agent.scheduler import start_scheduler
    start_scheduler()
    try:
        yield
    finally:
        # --- shutdown ---
        # Use try/finally so this runs even when uvicorn cancels the lifespan
        # task with CancelledError (e.g. Ctrl-C while a background task is active).
        try:
            from app.agent.llm_client import signal_shutdown
            signal_shutdown()
            from app.agent.scheduler import stop_scheduler
            stop_scheduler(timeout=60.0)
        except KeyboardInterrupt:
            logger.info("Shutdown sequence interrupted by user. Exiting immediately.")


app = FastAPI(title="Kanban Board API", lifespan=lifespan)

# Mount static files directory
app.mount("/static", StaticFiles(directory="app/web"), name="static")


# ============================================
# Kanban API Endpoints
# ============================================

@app.get("/api/tasks", response_model=List[dict])
def read_tasks():
    """Get all tasks from the database"""
    tasks = get_all_tasks()
    return [task_to_dict(task) for task in tasks]


@app.get("/api/projects/{project_name}/tasks", response_model=List[dict])
def read_tasks_by_project(project_name: str):
    """Get all tasks belonging to a specific project"""
    tasks = get_tasks_by_project(project_name)
    return [task_to_dict(task) for task in tasks]


@app.get("/api/tasks/{task_id}", response_model=dict)
def read_task(task_id: str):
    """Get a specific task by ID"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task_to_dict(task)


@app.get("/api/tasks/by-type/{task_type}", response_model=List[dict])
def read_tasks_by_type(task_type: str):
    """Get all tasks of a specific type (planning, development, review, completed, architecture)"""
    tasks = get_tasks_by_type(task_type)
    return [task_to_dict(task) for task in tasks]


_HUMAN_CREATABLE_TYPES = frozenset(PIPELINE_COLUMN_ORDER[:2])


@app.post("/api/tasks", response_model=dict)
def create_new_task(task_data: dict):
    """Create a new task"""
    if not task_data.get('title'):
        raise HTTPException(status_code=400, detail="Title is required")

    requested_type = task_data.get('type', 'idea')
    if requested_type not in _HUMAN_CREATABLE_TYPES:
        raise HTTPException(
            status_code=422,
            detail=f"Cannot create tasks directly in '{requested_type}'. "
                   f"Create an IDEA and advance it through the pipeline."
        )

    task = create_task(
        title=task_data['title'],
        task_type=requested_type,
        description=task_data.get('description', ''),
        owner=task_data.get('owner', 'user'),
        tags=task_data.get('tags', []),
        content=task_data.get('content'),
        llm_id=task_data.get('llm_id'),
        budget_id=task_data.get('budget_id'),
        prerequisites=task_data.get('prerequisites', []),
        project=task_data.get('project', 'TheMaestro')
    )

    if not task:
        raise HTTPException(status_code=500, detail="Failed to create task")

    # Queue clarification for new IDEA cards — scheduler picks it up on the next tick
    # at highest priority (before file summaries and all pipeline tasks).
    if requested_type == 'idea' and task.llm_id and task.budget_id:
        update_task(task.id, clarification_status='pending', description_original=task.description or '')
        task = get_task(task.id)  # reload to get updated clarification_status

    return task_to_dict(task)


def _is_advancing(old_type: str, new_type: str) -> bool:
    """True when the type change moves a task forward in the pipeline."""
    try:
        return PIPELINE_COLUMN_ORDER.index(new_type) > PIPELINE_COLUMN_ORDER.index(old_type)
    except ValueError:
        return False


@app.put("/api/tasks/{task_id}", response_model=dict)
def update_existing_task(task_id: str, task_data: dict):
    """Update an existing task"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Only allow updating specific fields
    allowed_fields = ['title', 'description', 'owner', 'tags', 'content', 'llm_id', 'budget_id', 'type', 'prerequisites', 'map_x', 'map_y', 'review_notes']
    update_data = {key: value for key, value in task_data.items() if key in allowed_fields}

    # Gate: advancing a task requires description, llm_id, and budget_id
    new_type = update_data.get('type')
    if new_type and _is_advancing(task.type, new_type):
        # Use incoming values if provided, otherwise fall back to current task values
        desc = update_data.get('description', task.description)
        llm = update_data.get('llm_id', task.llm_id)
        bud = update_data.get('budget_id', task.budget_id)
        missing = []
        if not desc:
            missing.append('description')
        if not llm:
            missing.append('llm_id')
        if not bud:
            missing.append('budget_id')
        if missing:
            raise HTTPException(
                status_code=422,
                detail=f"Cannot advance task: missing {', '.join(missing)}"
            )

    updated_task = update_task(task_id, **update_data)

    if not updated_task:
        raise HTTPException(status_code=500, detail="Failed to update task")

    # Queue clarification for IDEA cards that just got an LLM/Budget assigned
    if (updated_task.type == 'idea' and 
        updated_task.clarification_status == 'none' and 
        updated_task.llm_id and 
        updated_task.budget_id):
        updated_task = update_task(task_id, clarification_status='pending', description_original=updated_task.description or '')

    # Check for completion rollup if task moved to completed
    if new_type and new_type.lower() in PIPELINE_DONE_STATUSES:
        _check_completion_rollup(task_id)

    return task_to_dict(updated_task)


@app.patch("/api/tasks/map-positions")
def batch_update_map_positions_endpoint(updates: list = Body(...)):
    """
    Bulk-save Column Map View canvas positions for a list of tasks.

    Body: [{id, map_x, map_y}, ...]
    Does NOT touch task history — purely a canvas layout persistence call.
    """
    if not updates:
        return {"updated": 0}
    count = batch_update_map_positions(updates)
    return {"updated": count}


@app.delete("/api/tasks/{task_id}", response_model=dict)
def delete_task_endpoint(task_id: str):
    """Soft-delete a task and all its descendants (sets is_active=False)."""
    deactivated_ids = delete_task(task_id)
    if not deactivated_ids:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.agent.scheduler import cancel_task_sessions
    cancel_task_sessions(deactivated_ids)
    return {"deactivated": len(deactivated_ids)}


@app.get("/api/tasks/{task_id}/history", response_model=dict)
def get_task_history_endpoint(task_id: str):
    """Get task history"""
    history = get_task_history(task_id)
    return {"task_id": task_id, "history": history}


class ReorderTaskBody(BaseModel):
    """Request body for reordering a task"""
    position: int
    type: str


@app.post("/api/tasks/{task_id}/reorder", response_model=dict)
def reorder_task(task_id: str, task_data: ReorderTaskBody = Body(...)):
    """
    Reorder a task within its column
    """
    if task_data.position < 0:
        raise HTTPException(status_code=400, detail="Invalid position")

    result = reorder_tasks(task_id, task_data.position, task_data.type)
    if not result:
        raise HTTPException(status_code=404, detail="Task not found or reorder failed")

    # Return updated task with new position
    task = get_task(task_id)
    if task:
        return {"id": task.id, "title": task.title, "position": task.position}
    return {"id": task_id, "position": task_data.position}


def _resolve_llm_endpoint(task):
    """Resolve LLM base URL, model, and max_context from a task's llm_id."""
    llm_base_url = None
    llm_model = None
    max_context = None
    if task.llm_id:
        llm_record = get_llm(task.llm_id)
        if llm_record:
            llm_base_url = f"http://{llm_record.address}:{llm_record.port}/v1"
            llm_model = llm_record.model
            max_context = llm_record.max_context
    return llm_base_url, llm_model, max_context


def _setup_thread_context(task) -> str | None:
    """Return the project path for a task."""
    if task and task.project:
        from app.database import get_project_path
        return get_project_path(task.project)
    return None


def _setup_worktree(task_id: str, project_path: str | None) -> tuple[str | None, bool]:
    """Create a git worktree for task_id under project_path.

    Returns (worktree_path, aborted) where aborted=True means the caller should return
    immediately (git repo but worktree creation failed — strict isolation violation).
    Non-git projects and None project_path return (project_path, False) with no worktree.
    """
    from app.agent.worktree import setup_task_worktree, _is_git_repo
    from app.agent.tools import set_task_git_cwd
    if not project_path:
        return project_path, False
    wt = setup_task_worktree(task_id, project_path)
    if wt:
        set_task_git_cwd(wt)
        return wt, False
    if _is_git_repo(project_path):
        logger.error("[worktree] Strict isolation violation: could not create worktree for task '%s'. Aborting.", task_id)
        return project_path, True
    set_task_git_cwd(project_path)
    return project_path, False


def _teardown_worktree(task_id: str, project_path: str | None, worktree_path: str | None) -> None:
    """Tear down the worktree created by _setup_worktree, if one was created."""
    if project_path and worktree_path and worktree_path != project_path:
        from app.agent.worktree import teardown_task_worktree
        teardown_task_worktree(task_id, project_path)


def _store_pipeline_result(task_id, result, budget_id):
    """Store a transition result and its individual votes."""
    create_transition_result(
        task_id=task_id,
        transition="idea_to_planning",
        outcome=result["outcome"],
        vote_summary=result,
        total_prompt_tokens=result.get("total_prompt_tokens", 0),
        total_completion_tokens=result.get("total_completion_tokens", 0),
    )
    for vote in result.get("votes", []):
        create_transition_vote(
            task_id=task_id,
            transition="idea_to_planning",
            stage=vote["stage"],
            verdict=vote["verdict"],
            confidence=vote.get("confidence", 0),
            justification=vote.get("justification", ""),
            raw_response=vote.get("raw_response"),
            prompt_tokens=vote.get("prompt_tokens", 0),
            completion_tokens=vote.get("completion_tokens", 0),
            model=vote.get("model", ""),
            budget_id=budget_id,
        )


def _store_infra_abort_result(task_id: str, exc: "PipelineAbortedError", budget_id, transition: str = "idea_to_planning") -> None:
    """Record an infrastructure-abort event so the UI can surface it clearly."""
    from app.database.crud_pipeline import create_transition_result
    create_transition_result(
        task_id=task_id,
        transition=transition,
        outcome="aborted_infra",
        vote_summary={
            "stage": exc.stage,
            "error": str(exc.cause),
            "note": "Pipeline aborted due to infrastructure failure. Will retry when endpoint recovers.",
        },
        total_prompt_tokens=0,
        total_completion_tokens=0,
    )




def _execute_subdivision(task, llm_base_url, llm_model, max_context, scope_vote, rejection_context, loop):
    """Run the SubdivisionAgent and return the result."""
    from app.agent.subdivide import run_subdivision
    from app.database import get_project_path as _get_project_path

    project_root = _get_project_path(task.project) if task.project else None
    return loop.run_until_complete(
        run_subdivision(
            parent_task_id=task.id,
            parent_title=task.title,
            parent_description=task.description or "",
            scope_vote=scope_vote,
            rejection_context=rejection_context,
            max_context=max_context,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            llm_id=task.llm_id,
            budget_id=task.budget_id,
            project_root=project_root,
        )
    )


def _create_sub_idea_tasks(task, sub_result, generation):
    """Create child tasks from SubdivisionResult, return list of child IDs.

    Tasks are created one at a time in index order.  As each task is created
    its *actual* DB-assigned ID is recorded in `actual_id_map` so that any
    later sub-idea that lists it as a prerequisite gets the correct real ID,
    not a pre-generated placeholder that was never inserted into the DB.
    """
    from database import SessionLocal, Task as TaskModel

    child_ids = []
    # Maps "sub-{i}" → actual task ID, built incrementally as tasks are created.
    actual_id_map = {}

    for i, sub_idea in enumerate(sub_result.sub_ideas):
        # Resolve prerequisites from already-created siblings' actual IDs.
        # Because sub-ideas are created in index order, sub-j's prerequisites
        # can only legally reference sub-k where k < j (enforced by the DAG
        # validation step above).  Any forward references are silently skipped.
        prereqs = [actual_id_map[p] for p in sub_idea.prerequisites if p in actual_id_map]

        create_task(
            title=sub_idea.title,
            task_type="idea",
            description=sub_idea.description,
            owner="system",
            tags=["subdivision", f"gen-{generation}"],
            llm_id=task.llm_id,
            budget_id=task.budget_id,
            prerequisites=prereqs,
            project=task.project or "TheMaestro",
            position=i,
        )

        db = SessionLocal()
        try:
            latest = (db.query(TaskModel)
                      .filter(TaskModel.title == sub_idea.title,
                              TaskModel.owner == "system",
                              TaskModel.id != task.id)
                      .order_by(TaskModel.created_at.desc())
                      .first())
            if latest:
                actual_id_map[f"sub-{i}"] = latest.id
                child_ids.append(latest.id)
                latest.parent_task_id = task.id
                latest.subdivision_generation = generation
                # Store interface contracts on child if available
                child_contracts = {}
                if hasattr(sub_idea, 'provides') and sub_idea.provides:
                    child_contracts['provides'] = sub_idea.provides
                if hasattr(sub_idea, 'consumes') and sub_idea.consumes:
                    child_contracts['consumes'] = sub_idea.consumes
                if child_contracts:
                    latest.interface_contracts = json.dumps(child_contracts)
                db.commit()
        finally:
            db.close()

    return child_ids


def _handle_subdivision_outcome(task, result, llm_base_url, llm_model, max_context, loop):
    """Handle a 'subdivide' outcome from the intake pipeline."""
    from app.agent.config import (
        SUBDIVISION_MAX_DEPTH,
        SUBDIVISION_MAX_RETRIES,
        SUBDIVISION_MAX_TOTAL_SUB_IDEAS,
    )
    from app.agent.dag import DAGResolver

    generation = (task.subdivision_generation or 0) + 1

    # Check recursion depth limit
    if generation > SUBDIVISION_MAX_DEPTH:
        logger.warning("[intake] Task '%s' hit subdivision depth limit (%d). Downgrading to rejected.", task.id, SUBDIVISION_MAX_DEPTH)
        update_task(task.id, type="idea")
        return

    # Check total sub-idea count
    # Walk up to find the root task
    root_id = task.id
    current = task
    while current.parent_task_id:
        root_id = current.parent_task_id
        current = get_task(current.parent_task_id)
        if not current:
            break
    total_existing = count_total_sub_ideas(root_id)
    if total_existing >= SUBDIVISION_MAX_TOTAL_SUB_IDEAS:
        logger.warning("[intake] Total sub-ideas (%d) >= limit (%d). Downgrading to rejected.", total_existing, SUBDIVISION_MAX_TOTAL_SUB_IDEAS)
        update_task(task.id, type="idea")
        return

    # Extract scope vote from the pipeline result for context
    scope_vote = None
    for v in result.get("votes", []):
        if v.get("stage") == "scope_analysis":
            scope_vote = v.get("raw_response")
            break

    # Set parent to subdividing state
    update_task(task.id, type="subdividing")

    # Run subdivision agent
    sub_result = _execute_subdivision(
        task, llm_base_url, llm_model, max_context,
        scope_vote=scope_vote,
        rejection_context=None,
        loop=loop,
    )

    if not sub_result.sub_ideas or sub_result.confidence < 50:
        logger.warning(
            "[subdivide] Subdivision for task '%s' failed to produce confident children "
            "(sub_ideas=%d, confidence=%d). Reverting to IDEA.",
            task.id, len(sub_result.sub_ideas), sub_result.confidence
        )
        update_task(task.id, type="idea")
        return

    # Validate sub-idea DAG
    temp_tasks = []
    for i, si in enumerate(sub_result.sub_ideas):
        temp_tasks.append({
            "id": f"sub-{i}",
            "type": "idea",
            "position": i,
            "prerequisites": si.prerequisites,
        })
    dag = DAGResolver(temp_tasks)
    errors = dag.validate_dag()
    cycle_errors = [e for e in errors if "Cycle" in e]
    if cycle_errors:
        logger.warning("[intake] Subdivision produced cyclic DAG: %s. Reverting to idea.", cycle_errors)
        update_task(task.id, type="idea")
        return

    # Create child tasks (with interface contracts)
    child_ids = _create_sub_idea_tasks(task, sub_result, generation)

    # Set the Big Idea flag on the parent
    set_big_idea_flag(task.id)

    # Serialize interface contracts for the subdivision record
    contracts_json = None
    if sub_result.interface_contracts:
        contracts_json = json.dumps(sub_result.interface_contracts)

    # Create subdivision record
    create_subdivision_record(
        parent_task_id=task.id,
        child_task_ids=child_ids,
        generation=generation,
        attempt_number=1,
        agent_vote=sub_result.raw_output,
        prompt_tokens=sub_result.prompt_tokens,
        completion_tokens=sub_result.completion_tokens,
        status="active",
        interface_contracts=contracts_json,
    )

    # Transition parent back to 'idea' so it is visible on the board and the
    # Regenerate button works.  The transition_result with outcome="subdivide"
    # was already stored by the caller, so the scheduler will not re-dispatch
    # this task for another intake run.
    update_task(task.id, type="idea")

    logger.info("[intake] Task '%s' subdivided into %d sub-ideas (generation %d).", task.id, len(child_ids), generation)


def _handle_self_healing_rejection(task, result, llm_base_url, llm_model, max_context, loop):
    """Handle rejection of a system-generated sub-idea: retry subdivision if budget allows."""
    from app.agent.config import SUBDIVISION_MAX_RETRIES
    from app.agent.dag import DAGResolver

    parent_task = get_task(task.parent_task_id)
    if not parent_task:
        logger.warning("[intake] Parent task '%s' not found. Cannot self-heal.", task.parent_task_id)
        return

    # Find the current active subdivision record
    records = get_subdivision_records(task.parent_task_id)
    active_record = next((r for r in records if r.status == "active"), None)
    if not active_record:
        logger.warning("[intake] No active subdivision record for parent '%s'.", task.parent_task_id)
        return

    attempt = active_record.attempt_number
    if attempt >= SUBDIVISION_MAX_RETRIES:
        logger.warning("[intake] Subdivision retries exhausted (%d/%d) for parent '%s'. Reverting parent to idea.", attempt, SUBDIVISION_MAX_RETRIES, task.parent_task_id)
        # Mark record as failed, revert parent
        update_subdivision_record(active_record.id, status="failed")
        update_task(parent_task.id, type="idea")
        return

    # Build rejection context
    sibling_tasks = get_child_tasks(task.parent_task_id)
    rejected_sub_ideas = []
    passed_sub_ideas = []
    for sib in sibling_tasks:
        sib_dict = {"title": sib.title, "description": sib.description}
        # Check if sibling was rejected
        sib_results = get_transition_results(sib.id)
        if sib_results and sib_results[0].outcome in ("rejected", "failed"):
            sib_votes = get_transition_votes(sib.id)
            rejection_reasons = [
                {"stage": v.stage, "verdict": v.verdict, "justification": v.justification}
                for v in sib_votes
            ]
            sib_dict["rejection_reasons"] = rejection_reasons
            sib_dict["intake_votes"] = rejection_reasons
            rejected_sub_ideas.append(sib_dict)
        elif sib.type == "planning":
            passed_sub_ideas.append(sib_dict)

    rejection_context = {
        "attempt_number": attempt + 1,
        "previous_decomposition": [
            {"title": sib.title, "description": sib.description}
            for sib in sibling_tasks
        ],
        "rejected_sub_ideas": rejected_sub_ideas,
        "passed_sub_ideas": passed_sub_ideas,
        "guidance": "Previous decomposition failed. Try a different strategy. "
                    "You may keep sub-ideas that already passed.",
    }

    # Cancel all existing sibling sub-ideas
    for sib in sibling_tasks:
        if sib.type != "cancelled":
            update_task(sib.id, type="cancelled")

    # Mark old record as superseded
    update_subdivision_record(active_record.id, status="superseded")

    # Extract scope vote from parent's transition history
    scope_vote = None
    parent_results = get_transition_results(parent_task.id)
    for pr in parent_results:
        if pr.vote_summary and isinstance(pr.vote_summary, dict):
            for v in pr.vote_summary.get("votes", []):
                if v.get("stage") == "scope_analysis":
                    scope_vote = v.get("raw_response")
                    break
            if scope_vote:
                break

    generation = parent_task.subdivision_generation + 1 if parent_task.subdivision_generation else 1

    # Re-run subdivision with rejection context
    sub_result = _execute_subdivision(
        parent_task, llm_base_url, llm_model, max_context,
        scope_vote=scope_vote,
        rejection_context=rejection_context,
        loop=loop,
    )

    if not sub_result.sub_ideas or sub_result.confidence < 50:
        logger.warning("[intake] Retry subdivision returned low confidence. Reverting parent to idea.")
        update_task(parent_task.id, type="idea")
        return

    # Validate DAG
    temp_tasks = []
    for i, si in enumerate(sub_result.sub_ideas):
        temp_tasks.append({
            "id": f"sub-{i}",
            "type": "idea",
            "position": i,
            "prerequisites": si.prerequisites,
        })
    dag = DAGResolver(temp_tasks)
    errors = dag.validate_dag()
    cycle_errors = [e for e in errors if "Cycle" in e]
    if cycle_errors:
        logger.warning("[intake] Retry subdivision produced cyclic DAG. Reverting parent to idea.")
        update_task(parent_task.id, type="idea")
        return

    # Create new child tasks
    child_ids = _create_sub_idea_tasks(parent_task, sub_result, generation)

    # Create new subdivision record
    create_subdivision_record(
        parent_task_id=parent_task.id,
        child_task_ids=child_ids,
        generation=generation,
        attempt_number=attempt + 1,
        rejection_context=rejection_context,
        agent_vote=sub_result.raw_output,
        prompt_tokens=sub_result.prompt_tokens,
        completion_tokens=sub_result.completion_tokens,
        status="active",
    )

    logger.info("[intake] Self-healing: re-subdivided '%s' into %d sub-ideas (attempt %d).", parent_task.id, len(child_ids), attempt + 1)


def _pipeline_session(func):
    """Decorator for background pipeline functions.

    Waits until the target LLM is the active model (or the router is idle) and
    has a free slot, then registers the session with the scheduler before
    running.  Releases the slot when the function exits.

    This makes ALL API-triggered pipelines (intake, planning, review, loop, etc.)
    subject to the same one-LLM-at-a-time and capacity limits as scheduler-
    dispatched jobs, preventing the model-thrashing that occurs when a manual
    action fires while the scheduler has a different model loaded.

    Also writes an agent_session record for every user-triggered pipeline run.
    """
    import functools

    # Map wrapper function names to agent_type values
    _AGENT_TYPE_MAP = {
        "_run_regenerate_subdivision": "subdivision",
        "_run_planning_pipeline_bg": "planning",
        "_run_review_pipeline_bg": "conceptual_review",
        "_run_optimization_pipeline_bg": "optimization",
        "_run_security_pipeline_bg": "security",
        "_run_final_review_pipeline_bg": "final_review",
        "_run_loop_bg": "maestro_loop",
        "_run_intake_bg": "intake",
    }

    @functools.wraps(func)
    def wrapper(task_id: str, *args, **kwargs):
        from app.agent.scheduler import (
            wait_and_register_pipeline_session,
            unregister_pipeline_session,
        )
        from app.database import create_agent_session, close_agent_session
        task = get_task(task_id)
        if task and task.llm_id:
            key = f"bg-{func.__name__}-{task_id}"
            registered = wait_and_register_pipeline_session(key, task.llm_id)
            if not registered:
                logger.error(
                    "[pipeline] %s for task '%s': timed out waiting for LLM %d slot — aborting.",
                    func.__name__, task_id, task.llm_id,
                )
                return
            agent_type = _AGENT_TYPE_MAP.get(func.__name__, func.__name__.lstrip("_"))
            _session_id = create_agent_session(
                task_id=task_id,
                agent_type=agent_type,
                llm_id=task.llm_id,
                budget_id=task.budget_id,
                scheduler_reason="user_triggered",
            )
            _exit_reason = "completed"
            try:
                return func(task_id, *args, **kwargs)
            except TaskDeactivatedError as exc:
                _exit_reason = "deactivated"
                logger.info("[pipeline] %s", exc)
            except Exception:
                _exit_reason = "error"
                raise
            finally:
                close_agent_session(_session_id, _exit_reason)
                unregister_pipeline_session(key, task.llm_id)
        else:
            return func(task_id, *args, **kwargs)
    return wrapper


def _start_bg(fn, *args) -> None:
    """Start a pipeline function in a daemon thread.

    Replaces Starlette BackgroundTasks for pipeline work so that the thread is
    decoupled from the ASGI event loop.  When uvicorn handles Ctrl-C it cancels
    the event loop coroutines, and BackgroundTasks' run_in_threadpool receives
    CancelledError — producing noisy tracebacks.  A plain daemon thread is not
    affected by event-loop cancellation; the scheduler's _pipeline_session
    decorator still registers/tracks sessions for graceful shutdown.
    """
    threading.Thread(target=fn, args=args, daemon=True).start()


@_pipeline_session
def _run_regenerate_subdivision(task_id: str) -> None:
    """Background runner: re-runs the subdivision agent to produce a new set of children.

    Cancels the current active children, marks their record superseded, runs the
    agent fresh, and creates a new active subdivision record.  The parent task
    stays in 'subdividing' throughout.
    """
    try:
        import asyncio

        task = get_task(task_id)
        if not task:
            logger.warning("[regen] Task '%s' not found.", task_id)
            return
        _setup_thread_context(task)

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        # Cancel current non-cancelled children
        current_children = get_child_tasks(task_id)
        for child in current_children:
            if child.type != 'cancelled':
                update_task(child.id, type='cancelled')

        # Supersede the current active record
        existing_records = get_subdivision_records(task_id)
        for r in existing_records:
            if r.status == 'active':
                update_subdivision_record(r.id, status='superseded')

        # Keep parent in subdividing state
        update_task(task_id, type='subdividing')

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            sub_result = _execute_subdivision(
                task, llm_base_url, llm_model, max_context,
                scope_vote=None,
                rejection_context=None,
                loop=loop,
            )
        finally:
            loop.close()

        if not sub_result.sub_ideas or sub_result.confidence < 50:
            logger.warning(
                "[regen] Regeneration for '%s' returned low confidence (%d). Task stays in subdividing.",
                task_id, sub_result.confidence,
            )
            return

        generation = (task.subdivision_generation or 0) + 1
        child_ids = _create_sub_idea_tasks(task, sub_result, generation)

        contracts_json = None
        if sub_result.interface_contracts:
            contracts_json = json.dumps(sub_result.interface_contracts)

        create_subdivision_record(
            parent_task_id=task_id,
            child_task_ids=child_ids,
            generation=generation,
            attempt_number=len(existing_records) + 1,
            agent_vote=sub_result.raw_output,
            prompt_tokens=sub_result.prompt_tokens,
            completion_tokens=sub_result.completion_tokens,
            interface_contracts=contracts_json,
            status='active',
        )
        # Reset parent to 'idea' so children are visible on the board and the
        # scheduler can dispatch them.  The existing transition_result with
        # outcome="subdivide" ensures the scheduler won't re-run intake on it.
        update_task(task_id, type='idea')
        logger.info("[regen] Task '%s' regenerated into %d sub-ideas.", task_id, len(child_ids))
    except Exception:
        logger.exception("[regen] Regeneration for '%s' failed.", task_id)


@_pipeline_session
def _run_intake_pipeline(task_id: str) -> None:
    """Background runner for the intake pipeline."""
    try:
        import asyncio
        from app.agent.intake import run_intake_pipeline

        task = get_task(task_id)
        if not task:
            logger.warning("[intake] Task '%s' not found.", task_id)
            return
        _setup_thread_context(task)

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        if llm_base_url:
            logger.info("[intake] Using LLM: %s model=%s", llm_base_url, llm_model)

        project_tasks = get_tasks_by_project(task.project) if task.project else get_all_tasks()
        task_dicts = [task_to_dict(t) for t in project_tasks]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_intake_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    task_title=task.title,
                    all_tasks=task_dicts,
                    budget_id=task.budget_id,
                    llm_id=task.llm_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    project=task.project or None,  # Must be configured or pipeline will fail
                )
            )

            # Store the result
            _store_pipeline_result(task_id, result, task.budget_id)

            # Act on the result
            if result["outcome"] == "passed":
                update_task(task_id, type="planning")
                logger.info("[intake] Task '%s' advanced to PLANNING.", task_id)

            elif result["outcome"] == "subdivide":
                _handle_subdivision_outcome(
                    task, result, llm_base_url, llm_model, max_context, loop
                )

            elif result["outcome"] in ("rejected", "failed"):
                # Check if this is a system-generated sub-idea that should self-heal
                if task.parent_task_id:
                    logger.info("[intake] System-generated task '%s' rejected. Triggering self-healing.", task_id)
                    _handle_self_healing_rejection(
                        task, result, llm_base_url, llm_model, max_context, loop
                    )
                else:
                    logger.info("[intake] Task '%s' pipeline result: %s", task_id, result['outcome'])

            else:
                logger.info("[intake] Task '%s' pipeline result: %s", task_id, result['outcome'])

        finally:
            loop.close()
    except PipelineAbortedError as exc:
        logger.warning(
            "[intake] Task '%s' aborted due to infra error at stage '%s': %s — will retry.",
            task_id, exc.stage, exc.cause,
        )
        try:
            _store_infra_abort_result(task_id, exc, budget_id=None)
        except Exception:
            pass  # best-effort; don't mask the original abort
    except Exception as exc:
        logger.exception("[intake] Pipeline for '%s' failed.", task_id)


@_pipeline_session
def _run_planning_pipeline_bg(task_id: str) -> None:
    """Background runner for the planning pipeline."""
    project_path = None
    worktree_path = None
    loop = None
    try:
        import asyncio
        from app.agent.planning import run_planning_pipeline
        from app.agent.planning_gate import run_planning_gate
        from app.database import (
            supersede_planning_results, create_planning_result, update_planning_result,
        )
        from app.database.session import SessionLocal as _SL

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)

        # --- Planning cache gate (DB-only, runs before worktree setup) ---
        _content_hash = None
        try:
            from hashlib import sha256 as _sha256
            _content_hash = _sha256(f"{task.title}||{task.description or ''}".encode()).hexdigest()
        except Exception:
            pass

        _cache_mode = getattr(task, 'cache_mode', None) or 'normal'
        if _cache_mode == 'normal' and _content_hash:
            try:
                from app.database import get_reusable_planning_result, restore_planning_result
                _cached = get_reusable_planning_result(task_id, _content_hash)
                if _cached:
                    supersede_planning_results(task_id)
                    restore_planning_result(_cached.id)
                    update_task(task_id, type='indev')
                    logger.info(
                        "[planning] Cache HIT task '%s' — reusing plan %d, skipping pipeline.",
                        task_id, _cached.id,
                    )
                    return
            except Exception:
                logger.exception("[planning] Cache gate check failed for '%s' — running full pipeline.", task_id)
        elif _cache_mode in ('force_with_context', 'force_fresh'):
            try:
                update_task(task_id, cache_mode='normal')
            except Exception:
                pass

        _prior_failures = []
        if _cache_mode != 'force_fresh':
            try:
                from app.database import get_prior_failure_context
                _prior_failures = get_prior_failure_context(task_id)
            except Exception:
                pass

        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        all_tasks = [task_to_dict(t) for t in (get_tasks_by_project(task.project) if task.project else get_all_tasks())]

        # Lifecycle: supersede any stale active/in_progress rows, then create a
        # fresh in_progress row so the Stage Journal can show "Pipeline running…"
        # immediately rather than displaying the old stale result.
        supersede_planning_results(task_id)
        in_prog = create_planning_result(task_id, status='in_progress')
        run_row_id = in_prog.id if in_prog else None

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Run planning pipeline — _store_result inside will update run_row_id
            # row to status='active' on success.
            result = loop.run_until_complete(
                run_planning_pipeline(
                    task_id=task_id,
                    task_title=task.title,
                    task_description=task.description or "",
                    all_tasks=all_tasks,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    max_context=max_context,
                    project_path=worktree_path,
                    project_name=task.project,
                    run_row_id=run_row_id,
                    prior_failure_context=_prior_failures,
                )
            )
        except ShutdownError:
            logger.info("[planning] Pipeline for '%s' aborted due to server shutdown.", task_id)
            return
        except PipelineAbortedError as exc:
            logger.warning(
                "[planning] Task '%s' aborted due to infra error at stage '%s': %s — will retry.",
                task_id, exc.stage, exc.cause,
            )
            try:
                _store_infra_abort_result(task_id, exc, budget_id=getattr(task, "budget_id", None),
                                          transition="planning_to_indev")
            except Exception:
                pass
            return
        except Exception:
            logger.exception("[planning] Pipeline for '%s' failed.", task_id)
            return

        try:
            # Store transition result
            _store_pipeline_result_generic(task_id, result, task.budget_id, "planning_to_indev")

            if result.get("outcome") == "subdivide":
                # Scope too large — demote to IDEA and trigger subdivision immediately
                scope_reason = result.get("scope_reason", "Design scope too large.")
                logger.info(
                    "[planning] Task '%s' scope too large — demoting to IDEA for subdivision. %s",
                    task_id, scope_reason,
                )
                _rejection_ctx = {
                    "reason": "planning_scope_too_large",
                    "scope_reason": scope_reason,
                    "design_rationale": result.get("design_rationale", ""),
                    "file_manifest": result.get("file_manifest", []),
                    "survey_summary": result.get("survey_summary", ""),
                }
                update_task(task_id, type="subdividing")
                try:
                    _execute_subdivision(
                        task,
                        llm_base_url=llm_base_url,
                        llm_model=llm_model,
                        max_context=max_context,
                        scope_vote=None,
                        rejection_context=_rejection_ctx,
                        loop=loop,
                    )
                except Exception:
                    logger.exception("[planning] Subdivision after scope-fail failed for '%s'.", task_id)
                    update_task(task_id, type="idea")
            elif result.get("outcome") == "passed":
                # Run planning gate
                gate_result = loop.run_until_complete(
                    run_planning_gate(
                        task_id=task_id,
                        planning_result=result,
                        all_tasks=all_tasks,
                        max_context=max_context,
                        llm_base_url=llm_base_url,
                        llm_model=llm_model,
                        llm_id=task.llm_id,
                        budget_id=task.budget_id,
                        project_path=worktree_path,
                    )
                )
                # Persist gate check details so the UI can surface why a gate failed
                pr_row = get_planning_result(task_id)
                if pr_row:
                    _db = _SL()
                    try:
                        update_planning_result(_db, pr_row.id,
                                               gate_checks=json.dumps(gate_result.get("checks", [])))
                    finally:
                        _db.close()
                if gate_result.get("passed"):
                    # Mark the planning result as gate-passed so future runs can reuse it.
                    if pr_row:
                        try:
                            from app.database import mark_gate_passed
                            mark_gate_passed(pr_row.id, _content_hash)
                        except Exception:
                            pass
                    update_task(task_id, type="indev")
                    logger.info("[planning] Task '%s' advanced to IN DEV.", task_id)
                else:
                    logger.warning("[planning] Task '%s' failed planning gate.", task_id)
            else:
                logger.info("[planning] Task '%s' planning result: %s", task_id, result.get('outcome'))
        except Exception as exc:
            # Write the failure reason into the in_progress row so the Stage Journal
            # shows "run failed: <reason>" instead of the old stale result.
            if run_row_id is not None:
                _db = _SL()
                try:
                    update_planning_result(
                        _db, run_row_id,
                        status='failed',
                        error_message=str(exc)[:1000],
                    )
                finally:
                    _db.close()
            logger.exception("[planning] Pipeline for '%s' failed.", task_id)
    finally:
        if loop is not None:
            loop.close()
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_dev_orchestrator_bg(task_id: str) -> None:
    """Background runner for the development orchestrator."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.dev_orchestrator import run_dev_orchestrator

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        planning_result_obj = get_planning_result(task_id)

        if not planning_result_obj:
            logger.warning("[indev] No planning result for task '%s'.", task_id)
            return

        # Reconstruct planning result dict
        planning_result = {
            "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
            "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
            "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
            "interface_contracts": json.loads(planning_result_obj.interface_contracts or "[]"),
            "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
            "design_rationale": planning_result_obj.codebase_survey or "",
            "pitfalls_identified": json.loads(planning_result_obj.pitfalls_identified or "[]"),
            "review_votes": json.loads(planning_result_obj.review_votes or "[]"),
        }

        llm_record = get_llm(task.llm_id) if task.llm_id else None
        max_parallel = llm_record.parallel_sessions if llm_record else 1

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_dev_orchestrator(
                    task_id=task_id,
                    planning_result=planning_result,
                    max_parallel=max_parallel,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
        except ShutdownError:
            logger.info("[indev] Orchestrator for '%s' aborted due to server shutdown.", task_id)
            return
        except Exception:
            logger.exception("[indev] Orchestrator for '%s' failed.", task_id)
            return

        try:
            if result.get("status") == "ACCEPTED":
                update_task(task_id, type="conceptual_review")
                logger.info("[indev] Task '%s' advanced to CONCEPTUAL REVIEW.", task_id)
            else:
                update_task(task_id, type="planning")
                logger.warning("[indev] Task '%s' reverted to PLANNING: %s", task_id, result.get('error_detail'))
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[indev] Orchestrator for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _advance_to_optimization(task_id: str) -> None:
    """Auto-advance from conceptual review to optimization."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.conceptual_review import run_conceptual_review

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        planning_result_obj = get_planning_result(task_id)
        planning_result = {}
        if planning_result_obj:
            planning_result = {
                "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
                "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
                "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
                "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
            }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_conceptual_review(
                    task_id=task_id,
                    task_description=task.description or "",
                    planning_result=planning_result,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
        except ShutdownError:
            logger.info("[review] Pipeline for '%s' aborted due to server shutdown.", task_id)
            return
        except PipelineAbortedError as exc:
            logger.warning(
                "[review] Task '%s' aborted at stage '%s': %s — will retry.",
                task_id, exc.stage, exc.cause,
            )
            try:
                _store_infra_abort_result(task_id, exc, getattr(task, "budget_id", None),
                                          transition="conceptual_to_optimization")
            except Exception:
                pass
            return
        except Exception:
            logger.exception("[review] Pipeline for '%s' failed.", task_id)
            return

        try:
            _store_pipeline_result_generic(task_id, result, task.budget_id, "conceptual_to_optimization")

            if result.get("outcome") == "passed":
                update_task(task_id, type="optimization")
                logger.info("[review] Task '%s' advanced to OPTIMIZATION.", task_id)
            else:
                update_task(task_id, type="indev")
                logger.warning("[review] Task '%s' demoted to IN DEV.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[review] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_optimization_only_bg(task_id: str) -> None:
    """On-demand: run only the optimization pipeline (no security)."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.optimization import run_optimization_pipeline

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return
        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_optimization_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
            logger.info("[optimization-only] Task '%s': %s", task_id, result.get('outcome'))
        except ShutdownError:
            logger.info("[optimization-only] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[optimization-only] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[optimization-only] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[optimization-only] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_security_only_bg(task_id: str) -> None:
    """On-demand: run only the security review pipeline (no optimization)."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.security_review import run_security_pipeline

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return
        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            sec_result = loop.run_until_complete(
                run_security_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
            _store_pipeline_result_generic(task_id, sec_result, task.budget_id, "security_review")
            if sec_result.get("outcome") == "passed":
                update_task(task_id, type="security")
                logger.info("[security-only] Task '%s' advanced to SECURITY.", task_id)
            else:
                demotion = sec_result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "security", demotion, sec_result.get("summary", ""))
                logger.warning("[security-only] Task '%s' demoted to %s.", task_id, demotion)
        except ShutdownError:
            logger.info("[security-only] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[security-only] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[security-only] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[security-only] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_optimization_pipeline_bg(task_id: str) -> None:
    """Advance handler for CONCEPTUAL_REVIEW cards: run optimization, advance to security on pass."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.optimization import run_optimization_pipeline

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            opt_result = loop.run_until_complete(
                run_optimization_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
            _store_pipeline_result_generic(task_id, opt_result, task.budget_id, "optimization")
            logger.info("[optimization] Task '%s': %s", task_id, opt_result.get('outcome'))

            update_task(task_id, type="security")
            logger.info("[optimization] Task '%s' advanced to SECURITY.", task_id)
        except ShutdownError:
            logger.info("[optimization] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[optimization] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[optimization] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[optimization] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_security_pipeline_bg(task_id: str) -> None:
    """Advance handler for SECURITY cards: run security, advance to final_review on pass."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.security_review import run_security_pipeline

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            sec_result = loop.run_until_complete(
                run_security_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
            _store_pipeline_result_generic(task_id, sec_result, task.budget_id, "security_review")

            if sec_result.get("outcome") == "passed":
                update_task(task_id, type="final_review")
                logger.info("[security] Task '%s' passed. Advanced to FINAL REVIEW.", task_id)
            else:
                demotion = sec_result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "security", demotion, sec_result.get("summary", ""))
                logger.warning("[security] Task '%s' demoted to %s.", task_id, demotion)
        except ShutdownError:
            logger.info("[security] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[security] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[security] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[security] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_final_review_only_bg(task_id: str) -> None:
    """On-demand: run only the final review pipeline (no security, no stage advancement)."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.final_review import run_final_review_pipeline
        from app.agent.merge import execute_merge

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            fr_result = loop.run_until_complete(
                run_final_review_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                )
            )
            _store_pipeline_result_generic(task_id, fr_result, task.budget_id, "final_review")

            if fr_result.get("outcome") == "passed":
                logger.info("[final_review] Task '%s' passed. Running virtual merge test.", task_id)
                # Use real project root, not worktree path — git checkout fails inside a worktree.
                from app.database import get_project_path as _get_project_path
                real_pp = (_get_project_path(task.project) if task.project else None) or project_path
                merge_test = execute_merge(
                    task_id, project_path=real_pp, dry_run=True,
                    llm_id=task.llm_id, budget_id=task.budget_id,
                )
                if merge_test.status == "virtual_passed":
                    append_task_history(task_id, "ready_for_review", message="Final review passed. Virtual merge/test SUCCEEDED. Ready for final manual review and merge.")
                    logger.info("[final_review] Task '%s' virtual merge SUCCEEDED.", task_id)
                elif merge_test.status in ("conflict", "test_failure"):
                    msg = f"Final review passed, but virtual merge {merge_test.status.upper()}. Demoting to indev.\n\n{merge_test.error_detail or ''}"
                    append_task_history(task_id, "merge_test_failed", message=msg)
                    update_task(task_id, type="indev")
                    _record_demotion(task_id, "final_review", "indev", msg[:200])
                    logger.warning("[final_review] Task '%s' virtual merge %s. Demoted to indev.", task_id, merge_test.status)
                else:
                    append_task_history(task_id, "merge_test_failed", message=f"Final review passed, but VIRTUAL MERGE FAILED: {merge_test.status}. Detail: {merge_test.error_detail}")
                    logger.warning("[final_review] Task '%s' virtual merge FAILED: %s", task_id, merge_test.status)
            else:
                demotion = fr_result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "final_review", demotion, fr_result.get("summary", ""))
                logger.warning("[final_review] Task '%s' demoted to %s.", task_id, demotion)
        except ShutdownError:
            logger.info("[final_review] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[final_review] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[final_review] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[final_review] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


@_pipeline_session
def _run_final_review_pipeline_bg(task_id: str) -> None:
    """Advance handler for FINAL_REVIEW cards: run final review pipeline, advance to human_review on pass."""
    project_path = None
    worktree_path = None
    try:
        import asyncio
        from app.agent.final_review import run_final_review_pipeline
        from app.agent.merge import execute_merge

        task = get_task(task_id)
        if not task:
            return
        project_path = _setup_thread_context(task)
        worktree_path, _aborted = _setup_worktree(task_id, project_path)
        if _aborted:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            fr_result = loop.run_until_complete(
                run_final_review_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_path=worktree_path,
                    acceptance_criteria=json.loads(task.acceptance_criteria) if getattr(task, "acceptance_criteria", None) else None,
                )
            )
            _store_pipeline_result_generic(task_id, fr_result, task.budget_id, "final_review")

            if fr_result.get("outcome") == "passed":
                # Use real project root, not worktree path — git checkout fails inside a worktree.
                from app.database import get_project_path as _get_project_path
                real_pp = (_get_project_path(task.project) if task.project else None) or project_path
                merge_test = execute_merge(
                    task_id, project_path=real_pp, dry_run=True,
                    llm_id=task.llm_id, budget_id=task.budget_id,
                )
                if merge_test.status == "virtual_passed":
                    append_task_history(task_id, "ready_for_review", message="Final AI review passed. Virtual merge SUCCEEDED. Ready for human review.")
                    update_task(task_id, type="human_review")
                    logger.info("[final_review] Task '%s' advanced to HUMAN REVIEW.", task_id)
                elif merge_test.status in ("conflict", "test_failure"):
                    msg = f"Final AI review passed, but virtual merge {merge_test.status.upper()}. Demoting to indev.\n\n{merge_test.error_detail or ''}"
                    append_task_history(task_id, "merge_test_failed", message=msg)
                    update_task(task_id, type="indev")
                    _record_demotion(task_id, "final_review", "indev", msg[:200])
                    logger.warning("[final_review] Task '%s' virtual merge %s. Demoted to indev.", task_id, merge_test.status)
                else:
                    append_task_history(task_id, "merge_test_failed", message=f"Final AI review passed, but VIRTUAL MERGE FAILED: {merge_test.status}. Detail: {merge_test.error_detail}")
                    update_task(task_id, type="human_review")
                    logger.warning("[final_review] Task '%s' virtual merge infrastructure error (%s). Advanced to HUMAN REVIEW with warning.", task_id, merge_test.status)
            else:
                demotion = fr_result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "final_review", demotion, fr_result.get("summary", ""))
                logger.warning("[final_review] Task '%s' demoted to %s.", task_id, demotion)
        except ShutdownError:
            logger.info("[final_review] Pipeline for '%s' aborted due to server shutdown.", task_id)
        except PipelineAbortedError as exc:
            logger.warning("[final_review] Task '%s' aborted at stage '%s': %s — will retry.",
                           task_id, exc.stage, exc.cause)
        except Exception:
            logger.exception("[final_review] Pipeline for '%s' failed.", task_id)
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[final_review] Pipeline for '%s' failed.", task_id)
    finally:
        _teardown_worktree(task_id, project_path, worktree_path)


def _execute_merge_bg(task_id: str) -> None:
    """Background runner for merge to main."""
    try:
        from app.agent.merge import execute_merge
        from app.database import get_project_path

        task = get_task(task_id)
        project_path = None
        if task and task.project:
            project_path = get_project_path(task.project)

        result = execute_merge(task_id, project_path=project_path)

        if result.status == "merged":
            logger.info("[merge] Task '%s' merged to main (%s).", task_id, result.merge_commit_sha)
            _check_completion_rollup(task_id)
        elif result.status == "conflict":
            update_task(task_id, type="indev")
            _record_demotion(task_id, "merge", "indev", result.error_detail or "Merge conflict")
            logger.warning("[merge] Task '%s' merge conflict. Demoted to IN DEV.", task_id)
        elif result.status == "test_failure":
            update_task(task_id, type="indev")
            _record_demotion(task_id, "merge", "indev", result.error_detail or "Tests failed")
            logger.warning("[merge] Task '%s' tests failed after merge. Demoted to IN DEV.", task_id)
        elif result.status == "push_failure":
            update_task(task_id, type="human_review")
            _record_demotion(task_id, "merge", "human_review",
                             result.error_detail or "Push to remote failed")
            logger.error("[merge] Task '%s' push failed permanently. Demoted to human_review. %s",
                         task_id, result.error_detail)
        else:
            logger.error("[merge] Task '%s' merge error: %s", task_id, result.error_detail)
    except Exception as exc:
        logger.exception("[merge] Merge for '%s' failed.", task_id)


def _store_pipeline_result_generic(task_id: str, result: dict, budget_id: int | None, transition: str) -> None:
    """Store a transition result and its votes for any pipeline stage."""
    create_transition_result(
        task_id=task_id,
        transition=transition,
        outcome=result.get("outcome", "unknown"),
        vote_summary=result,
        total_prompt_tokens=result.get("total_prompt_tokens", 0),
        total_completion_tokens=result.get("total_completion_tokens", 0),
    )
    for vote in result.get("votes", []):
        create_transition_vote(
            task_id=task_id,
            transition=transition,
            stage=vote.get("stage", ""),
            verdict=vote.get("verdict", ""),
            confidence=vote.get("confidence", 0),
            justification=vote.get("justification", ""),
            raw_response=vote.get("raw_response"),
            prompt_tokens=vote.get("prompt_tokens", 0),
            completion_tokens=vote.get("completion_tokens", 0),
            model=vote.get("model", ""),
            budget_id=budget_id,
        )


def _record_demotion(task_id: str, from_stage: str, to_stage: str, reason: str) -> None:
    """Record a demotion event on a task."""
    from datetime import datetime, timezone
    from app.agent.pip_agent import generate_pip
    task = get_task(task_id)
    if not task:
        return
    history = task.demotion_history or []
    history.append({
        "from": from_stage,
        "to": to_stage,
        "reason": reason[:500],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })
    update_task(task_id, demotion_count=(task.demotion_count or 0) + 1, demotion_history=history)

    # Trigger PIP generation if demoted from a review stage
    review_stages = {"conceptual_review", "optimization", "security", "final_review", "human_review"}

    if from_stage in review_stages:
        logger.info("[pip] Triggering PIP generation for task '%s' demoted from '%s'.", task_id, from_stage)
        # Create a task in the running loop
        try:
            loop = asyncio.get_running_loop()
            loop.create_task(generate_pip(task_id, from_stage, reason))
        except RuntimeError:
            # Fallback if no loop is running (shouldn't happen in FastAPI context but for safety)
            asyncio.run(generate_pip(task_id, from_stage, reason))

# Pipeline handler dispatch table
ADVANCE_HANDLERS = {
    "idea": "_run_intake_pipeline",
    "planning": "_run_planning_pipeline_bg",
    "indev": "_run_dev_orchestrator_bg",
    "conceptual_review": "_run_optimization_pipeline_bg",
    "optimization": "_run_security_pipeline_bg",
    "security": "_run_security_pipeline_bg",
    "final_review": "_run_final_review_pipeline_bg",
    # "human_review": "_execute_merge_bg",  # DISABLED: Requires manual review/merge
}


@app.post("/api/tasks/{task_id}/advance", response_model=dict)
def advance_task(task_id: str):
    """
    Request advancement of a task to the next column.
    Detects current column and dispatches the appropriate pipeline.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    current_type = task.type
    if current_type not in ADVANCE_HANDLERS:
        raise HTTPException(
            status_code=422,
            detail=f"Task is in '{current_type}' column. Cannot advance from this column."
        )

    # Check required fields
    if not task.description:
        raise HTTPException(status_code=422, detail="Task must have a description before advancing.")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM endpoint assigned before advancing.")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned before advancing.")

    # Clarification gate: IDEA cards must be reviewed before entering the pipeline.
    # 'none' is no longer accepted — all IDEA cards go through clarification first.
    if current_type == 'idea':
        cs = getattr(task, 'clarification_status', 'none')
        if cs not in ('approved', 'skipped'):
            raise HTTPException(
                status_code=400,
                detail={
                    "error": "clarification_required",
                    "clarification_status": cs,
                    "message": "Review and approve this card's intake draft before running the pipeline.",
                }
            )

    handler_name = ADVANCE_HANDLERS[current_type]

    # Dispatch to appropriate handler
    if handler_name == "_run_intake_pipeline":
        _start_bg(_run_intake_pipeline, task_id)
    elif handler_name == "_run_planning_pipeline_bg":
        _start_bg(_run_planning_pipeline_bg, task_id)
    elif handler_name == "_run_dev_orchestrator_bg":
        _start_bg(_run_dev_orchestrator_bg, task_id)
    elif handler_name == "_run_optimization_pipeline_bg":
        _start_bg(_run_optimization_pipeline_bg, task_id)
    elif handler_name == "_run_security_pipeline_bg":
        _start_bg(_run_security_pipeline_bg, task_id)
    elif handler_name == "_run_final_review_pipeline_bg":
        _start_bg(_run_final_review_pipeline_bg, task_id)
    elif handler_name == "_execute_merge_bg":
        _start_bg(_execute_merge_bg, task_id)

    return {
        "task_id": task_id,
        "status": "PIPELINE_STARTED",
        "message": f"Pipeline started for task '{task_id}' (from {current_type}). Poll /api/tasks/{task_id}/transition-status for updates."
    }


# ============================================
# Intake Clarification Endpoints
# ============================================

@app.get("/api/tasks/{task_id}/clarification", response_model=dict)
def get_clarification(task_id: str):
    """Get the current intake draft for an IDEA card."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    draft = get_intake_draft(task_id)
    if not draft:
        raise HTTPException(status_code=404, detail="No intake draft found for this task.")
    return {
        "clarification_status": getattr(task, 'clarification_status', 'none'),
        "description_original": getattr(task, 'description_original', None),
        "draft": intake_draft_to_dict(draft),
    }


@app.get("/api/tasks/{task_id}/clarification/trace", response_model=dict)
def get_clarification_trace(task_id: str):
    """Return the clarification agent's LLM call trace for the intake modal investigation viewer."""
    import json as _json

    def _extract_fields(response_data_raw):
        try:
            data = _json.loads(response_data_raw or "{}")
            choice = (data.get("choices") or [{}])[0]
            msg = choice.get("message", {})
            content = (msg.get("content") or "").strip()
            reasoning = (msg.get("reasoning_content") or "").strip()
            tool_calls = msg.get("tool_calls") or []
            tools_used = []
            for tc in tool_calls:
                fn = tc.get("function", {})
                name = fn.get("name", "")
                try:
                    args = _json.loads(fn.get("arguments", "{}"))
                    # Extract the most meaningful arg (path, query, etc.)
                    label = (args.get("path") or args.get("query") or args.get("pattern")
                             or args.get("command") or next(iter(args.values()), ""))
                    if isinstance(label, str) and len(label) > 60:
                        label = label[:60] + "…"
                except Exception:
                    label = ""
                tools_used.append({"name": name, "arg": label})
            return {
                "finish_reason": choice.get("finish_reason", ""),
                "content_preview": content[:500] if content else "",
                "reasoning_preview": reasoning[:300] if reasoning else "",
                "tools_used": tools_used,
            }
        except Exception:
            return {"finish_reason": "", "content_preview": "", "reasoning_preview": "", "tools_used": []}

    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    from app.database import get_budget_entries
    all_entries = get_budget_entries(task_id=task_id, limit=200)
    # Filter to Clarification Agent, reverse to chronological order
    entries = [e for e in reversed(all_entries) if e.agent_name == "Clarification Agent"]

    result = []
    for i, entry in enumerate(entries):
        fields = _extract_fields(entry.response_data)
        result.append({
            "step": i + 1,
            "id": entry.id,
            "created_at": entry.created_at.isoformat() if entry.created_at else None,
            "prompt_tokens": entry.prompt_cost,
            "completion_tokens": entry.generation_cost,
            "finish_reason": fields["finish_reason"],
            "reasoning_preview": fields["reasoning_preview"],
            "content_preview": fields["content_preview"],
            "tools_used": fields["tools_used"],
            "is_final": fields["finish_reason"] == "stop",
        })

    return {"task_id": task_id, "steps": result, "total": len(result)}


@app.post("/api/tasks/{task_id}/clarification/message", response_model=dict)
def clarification_message(task_id: str, body: dict = Body(...)):
    """Send a refinement message and get an updated draft in return."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    draft = get_intake_draft(task_id)
    if not draft:
        raise HTTPException(status_code=404, detail="No intake draft for this task.")

    user_message = body.get("message", "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message is required.")

    # Build a one-shot LLM conversation to refine the draft
    import asyncio as _asyncio
    from app.agent.llm_client import call_llm as _call_llm
    from database import get_project_path
    import json as _json

    draft_dict = intake_draft_to_dict(draft)
    history = draft_dict.get("conversation_history") or []

    # Build system + conversation + user message
    system = (
        "You are helping a developer refine an IDEA card specification. "
        "You have access to the current draft. When the user asks for changes, "
        "update the relevant fields and return the FULL updated draft as JSON "
        "inside a ```json block, followed by a brief conversational response explaining what you changed."
    )
    current_draft_summary = _json.dumps({
        "rewritten_description": draft_dict.get("rewritten_description"),
        "acceptance_criteria": draft_dict.get("acceptance_criteria"),
        "out_of_scope": draft_dict.get("out_of_scope"),
        "open_questions": draft_dict.get("open_questions"),
        "suggested_prerequisites": draft_dict.get("suggested_prerequisites"),
        "suggested_subtasks": draft_dict.get("suggested_subtasks"),
    }, indent=2)

    messages = [{"role": "system", "content": system}]
    messages.append({"role": "user", "content": f"Current draft:\n```json\n{current_draft_summary}\n```"})
    for msg in history[-6:]:  # last 3 exchanges
        messages.append({"role": msg["role"] if msg["role"] in ("user", "assistant") else "user", "content": msg["content"]})
    messages.append({"role": "user", "content": user_message})

    llm = None
    if task.llm_id:
        from database import get_llm as _get_llm
        llm = _get_llm(task.llm_id)

    try:
        response = _asyncio.run(_call_llm(
            messages,
            base_url=f"http://{llm.address}:{llm.port}/v1" if llm else None,
            model=llm.model if llm else None,
            tools=None,
            task_id=task_id,
            llm_id=task.llm_id,
            budget_id=task.budget_id,
            agent_name="Clarification Chat",
        ))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"LLM call failed: {exc}")

    assistant_content = response.get("choices", [{}])[0].get("message", {}).get("content", "")

    # Try to extract updated draft JSON from response
    from app.agent.json_utils import extract_json_block as _extract_json
    import json as _json2
    updated_draft_fields: dict = {}
    raw_json = _extract_json(assistant_content)
    if raw_json:
        try:
            parsed = _json2.loads(raw_json)
            if isinstance(parsed, dict) and "rewritten_description" in parsed:
                updated_draft_fields = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    # Extract conversational response (text after the JSON block)
    chat_response = assistant_content
    if "```" in assistant_content:
        parts = assistant_content.split("```")
        # Text after the last code block
        after_blocks = [p for i, p in enumerate(parts) if i % 2 == 0]
        chat_response = "\n".join(p.strip() for p in after_blocks if p.strip()) or assistant_content

    # Persist conversation turn
    append_conversation_message(task_id, "user", user_message)
    append_conversation_message(task_id, "assistant", chat_response)

    # Apply updated fields if present
    update_kwargs = {}
    for field in ("rewritten_description", "acceptance_criteria", "out_of_scope",
                  "open_questions", "suggested_prerequisites", "suggested_subtasks"):
        if field in updated_draft_fields:
            update_kwargs[field] = updated_draft_fields[field]
    if update_kwargs:
        update_intake_draft(task_id, **update_kwargs)

    updated_draft = get_intake_draft(task_id)
    return {
        "response": chat_response,
        "updated_draft": intake_draft_to_dict(updated_draft) if updated_draft else None,
    }


@app.post("/api/tasks/{task_id}/clarification/approve", response_model=dict)
def approve_clarification(task_id: str, body: dict = Body(default={})):
    """Approve the intake draft: update task description, set prerequisites, create subtasks."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    draft = get_intake_draft(task_id)
    if not draft:
        raise HTTPException(status_code=404, detail="No intake draft for this task.")

    draft_dict = intake_draft_to_dict(draft)

    # Use the user-edited rewritten description (may differ from agent's if they edited the textarea)
    approved_description = body.get("rewritten_description") or draft_dict.get("rewritten_description") or task.description

    # Prerequisites: user may have checked a subset of the suggestions
    apply_prerequisites = body.get("apply_prerequisites")  # list of task IDs or None (use all suggested)
    if apply_prerequisites is None:
        suggested = draft_dict.get("suggested_prerequisites") or []
        apply_prerequisites = [s["task_id"] for s in suggested if isinstance(s, dict) and "task_id" in s]

    # Subtasks: user may have checked a subset
    apply_subtasks = body.get("apply_subtasks")  # list of {title, description} dicts or None
    if apply_subtasks is None:
        apply_subtasks = draft_dict.get("suggested_subtasks") or []

    # Apply changes to the task
    update_kwargs = {
        "description": approved_description,
        "clarification_status": "approved",
    }
    if apply_prerequisites:
        update_kwargs["prerequisites"] = apply_prerequisites
    # Copy acceptance criteria from the approved draft to the task
    acceptance_criteria = draft_dict.get("acceptance_criteria")
    if acceptance_criteria:
        update_kwargs["acceptance_criteria"] = json.dumps(acceptance_criteria) if isinstance(acceptance_criteria, list) else acceptance_criteria
    update_task(task_id, **update_kwargs)

    # Create sub-tasks for approved subtasks
    created_subtasks = []
    for sub in apply_subtasks:
        if not isinstance(sub, dict) or not sub.get("title"):
            continue
        sub_task = create_task(
            title=sub["title"],
            task_type="idea",
            description=sub.get("description", ""),
            owner=task.owner or "user",
            llm_id=task.llm_id,
            budget_id=task.budget_id,
            prerequisites=[task_id],
            project=task.project or "TheMaestro",
        )
        if sub_task:
            # Mark sub-tasks as skipped clarification so they can advance immediately
            update_task(sub_task.id, clarification_status="skipped")
            created_subtasks.append({"id": sub_task.id, "title": sub_task.title})

    return {
        "status": "approved",
        "task_id": task_id,
        "created_subtasks": created_subtasks,
    }


@app.post("/api/tasks/{task_id}/clarification/skip", response_model=dict)
def skip_clarification(task_id: str):
    """Skip the intake clarification for this card (keep original description)."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    update_task(task_id, clarification_status="skipped")
    return {"status": "skipped", "task_id": task_id}


@app.post("/api/tasks/{task_id}/clarification/retrigger", response_model=dict)
def retrigger_clarification(task_id: str):
    """Re-run the clarification agent on a card that has already been approved/skipped."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")
    if task.type != 'idea':
        raise HTTPException(status_code=400, detail="Only IDEA cards can be re-clarified.")
    update_task(task_id,
                clarification_status='pending',
                description_original=task.description or '')
    existing = get_intake_draft(task_id)
    if existing:
        update_intake_draft(task_id,
                            rewritten_description=None,
                            design_rationale=None,
                            acceptance_criteria=[],
                            out_of_scope=None,
                            open_questions=[],
                            suggested_prerequisites=[],
                            suggested_subtasks=[],
                            conversation_history=[],
                            agent_token_cost=0)
    else:
        create_intake_draft(task_id)
    # Scheduler picks up the pending status on the next tick at highest priority
    return {"status": "retriggered", "task_id": task_id}


@app.get("/api/tasks/{task_id}/transition-status", response_model=dict)
def get_transition_status(task_id: str):
    """Get the latest transition pipeline result for a task."""
    results = get_transition_results(task_id)
    if not results:
        return {"task_id": task_id, "status": "no_transitions"}

    latest = results[0]  # ordered by created_at desc
    votes = get_transition_votes(task_id, latest.transition)

    # Build full history of all transition results for this task
    all_results = []
    for result in results:
        result_votes = get_transition_votes(task_id, result.transition)
        # Filter votes by matching created_at window (within same result)
        all_results.append({
            "transition": result.transition,
            "outcome": result.outcome,
            "vote_summary": result.vote_summary,
            "votes": [
                {
                    "stage": v.stage,
                    "verdict": v.verdict,
                    "confidence": v.confidence,
                    "justification": v.justification,
                    "model": v.model,
                    "prompt_tokens": v.prompt_tokens,
                    "completion_tokens": v.completion_tokens,
                }
                for v in result_votes
            ],
            "total_prompt_tokens": result.total_prompt_tokens,
            "total_completion_tokens": result.total_completion_tokens,
            "created_at": result.created_at.isoformat() if result.created_at else None,
        })

    latest_entry = all_results[0]
    return {
        "task_id": task_id,
        "transition": latest_entry["transition"],
        "outcome": latest_entry["outcome"],
        "vote_summary": latest_entry["vote_summary"],
        "votes": latest_entry["votes"],
        "total_prompt_tokens": latest_entry["total_prompt_tokens"],
        "total_completion_tokens": latest_entry["total_completion_tokens"],
        "created_at": latest_entry["created_at"],
        "history": all_results,
    }


@app.get("/api/tasks/{task_id}/planning-result", response_model=dict)
def get_task_planning_result(task_id: str):
    """Get the planning result for a task.

    Returns the most-recent row regardless of status so the Stage Journal
    can display in_progress and failed states, not just completed ones.
    """
    from app.database import get_latest_planning_result
    result = get_latest_planning_result(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="No planning result found")
    # Short-circuit for non-terminal states
    if result.status == 'in_progress':
        return {"status": "in_progress", "task_id": task_id}
    if result.status == 'failed':
        return {
            "status": "failed",
            "task_id": task_id,
            "error_message": result.error_message or "Unknown error",
        }
    gate_checks = json.loads(result.gate_checks) if result.gate_checks else None
    return {
        "id": result.id, "task_id": result.task_id,
        "design_rationale": json.loads(result.codebase_survey)
            if result.codebase_survey and result.codebase_survey.startswith('[') else result.codebase_survey,
        "file_manifest": json.loads(result.file_manifest) if result.file_manifest else [],
        "dependency_graph": json.loads(result.dependency_graph) if result.dependency_graph else {},
        "interface_contracts": json.loads(result.interface_contracts) if result.interface_contracts else [],
        "test_strategy": json.loads(result.test_strategy) if result.test_strategy else [],
        "implementation_steps": json.loads(result.implementation_steps) if result.implementation_steps else [],
        "pitfalls_identified": json.loads(result.pitfalls_identified) if result.pitfalls_identified else [],
        "review_votes": json.loads(result.review_votes) if result.review_votes else [],
        "gate_checks": gate_checks,
        "gate_passed": (len([c for c in gate_checks if not c.get("passed") and c.get("hard_fail")]) == 0)
                       if gate_checks is not None else None,
        "confidence": result.confidence,
        "selected_design_index": result.selected_design_index,
        "status": result.status,
        "created_at": result.created_at.isoformat() if result.created_at else None,
    }


@app.get("/api/tasks/{task_id}/stage-summary", response_model=dict)
def get_task_stage_summary(task_id: str):
    """Compact per-stage status summary for the card footer."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    _verdict_order = ["REJECTED", "NOT_SUITABLE", "NEEDS_RESEARCH", "POSSIBLE", "LIKELY"]

    summary = {
        "task_id": task_id,
        "current_stage": task.type,
        "planning": {"has_result": False},
        "components": {"total": 0, "done": 0, "pending": 0, "failed": 0, "failing": [], "files_changed": 0},
        "optimization": {"has_result": False, "outcome": None, "improvement_summary": None},
        "security": {"has_result": False, "worst_verdict": None, "critical_count": 0, "high_count": 0},
        "final_review": {"has_result": False, "worst_verdict": None},
        "human_review": {"has_result": False, "worst_verdict": None},
        "merge": {"status": None, "branch_name": None},
        "blocking_issue": None,
    }

    # Planning result
    pr = get_planning_result(task_id)
    if pr:
        fm = json.loads(pr.file_manifest or "[]")
        steps = json.loads(pr.implementation_steps or "[]")
        gate_checks = json.loads(pr.gate_checks) if pr.gate_checks else None
        gate_passed = None
        gate_failing = []
        if gate_checks is not None:
            gate_failing = [c["name"] for c in gate_checks if not c.get("passed") and c.get("hard_fail")]
            gate_passed = len(gate_failing) == 0
        summary["planning"] = {
            "has_result": True,
            "file_count": len(fm),
            "step_count": len(steps),
            "confidence": pr.confidence or 0,
            "gate_passed": gate_passed,
            "gate_failing_checks": gate_failing,
        }

    # Component results
    comps = get_component_results(task_id)
    if comps:
        done = sum(1 for c in comps if c.status == "done")
        failed = sum(1 for c in comps if c.status == "failed")
        pending = sum(1 for c in comps if c.status in ("pending", "running"))
        failing_names = [c.component_name for c in comps if c.status == "failed"][:3]
        files_changed = sum(len(json.loads(c.files_changed or "[]")) for c in comps)
        summary["components"] = {
            "total": len(comps),
            "done": done,
            "pending": pending,
            "failed": failed,
            "failing": failing_names,
            "files_changed": files_changed,
        }

    # Optimization result
    opt = get_optimization_result(task_id)
    if opt:
        summary["optimization"] = {
            "has_result": True,
            "outcome": opt.outcome,
            "improvement_summary": opt.improvement_summary,
        }

    # Security reviews
    sec = get_security_review_results(task_id)
    if sec:
        verdicts = [s.verdict for s in sec]
        worst = next((v for v in _verdict_order if v in verdicts), None)
        summary["security"] = {
            "has_result": True,
            "worst_verdict": worst,
            "critical_count": sum(s.critical_count or 0 for s in sec),
            "high_count": sum(s.high_count or 0 for s in sec),
        }

    # Final review
    fr = get_final_review_results(task_id)
    if fr:
        verdicts = [r.verdict for r in fr]
        worst = next((v for v in _verdict_order if v in verdicts), None)
        summary["final_review"] = {"has_result": True, "worst_verdict": worst}

    # Merge record
    mr = get_merge_record(task_id)
    if mr:
        summary["merge"] = {"status": mr.status, "branch_name": mr.branch_name}

    # Compute blocking_issue
    stage = task.type
    if stage == "planning" and summary["planning"].get("has_result"):
        if summary["planning"].get("gate_passed") is False:
            failing = summary["planning"].get("gate_failing_checks", [])
            summary["blocking_issue"] = f"gate: {', '.join(failing)}" if failing else "gate failed"
    elif stage == "indev" and summary["components"]["failed"] > 0:
        failing = summary["components"]["failing"]
        summary["blocking_issue"] = f"failed: {failing[0]}" if failing else "component failed"
    elif stage == "security" and summary["security"]["has_result"]:
        if summary["security"]["worst_verdict"] in ("REJECTED", "NOT_SUITABLE"):
            c = summary["security"]["critical_count"]
            summary["blocking_issue"] = f"security {summary['security']['worst_verdict']}" + (f" · {c} critical" if c else "")
    elif stage in ("final_review", "human_review") and summary["final_review"]["has_result"]:
        if summary["final_review"]["worst_verdict"] in ("REJECTED", "NOT_SUITABLE"):
            summary["blocking_issue"] = f"review {summary['final_review']['worst_verdict']}"

    return summary


@app.get("/api/tasks/{task_id}/diff", response_model=dict)
def get_task_diff(task_id: str, max_bytes: int = 65536):
    """Return the git diff for all changes made on the task's branch.

    For in-progress tasks (INDEV → FINAL_REVIEW): diffs the task branch
    against the project's main/master.
    For completed/merged tasks: diffs the merge commit against its parent.

    max_bytes caps the returned diff to avoid huge payloads (default 64 KiB).
    """
    import subprocess
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.database import get_project_path
    project_path = get_project_path(task.project) if task.project else None
    if not project_path:
        raise HTTPException(status_code=422, detail="Task has no associated project path")

    from app.agent.config import GIT_SAFETY_BRANCH_PREFIX
    branch = f"{GIT_SAFETY_BRANCH_PREFIX}{task_id}"

    def _run(*args, cwd=None):
        result = subprocess.run(
            args, cwd=cwd or project_path,
            capture_output=True, text=True, timeout=15
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()

    # Determine base branch (main or master)
    rc, out, _ = _run("git", "rev-parse", "--verify", "main")
    base_branch = "main" if rc == 0 else "master"

    # Check if the task branch exists locally
    rc_branch, _, _ = _run("git", "rev-parse", "--verify", branch)
    branch_exists = rc_branch == 0

    # Check for a merge commit in merge_records
    mr = get_merge_record(task_id)
    merge_sha = mr.merge_commit_sha if mr else None

    method = None
    diff_text = ""
    stat_text = ""
    base_ref = None
    head_ref = None

    if branch_exists:
        # Branch still exists — diff it against base
        method = "branch"
        base_ref = base_branch
        head_ref = branch
        rc, diff_text, err = _run("git", "diff", f"{base_branch}...{branch}")
        if rc != 0:
            # Fallback: try two-dot diff
            rc, diff_text, err = _run("git", "diff", f"{base_branch}..{branch}")
        rc2, stat_text, _ = _run("git", "diff", "--stat", f"{base_branch}...{branch}")
    elif merge_sha:
        # Branch was merged — show the merge commit
        method = "merge_commit"
        base_ref = f"{merge_sha}^1"
        head_ref = merge_sha
        rc, diff_text, err = _run("git", "diff", f"{merge_sha}^1", merge_sha)
        rc2, stat_text, _ = _run("git", "diff", "--stat", f"{merge_sha}^1", merge_sha)
    else:
        return {
            "task_id": task_id,
            "branch": branch,
            "method": None,
            "diff": "",
            "stat": "",
            "error": "No task branch found and no merge commit recorded. INDEV may not have started yet.",
        }

    # Truncate large diffs
    truncated = False
    if len(diff_text) > max_bytes:
        diff_text = diff_text[:max_bytes]
        # Cut at last complete line
        last_nl = diff_text.rfind("\n")
        if last_nl > 0:
            diff_text = diff_text[:last_nl]
        truncated = True

    return {
        "task_id": task_id,
        "branch": branch,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "method": method,
        "stat": stat_text,
        "diff": diff_text,
        "truncated": truncated,
        "error": None,
    }


@app.get("/api/tasks/{task_id}/component-status", response_model=list)
def get_task_component_status(task_id: str):
    """Get component agent statuses for a task."""
    results = get_component_results(task_id)
    return [
        {
            "id": r.id, "component_name": r.component_name,
            "batch_number": r.batch_number, "step_order": r.step_order,
            "status": r.status, "turns_used": r.turns_used,
            "files_changed": json.loads(r.files_changed) if r.files_changed else [],
            "error_detail": r.error_detail,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in results
    ]


@app.get("/api/tasks/{task_id}/optimization-status", response_model=dict)
def get_task_optimization_status(task_id: str):
    """Get optimization pipeline status for a task."""
    result = get_optimization_result(task_id)
    if not result:
        return {"task_id": task_id, "status": "not_run"}
    return {
        "outcome": result.outcome,
        "improvement_summary": result.improvement_summary,
        "winning_proposal_index": result.winning_proposal_index,
        "created_at": result.created_at.isoformat() if result.created_at else None,
    }


@app.get("/api/tasks/{task_id}/security-status", response_model=list)
def get_task_security_status(task_id: str):
    """Get security review findings for a task."""
    results = get_security_review_results(task_id)
    return [
        {
            "reviewer_type": r.reviewer_type, "verdict": r.verdict,
            "confidence": r.confidence, "justification": r.justification,
            "critical_count": r.critical_count, "high_count": r.high_count,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in results
    ]


@app.get("/api/tasks/{task_id}/final-review-status", response_model=list)
def get_task_final_review_status(task_id: str):
    """Get final review findings for a task."""
    results = get_final_review_results(task_id)
    return [
        {
            "reviewer_type": r.reviewer_type, "verdict": r.verdict,
            "confidence": r.confidence, "justification": r.justification,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in results
    ]


@app.get("/api/tasks/{task_id}/merge-status", response_model=dict)
def get_task_merge_status(task_id: str):
    """Get merge status for a task."""
    record = get_merge_record(task_id)
    if not record:
        return {"task_id": task_id, "status": "not_merged"}
    return {
        "branch_name": record.branch_name,
        "merge_commit_sha": record.merge_commit_sha,
        "status": record.status,
        "error_detail": record.error_detail,
        "created_at": record.created_at.isoformat() if record.created_at else None,
    }


@app.get("/api/tasks/{task_id}/audit-trail", response_model=dict)
def get_task_audit_trail(task_id: str):
    """Get the full audit trail for a task across all pipeline stages."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    trail = {
        "task_id": task_id,
        "current_type": task.type,
        "demotion_count": getattr(task, "demotion_count", 0) or 0,
        "demotion_history": getattr(task, "demotion_history", None),
        "review_notes": getattr(task, "review_notes", None),
        "transitions": [],
        "planning": None,
        "components": [],
        "optimization": None,
        "security_reviews": [],
        "final_reviews": [],
        "merge": None,
    }

    # Transition results
    results = get_transition_results(task_id)
    for r in results:
        trail["transitions"].append({
            "transition": r.transition,
            "outcome": r.outcome,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    # Planning
    pr = get_planning_result(task_id)
    if pr:
        trail["planning"] = {"status": pr.status, "created_at": pr.created_at.isoformat() if pr.created_at else None}

    # Components
    comps = get_component_results(task_id)
    for c in comps:
        trail["components"].append({
            "component_name": c.component_name,
            "status": c.status,
            "created_at": c.created_at.isoformat() if c.created_at else None,
        })

    # Optimization
    opt = get_optimization_result(task_id)
    if opt:
        trail["optimization"] = {"status": opt.status, "created_at": opt.created_at.isoformat() if opt.created_at else None}

    # Security reviews
    sec_reviews = get_security_review_results(task_id)
    for s in sec_reviews:
        trail["security_reviews"].append({
            "reviewer_type": s.reviewer_type,
            "verdict": s.verdict,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        })

    # Final reviews
    fr_reviews = get_final_review_results(task_id)
    for f in fr_reviews:
        trail["final_reviews"].append({
            "reviewer_type": f.reviewer_type,
            "verdict": f.verdict,
            "created_at": f.created_at.isoformat() if f.created_at else None,
        })

    # Merge
    mr = get_merge_record(task_id)
    if mr:
        trail["merge"] = {
            "status": mr.status,
            "merge_commit_sha": mr.merge_commit_sha,
            "created_at": mr.created_at.isoformat() if mr.created_at else None,
        }

    return trail


# ============================================
# Subdivision API Endpoints
# ============================================

@app.get("/api/tasks/{task_id}/children", response_model=List[dict])
def get_task_children(task_id: str):
    """Get direct child tasks of a subdivided task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    children = get_child_tasks(task_id)
    return [task_to_dict(c) for c in children]


@app.get("/api/tasks/{task_id}/subdivision-records", response_model=List[dict])
def get_task_subdivision_records(task_id: str):
    """Get the subdivision audit trail for a task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    records = get_subdivision_records(task_id)
    return [
        {
            "id": r.id,
            "parent_task_id": r.parent_task_id,
            "attempt_number": r.attempt_number,
            "generation": r.generation,
            "child_task_ids": r.child_task_ids,
            "rejection_context": r.rejection_context,
            "agent_vote": r.agent_vote,
            "prompt_tokens": r.prompt_tokens,
            "completion_tokens": r.completion_tokens,
            "status": r.status,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


@app.post("/api/tasks/{task_id}/regenerate-subdivision", status_code=202)
def regenerate_subdivision_endpoint(task_id: str):
    """Queue a new subdivision agent run for a Big Idea task.

    Cancels the current active children, supersedes their record, then re-runs
    the subdivision agent to produce a fresh set of child ideas.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.is_big_idea:
        raise HTTPException(status_code=400, detail="Task is not a Big Idea")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM assigned before regenerating")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned before regenerating")
    _start_bg(_run_regenerate_subdivision, task_id)
    return {"status": "queued"}


@app.post("/api/tasks/{task_id}/subdivision-records/{record_id}/activate")
def activate_subdivision_record_endpoint(task_id: str, record_id: int):
    """Make a previous subdivision record the active one.

    Cancels children belonging to all OTHER records and un-cancels the
    children of the selected record (restoring them to 'idea' status).
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    records = get_subdivision_records(task_id)
    target = next((r for r in records if r.id == record_id), None)
    if not target:
        raise HTTPException(status_code=404, detail="Subdivision record not found")

    target_ids = set(target.child_task_ids or [])
    children = get_child_tasks(task_id)

    for child in children:
        if child.id in target_ids:
            if child.type == 'cancelled':
                update_task(child.id, type='idea')
        else:
            if child.type != 'cancelled':
                update_task(child.id, type='cancelled')

    for r in records:
        new_status = 'active' if r.id == record_id else ('superseded' if r.status == 'active' else r.status)
        if new_status != r.status:
            update_subdivision_record(r.id, status=new_status)

    return {"status": "activated", "record_id": record_id}


# ============================================
# Git Diff & Branch View API Endpoints
# ============================================

@app.get("/api/tasks/{task_id}/branch", response_model=dict)
def get_task_branch(task_id: str):
    """Get the git branch name associated with a task.

    Returns the branch name if the task has a git branch created for it,
    otherwise returns None. This is useful for tasks in development, review,
    or completed stages where code changes have been made.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.agent.config import GIT_SAFETY_BRANCH_PREFIX
    branch_name = f"{GIT_SAFETY_BRANCH_PREFIX}{task_id}"

    return {
        "task_id": task_id,
        "branch_name": branch_name,
        "exists": branch_name  # The agent tools will validate if it exists
    }


# ============================================
# Research Jobs API
# ============================================

def _research_job_to_dict(job) -> dict:
    return {
        "id": job.id,
        "task_id": job.task_id,
        "status": job.status,
        "priority": job.priority,
        "depth": job.depth,
        "question": job.question,
        "findings": job.findings,
        "verdict": job.verdict,
        "lives_used": job.lives_used,
        "prompt_tokens": job.prompt_tokens,
        "completion_tokens": job.completion_tokens,
        "llm_id": job.llm_id,
        "budget_id": job.budget_id,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@app.get("/api/tasks/{task_id}/research-jobs", response_model=List[dict])
def get_task_research_jobs(task_id: str):
    """Get all research jobs for a task, most recent first."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    jobs = get_research_jobs_for_task(task_id)
    return [_research_job_to_dict(j) for j in jobs]


# ============================================
# Ad-hoc Agent Toolbar Routes
# ============================================

# In-memory status registry for toolbar-triggered research jobs
_ADHOC_RESEARCH_JOBS: dict[int, dict] = {}  # job_id -> {status, findings, verdict, error}


@_pipeline_session
def _run_adhoc_research(task_id: str, question: str, job_id: int) -> None:
    """Background runner: executes a standalone research agent triggered from the card toolbar."""
    import asyncio
    from app.agent.research import run_research
    from app.database import get_project_path as _get_project_path

    _ADHOC_RESEARCH_JOBS[job_id] = {"status": "running"}
    try:
        task = get_task(task_id)
        if not task:
            _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": "Task not found"}
            update_research_job(job_id, status="failed")
            return

        _setup_thread_context(task)
        llm_base_url, llm_model, _ = _resolve_llm_endpoint(task)
        project_root = _get_project_path(task.project) if task.project else None

        context = {
            "task_id": task.id,
            "task_title": task.title,
            "task_description": task.description or "",
            "task_type": task.type,
        }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_research(
                    question=question,
                    context=context,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    task_id=task_id,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_root=project_root,
                )
            )
        except ShutdownError:
            logger.info("[adhoc-research] Aborted for task '%s' due to server shutdown.", task_id)
            _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": "Server is shutting down"}
            update_research_job(job_id, status="failed")
            return
        except Exception:
            logger.exception("[toolbar] Ad-hoc research for task '%s' failed.", task_id)
            return
        finally:
            loop.close()

        vote = result.vote or {}
        update_research_job(
            job_id,
            status="completed",
            findings=result.findings or "",
            verdict=vote.get("verdict", ""),
            lives_used=getattr(result, "lives_used", 1),
            prompt_tokens=getattr(result, "prompt_tokens", 0),
            completion_tokens=getattr(result, "completion_tokens", 0),
        )
        _ADHOC_RESEARCH_JOBS[job_id] = {
            "status": "completed",
            "findings": result.findings or "",
            "verdict": vote.get("verdict", ""),
        }
    except Exception as exc:
        logger.exception("[toolbar] Ad-hoc research for task '%s' failed.", task_id)
        err = str(exc)
        update_research_job(job_id, status="failed")
        _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": err}


@app.post("/api/agent/research/{task_id}", response_model=dict)
def start_adhoc_research(task_id: str, body: dict):
    """Start an ad-hoc research agent on a task from the card toolbar."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM assigned")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned")

    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="'question' is required")

    job = create_research_job(
        task_id=task_id,
        question=question,
        llm_id=task.llm_id,
        budget_id=task.budget_id,
    )
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create research job")

    _start_bg(_run_adhoc_research, task_id, question, job.id)
    return {"job_id": job.id, "status": "queued"}


@app.get("/api/agent/research/{task_id}/status", response_model=dict)
def get_adhoc_research_status(task_id: str, job_id: int):
    """Poll the status of a toolbar-triggered research job."""
    in_mem = _ADHOC_RESEARCH_JOBS.get(job_id)
    if in_mem:
        return in_mem

    # Fall back to DB record (e.g. after server restart)
    job = get_research_job(job_id)
    if not job or job.task_id != task_id:
        raise HTTPException(status_code=404, detail="Research job not found")
    return {
        "status": job.status,
        "findings": job.findings or "",
        "verdict": job.verdict or "",
    }


@_pipeline_session
def _run_adhoc_investigation(task_id: str, question: str, job_id: int) -> None:
    """Background runner: executes an InvestigationAgent triggered from the card toolbar."""
    import asyncio
    from app.agent.research import run_investigation
    from app.database import get_project_path as _get_project_path

    _ADHOC_RESEARCH_JOBS[job_id] = {"status": "running"}
    try:
        task = get_task(task_id)
        if not task:
            _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": "Task not found"}
            update_research_job(job_id, status="failed")
            return

        _setup_thread_context(task)
        llm_base_url, llm_model, _ = _resolve_llm_endpoint(task)
        project_root = _get_project_path(task.project) if task.project else None

        context = {
            "task_id": task.id,
            "task_title": task.title,
            "task_description": task.description or "",
            "task_type": task.type,
            "task_project": task.project or "",
        }

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_investigation(
                    question=question,
                    context=context,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    task_id=task_id,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                    project_root=project_root,
                )
            )
        except ShutdownError:
            logger.info("[adhoc-investigation] Aborted for task '%s' due to server shutdown.", task_id)
            _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": "Server is shutting down"}
            update_research_job(job_id, status="failed")
            return
        except Exception:
            logger.exception("[toolbar] Ad-hoc investigation for task '%s' failed.", task_id)
            return
        finally:
            loop.close()

        report = result.report or {}
        report_json = json.dumps(report)
        update_research_job(
            job_id,
            status="completed",
            findings=result.raw_findings or "",
            verdict=report_json,   # store report JSON in verdict column
            lives_used=getattr(result, "lives_used", 1),
            prompt_tokens=getattr(result, "prompt_tokens", 0),
            completion_tokens=getattr(result, "completion_tokens", 0),
        )
        _ADHOC_RESEARCH_JOBS[job_id] = {
            "status": "completed",
            "report": report,
            "findings": result.raw_findings or "",
        }

        # Send inbox notification
        answer_snippet = (report.get("answer") or "")[:200]
        create_inbox_message(
            subject=f"Investigation: {question[:80]}",
            source_type="investigation",
            task_id=task_id,
            task_title=task.title,
            outcome="completed",
            data_json=json.dumps({
                "job_id": job_id,
                "question": question,
                "answer": answer_snippet,
                "key_findings": report.get("key_findings", [])[:3],
                "recommendation": (report.get("recommendation") or "")[:200],
            }),
        )

    except Exception as exc:
        logger.exception("[toolbar] Ad-hoc investigation for task '%s' failed.", task_id)
        err = str(exc)
        update_research_job(job_id, status="failed")
        _ADHOC_RESEARCH_JOBS[job_id] = {"status": "failed", "error": err}


@app.post("/api/agent/investigate/{task_id}", response_model=dict)
def start_adhoc_investigation(task_id: str, body: dict):
    """Start an ad-hoc investigation agent on a task from the card toolbar."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM assigned")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned")

    question = (body.get("question") or "").strip()
    if not question:
        raise HTTPException(status_code=422, detail="'question' is required")

    job = create_research_job(
        task_id=task_id,
        question=question,
        llm_id=task.llm_id,
        budget_id=task.budget_id,
    )
    if not job:
        raise HTTPException(status_code=500, detail="Failed to create investigation job")

    _start_bg(_run_adhoc_investigation, task_id, question, job.id)
    return {"job_id": job.id, "status": "queued"}


@app.get("/api/agent/investigate/{task_id}/status", response_model=dict)
def get_adhoc_investigation_status(task_id: str, job_id: int):
    """Poll the status of a toolbar-triggered investigation job."""
    in_mem = _ADHOC_RESEARCH_JOBS.get(job_id)
    if in_mem:
        return in_mem

    job = get_research_job(job_id)
    if not job or job.task_id != task_id:
        raise HTTPException(status_code=404, detail="Investigation job not found")

    # Attempt to parse verdict column as JSON report
    report = {}
    if job.verdict:
        try:
            parsed = json.loads(job.verdict)
            if isinstance(parsed, dict) and "answer" in parsed:
                report = parsed
        except (json.JSONDecodeError, ValueError):
            pass

    return {
        "status": job.status,
        "report": report,
        "findings": job.findings or "",
    }


@app.post("/api/agent/subdivide/{task_id}", status_code=202)
def adhoc_subdivide(task_id: str):
    """Trigger the subdivision agent on any task (toolbar shortcut, no is_big_idea guard)."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM assigned")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned")
    _start_bg(_run_regenerate_subdivision, task_id)
    return {"status": "queued"}


# ============================================
# Manual Session API
# ============================================

from app.agent.manual_session import ManualSession, _ACTIVE_MANUAL_SESSIONS

_MANUAL_SESSION_DENIED_TOOLS = {"spawn_research_agent"}


def _build_manual_session_context(task) -> str:
    """Build the initial context string shown in a manual session."""
    lines = [
        f"Task: {task.title}",
        f"ID: {task.id}",
        f"Status: {task.type}",
        f"Project: {task.project or '(none)'}",
    ]
    if task.description:
        lines.append(f"\nDescription:\n{task.description}")
    if task.tags:
        lines.append(f"\nTags: {', '.join(task.tags)}")
    lines.append(
        "\n[Manual Session] You are the reasoning layer. Pick tools, see results, iterate."
        " No LLM is called. End with a signal when done."
    )
    return "\n".join(lines)


@app.post("/api/manual-session/{task_id}/start", response_model=dict)
async def start_manual_session(task_id: str):
    """Start a new manual session for a task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    context = _build_manual_session_context(task)
    session = ManualSession.create(task_id, task.title, context)
    _ACTIVE_MANUAL_SESSIONS[session.session_id] = session

    from app.agent.tools import TOOL_SCHEMAS
    available = [s for s in TOOL_SCHEMAS if s["function"]["name"] not in _MANUAL_SESSION_DENIED_TOOLS]

    return {
        "session_id": session.session_id,
        "task_title": task.title,
        "messages": session.messages,
        "available_tools": available,
        "status": session.status,
    }


@app.get("/api/manual-session/{session_id}", response_model=dict)
def get_manual_session(session_id: str):
    """Get current state of a manual session."""
    session = _ACTIVE_MANUAL_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired (server may have restarted)")

    from app.agent.tools import TOOL_SCHEMAS
    available = [s for s in TOOL_SCHEMAS if s["function"]["name"] not in _MANUAL_SESSION_DENIED_TOOLS]

    return {
        "session_id": session_id,
        "task_title": session.task_title,
        "messages": session.messages,
        "status": session.status,
        "available_tools": available,
    }


@app.post("/api/manual-session/{session_id}/tool", response_model=dict)
async def manual_session_tool(session_id: str, body: dict):
    """Dispatch a tool call in a manual session."""
    session = _ACTIVE_MANUAL_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired (server may have restarted)")
    if session.status == "ended":
        raise HTTPException(status_code=409, detail="Session has already ended")

    tool_name = (body.get("tool_name") or "").strip()
    arguments = body.get("arguments") or {}

    if not tool_name:
        raise HTTPException(status_code=422, detail="'tool_name' is required")
    if tool_name in _MANUAL_SESSION_DENIED_TOOLS:
        raise HTTPException(status_code=422, detail=f"Tool '{tool_name}' is not available in manual sessions")

    task = get_task(session.task_id)
    if task:
        _setup_thread_context(task)

    from app.agent.tools import async_dispatch_tool
    result = await async_dispatch_tool(
        tool_name,
        arguments,
        task_id=session.task_id,
        llm_id=task.llm_id if task else None,
        budget_id=task.budget_id if task else None,
        llm_base_url=_resolve_llm_endpoint(task)[0] if task else None,
        llm_model=_resolve_llm_endpoint(task)[1] if task else None,
    )

    session.record_tool_call(tool_name, arguments, result)
    return {"messages": session.messages, "result": result}


@app.post("/api/manual-session/{session_id}/message", response_model=dict)
def manual_session_add_message(session_id: str, body: dict):
    """Add a user or assistant message to the manual session log."""
    session = _ACTIVE_MANUAL_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    if session.status == "ended":
        raise HTTPException(status_code=409, detail="Session has already ended")
    role = body.get("role", "user")
    content = (body.get("content") or "").strip()
    if content:
        session.add_message(role, content)
    return {"messages": session.messages}


@app.post("/api/manual-session/{session_id}/end", response_model=dict)
def end_manual_session(session_id: str, body: dict):
    """End a manual session with a terminal signal."""
    session = _ACTIVE_MANUAL_SESSIONS.get(session_id)
    if not session:
        raise HTTPException(status_code=404, detail="Session not found or expired")
    session.end(
        signal=body.get("signal", "MANUAL_END"),
        summary=body.get("summary", ""),
    )
    return {"status": "ended", "signal": session.signal}


@app.get("/api/tasks/{task_id}/benchmarks", response_model=List[dict])
def get_task_benchmarks(task_id: str):
    """Get all optimization benchmarks for a task (as parent), ordered by created_at."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    records = get_optimization_benchmarks(task_id)
    return [
        {
            "id": r.id,
            "task_id": r.task_id,
            "parent_task_id": r.parent_task_id,
            "benchmark_type": r.benchmark_type,
            "metrics": r.metrics,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in records
    ]


@app.get("/api/research-jobs/{job_id}", response_model=dict)
def get_single_research_job(job_id: int):
    """Get a single research job by ID."""
    job = get_research_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Research job not found")
    return _research_job_to_dict(job)


# ============================================
# Descendants API
# ============================================

@app.get("/api/tasks/{task_id}/descendants", response_model=List[dict])
def get_task_descendants(task_id: str):
    """Return a flat list of all descendant task IDs with column, position, and depth info."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return get_descendant_tree(task_id)


# ============================================
# Batch Reorder API
# ============================================

class BatchReorderRequest(BaseModel):
    moves: list

@app.post("/api/tasks/batch-reorder")
def batch_reorder(request: BatchReorderRequest):
    """Process multiple task reorders in a single transaction."""
    if not request.moves:
        return {"success": True, "message": "No moves to process"}
    success = batch_reorder_tasks(request.moves)
    if not success:
        raise HTTPException(status_code=500, detail="Batch reorder failed")
    return {"success": True, "message": f"Reordered {len(request.moves)} tasks"}


# ============================================
# Completion Rollup — check if parent can be completed
# ============================================

def _check_completion_rollup(task_id: str):
    """After a task completes, check if its parent's children are all done."""
    task = get_task(task_id)
    if not task or not task.parent_task_id:
        return

    parent = get_task(task.parent_task_id)
    if not parent:
        return

    children = get_active_child_tasks(parent.id)
    if not children:
        return

    all_done = all(
        (c.type or "").lower() in PIPELINE_DONE_STATUSES
        for c in children
    )

    if all_done:
        update_task(parent.id, type="completed")
        logger.info("[rollup] All children of '%s' completed. Parent marked completed.", parent.id)
        # Recurse upward
        _check_completion_rollup(parent.id)


# ============================================
# Helper Functions
# ============================================

def _check_has_cached_plan(task) -> bool:
    """Return True if a reusable planning result exists for this task's current spec."""
    if getattr(task, 'type', None) not in ('planning', 'idea'):
        return False
    try:
        from hashlib import sha256
        from app.database import get_reusable_planning_result
        content_hash = sha256(f"{task.title}||{task.description or ''}".encode()).hexdigest()
        return get_reusable_planning_result(task.id, content_hash) is not None
    except Exception:
        return False


def task_to_dict(task):
    """Convert SQLAlchemy Task model to dictionary"""
    llm_obj = getattr(task, 'llm_ref', None)
    budget_obj = getattr(task, 'budget_ref', None)

    # PIPs — derived status per current pipeline stage
    pips_data = []
    try:
        from app.database import get_pips_for_task, pip_status_at_stage, get_latest_pip_verification
        for pip in get_pips_for_task(task.id):
            reqs = json.loads(pip.requirements) if pip.requirements else []
            latest_v = get_latest_pip_verification(pip.id, task.type)
            created_str = (
                pip.created_at.isoformat() if hasattr(pip.created_at, "isoformat")
                else str(pip.created_at)
            )
            pips_data.append({
                "id": pip.id,
                "origin_stage": pip.origin_stage,
                "requirements": reqs,
                "created_at": created_str,
                "status": pip_status_at_stage(pip, task.type),
                "last_summary": latest_v.summary if latest_v else None,
                "last_checked": latest_v.created_at if latest_v else None,
            })
    except Exception:
        pass

    return {
        "id": task.id,
        "title": task.title,
        "type": task.type,
        "description": task.description,
        "owner": task.owner,
        "tags": task.tags,
        "content": task.content,
        "llm_id": task.llm_id,
        "llm_label": llm_obj.label if llm_obj else None,
        "budget_id": task.budget_id,
        "budget_name": budget_obj.name if budget_obj else None,
        "history": task.history,
        "prerequisites": getattr(task, "prerequisites", None) or [],
        "position": task.position,
        "project": getattr(task, "project", None) or "TheMaestro",
        "parent_task_id": getattr(task, "parent_task_id", None),
        "subdivision_generation": getattr(task, "subdivision_generation", 0) or 0,
        "is_big_idea": bool(getattr(task, "is_big_idea", False)),
        "interface_contracts": json.loads(task.interface_contracts) if getattr(task, "interface_contracts", None) else None,
        "review_notes": getattr(task, "review_notes", None),
        "demotion_count": getattr(task, "demotion_count", 0) or 0,
        "demotion_history": getattr(task, "demotion_history", None),
        "map_x": getattr(task, "map_x", None),
        "map_y": getattr(task, "map_y", None),
        "is_active": bool(getattr(task, "is_active", True)),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "pips": pips_data,
        # Intake exhaustion state (only relevant for IDEA cards)
        "intake_exhausted": bool(getattr(task, "intake_exhausted_at", None)),
        "intake_rejection_count": (
            sum(
                1 for r in get_transition_results(task.id, transition="idea_to_planning")
                if r.outcome in ("rejected", "needs_research")
            )
            if task.type == "idea"
            else 0
        ),
        "cache_mode": getattr(task, "cache_mode", "normal") or "normal",
        "has_cached_plan": _check_has_cached_plan(task),
        "acceptance_criteria": json.loads(task.acceptance_criteria) if getattr(task, "acceptance_criteria", None) else None,
        "clarification_status": getattr(task, "clarification_status", "none") or "none",
        "is_starred": bool(getattr(task, "is_starred", False)),
        "last_progress_at": task.last_progress_at.isoformat() if getattr(task, "last_progress_at", None) else None,
    }


def llm_to_dict(llm):
    """Convert SQLAlchemy LLM model to dictionary"""
    return {
        "id": llm.id,
        "address": llm.address,
        "port": llm.port,
        "model": llm.model,
        "label": llm.label,
        "settings": llm.settings,
        "parallel_sessions": llm.parallel_sessions,
        "max_context": llm.max_context,
        "notes": llm.notes,
        "cost_per_million_prompt_tokens": llm.cost_per_million_prompt_tokens,
        "cost_per_million_completion_tokens": llm.cost_per_million_completion_tokens,
        "compute_node_id": getattr(llm, "compute_node_id", None),
    }


def compute_node_to_dict(node):
    """Convert SQLAlchemy ComputeNode model to dictionary."""
    return {
        "id": node.id,
        "name": node.name,
        "description": node.description,
        "max_parallel_sessions": node.max_parallel_sessions,
        "max_loaded_models": node.max_loaded_models,
    }


def budget_to_dict(budget):
    """Convert SQLAlchemy Budget model to dictionary"""
    return {
        "id": budget.id,
        "name": budget.name,
        "dollar_amount": budget.dollar_amount,
        "settings": budget.settings,
    }


# ============================================
# Project prewarm helpers
# ============================================

def _pick_prewarm_resources(
    project_llm_id: "int | None" = None,
    project_budget_id: "int | None" = None,
) -> "tuple[int | None, int | None]":
    """Return (llm_id, budget_id) for background file-summary prewarm jobs.

    Uses ``project_llm_id`` / ``project_budget_id`` when the project has them
    configured.  Falls back to the first available LLM / infinite budget only
    when the project has none set.
    Returns (None, None) if no LLM or budget is available.
    """
    from app.database import get_all_llms, get_all_budgets

    # Resolve budget: use project's own if set; otherwise prefer infinite.
    if project_budget_id is not None:
        budget_id = project_budget_id
    else:
        budgets = get_all_budgets()
        if not budgets:
            return None, None
        infinite = [b for b in budgets if b.dollar_amount == -1.0]
        budget_id = (infinite or budgets)[0].id

    # Resolve LLM: use project's own if set; otherwise first available.
    if project_llm_id is not None:
        return project_llm_id, budget_id

    llms = get_all_llms()
    if not llms:
        return None, None
    return llms[0].id, budget_id


def _trigger_project_prewarm(
    project_name: str,
    project_path: str,
    project_llm_id: "int | None" = None,
    project_budget_id: "int | None" = None,
) -> None:
    """Trigger the tiered project survey process in a background thread.
    
    This replaces the old flat file-summary prewarm with the new
    hierarchical SurveyOrchestrator approach.
    """
    llm_id, budget_id = _pick_prewarm_resources(project_llm_id, project_budget_id)
    if llm_id is None or budget_id is None:
        logger.debug("survey/prewarm skipped for '%s' — no LLM/budget configured", project_path)
        return

    def _run():
        from app.agent.survey_orchestrator import SurveyOrchestrator
        try:
            orchestrator = SurveyOrchestrator()
            result = orchestrator.ensure_project_surveyed(
                project_name, project_path, llm_id, budget_id
            )
            logger.info("[Survey] Tiered survey triggered for '%s': %s", project_name, result)
        except Exception:
            logger.exception("[Survey] Tiered survey failed to initiate for '%s'", project_name)

    threading.Thread(target=_run, daemon=True, name=f"survey-{project_name}").start()


# ============================================
# Project API Endpoints
# ============================================

def _project_to_dict(p) -> dict:
    return {
        "name": p.name,
        "path": p.path or "",
        "description": p.description or "",
        "llm_id": p.llm_id,
        "budget_id": p.budget_id,
    }


@app.get("/api/projects", response_model=List[dict])
def list_projects():
    """List all known projects with their filesystem paths."""
    return [_project_to_dict(p) for p in get_all_projects()]


@app.get("/api/system/browse-folder")
def browse_folder():
    """Open a native folder-picker dialog and return the chosen path."""
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.wm_attributes("-topmost", 1)
        chosen = filedialog.askdirectory(parent=root)
        root.destroy()
        return {"path": chosen or ""}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Folder picker unavailable: {exc}")


def _validate_and_prepare_path(path: str | None, create_if_missing: bool) -> str | None:
    """Return the path unchanged, create it on request, or raise 422 if it doesn't exist."""
    if not path:
        return None
    if os.path.exists(path):
        return path
    if create_if_missing:
        os.makedirs(path, exist_ok=True)
        return path
    raise HTTPException(
        status_code=422,
        detail={"error": "path_not_found", "path": path},
    )


@app.post("/api/projects", response_model=dict, status_code=201)
def create_project(data: dict):
    """Create or update a project record."""
    name = (data.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Project name is required.")
    raw_path = (data.get("path") or "").strip() or None
    create_if_missing = bool(data.get("create_if_missing", False))
    path = _validate_and_prepare_path(raw_path, create_if_missing)
    description = (data.get("description") or "").strip() or None
    llm_id = data.get("llm_id") or None
    if llm_id is not None:
        llm_id = int(llm_id)
    budget_id = data.get("budget_id") or None
    if budget_id is not None:
        budget_id = int(budget_id)
    project = upsert_project(name, path=path, description=description, llm_id=llm_id, budget_id=budget_id)
    if not project:
        raise HTTPException(status_code=500, detail="Failed to create project.")
    if project.path:
        _trigger_project_prewarm(project.name, project.path, project_llm_id=project.llm_id, project_budget_id=project.budget_id)
    return _project_to_dict(project)


@app.put("/api/projects/{project_name}", response_model=dict)
def update_project(project_name: str, data: dict):
    """Update a project's name, path, description, default LLM, and/or default budget."""
    new_name = (data.get("name") or "").strip() or None

    # Rename first if a different name was requested
    effective_name = project_name
    if new_name and new_name != project_name:
        try:
            renamed = rename_project(project_name, new_name)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        if not renamed:
            raise HTTPException(status_code=404, detail="Project not found.")
        effective_name = new_name

    raw_path = data.get("path")           # None means "don't change"
    create_if_missing = bool(data.get("create_if_missing", False))
    path = _validate_and_prepare_path(raw_path, create_if_missing) if raw_path is not None else raw_path
    description = data.get("description")
    llm_id = data.get("llm_id", ...)      # Ellipsis = don't change; None = clear
    if llm_id is not ... and llm_id is not None:
        llm_id = int(llm_id)
    budget_id = data.get("budget_id", ...)  # Ellipsis = don't change; None = clear
    if budget_id is not ... and budget_id is not None:
        budget_id = int(budget_id)
    project = upsert_project(effective_name, path=path, description=description, llm_id=llm_id, budget_id=budget_id)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found or update failed.")
    if project.path:
        _trigger_project_prewarm(project.name, project.path, project_llm_id=project.llm_id, project_budget_id=project.budget_id)
    return _project_to_dict(project)


@app.delete("/api/projects/{project_name}")
def remove_project(project_name: str):
    """Delete a project record. Tasks that reference it are unaffected."""
    if not delete_project(project_name):
        raise HTTPException(status_code=404, detail="Project not found.")
    return {"deleted": project_name}


_ARCH_CATEGORIES = [
    "Platform", "Design", "Testing", "Security", "Performance",
    "API", "Tooling", "Data", "UX", "Accessibility",
    "Compliance", "Deployment", "Observability", "General",
]


@app.post("/api/projects/{project_name}/populate-arch")
def populate_arch(project_name: str):
    """Queue arch_gen_jobs for every architecture category not yet present in the project.

    Existing cards are never modified.  Returns the count of jobs queued and the
    list of categories that will be generated.
    """
    from app.database import get_project, get_tasks_by_project, create_arch_gen_job
    from app.database import SessionLocal as _SL
    from app.database.models import ArchGenJob as _ArchGenJob

    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Collect categories that already have at least one arch card
    tasks = get_tasks_by_project(project_name)
    existing_categories: set[str] = set()
    for t in tasks:
        if t.type != 'architecture':
            continue
        try:
            content = t.content if isinstance(t.content, dict) else (
                json.loads(t.content) if t.content else {}
            )
            cat = content.get('category')
            if cat:
                existing_categories.add(cat)
        except Exception:
            pass

    missing = [c for c in _ARCH_CATEGORIES if c not in existing_categories]
    if not missing:
        return {"queued": 0, "categories": [], "message": "All 14 categories already have cards"}

    # Prewarm gate: ensure file summaries exist so arch_gen_agent has context
    from app.database import get_file_summaries_for_project_root
    summaries = get_file_summaries_for_project_root(project.path or "")
    if not summaries:
        raise HTTPException(
            status_code=409,
            detail=f"No file summaries found for project '{project_name}'. "
                   "Architecture generation requires file summaries for context. "
                   "Please set a project path and run a prewarm/file-summary pass first."
        )

    # Skip categories that already have an active (pending/running) job
    _db = _SL()
    try:
        active_cats: set[str] = set(
            row.category for row in
            _db.query(_ArchGenJob.category)
               .filter(
                   _ArchGenJob.project_id == project.id,
                   _ArchGenJob.status.in_(('pending', 'running')),
               )
               .all()
        )
    finally:
        _db.close()

    to_queue = [c for c in missing if c not in active_cats]
    already_queued = [c for c in missing if c in active_cats]
    if already_queued:
        logger.info(
            "populate-arch: skipping %d categories already queued for '%s': %s",
            len(already_queued), project_name, already_queued,
        )

    if not to_queue:
        return {
            "queued": 0,
            "categories": [],
            "message": f"All missing categories already have active jobs ({len(already_queued)} pending/running)",
        }

    llm_id, budget_id = _pick_prewarm_resources(project.llm_id, project.budget_id)
    if llm_id is None or budget_id is None:
        raise HTTPException(
            status_code=503,
            detail="No LLM endpoint or budget available. Configure a default LLM and budget on the project.",
        )

    for category in to_queue:
        create_arch_gen_job(project_name, category, llm_id=llm_id, budget_id=budget_id)

    logger.info(
        "populate-arch: queued %d arch_gen_jobs for project '%s': %s",
        len(to_queue), project_name, to_queue,
    )
    return {"queued": len(to_queue), "categories": to_queue}


@app.get("/api/projects/{project_name}/arch-gen-jobs")
def get_project_arch_gen_jobs(project_name: str):
    """Return active jobs and a summary of all arch gen jobs for a project."""
    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    from app.database import SessionLocal as _SL
    from app.database.models import ArchGenJob as _AGJ
    from sqlalchemy import func
    with _SL() as db:
        # Get pending/running for the ghost cards
        active_jobs = (
            db.query(_AGJ)
            .filter(_AGJ.project_id == project.id, _AGJ.status.in_(["pending", "running"]))
            .order_by(_AGJ.created_at)
            .all()
        )

        # Get counts for the summary
        counts = (
            db.query(_AGJ.status, func.count(_AGJ.id))
            .filter(_AGJ.project_id == project.id)
            .group_by(_AGJ.status)
            .all()
        )
        summary = {s: c for s, c in counts}
        for status in ["pending", "running", "completed", "failed"]:
            if status not in summary:
                summary[status] = 0

        return {
            "jobs": [
                {
                    "id": j.id,
                    "category": j.category,
                    "status": j.status,
                    "created_at": j.created_at.isoformat() if j.created_at else None,
                    "retry_count": j.retry_count,
                }
                for j in active_jobs
            ],
            "summary": summary
        }


@app.post("/api/projects/{name}/survey")
async def trigger_project_survey(name: str):
    """Enqueue a full survey pass for the project."""
    project = db.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    from app.agent.survey_orchestrator import SurveyOrchestrator
    orchestrator = SurveyOrchestrator()
    result = orchestrator.ensure_project_surveyed(
        name, project.path, project.llm_id, project.budget_id
    )
    return result


@app.get("/api/projects/{name}/scope-summaries")
async def list_project_scopes(name: str, scope_type: str = None):
    """List all scopes for a project."""
    return db.list_scope_summaries(name, scope_type)


@app.get("/api/projects/{name}/scope-summaries/{scope_type}/{scope_key:path}")
async def get_scope_detail(name: str, scope_type: str, scope_key: str):
    """Get detail for a specific scope. Note: scope_key may contain slashes."""
    scope = db.get_scope_summary(name, scope_type, scope_key)
    if not scope:
        raise HTTPException(status_code=404, detail="Scope not found")
    return scope


@app.post("/api/projects/{name}/scope-summaries/{scope_type}/{scope_key:path}/re-survey")
async def enqueue_resurvey(name: str, scope_type: str, scope_key: str):
    """Enqueue a re-survey for a specific scope."""
    project = db.get_project(name)
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db.enqueue_scope_survey_job(
        name, scope_type, scope_key, action="generate",
        llm_id=project.llm_id, budget_id=project.budget_id
    )
    return {"status": "enqueued"}


@app.get("/summary-browser", response_class=HTMLResponse)
async def get_summary_browser():
    """Standalone summary browser UI."""
    try:
        with open("app/web/summary-browser.html", "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return f"Error loading summary-browser.html: {e}"


# ============================================
# Dreamer API Endpoints
# ============================================

def _dreamer_run_to_dict(run) -> dict:
    import json as _json
    actions = []
    new_ids = []
    if run.actions_taken:
        try:
            actions = _json.loads(run.actions_taken)
        except Exception:
            pass
    if run.new_task_ids:
        try:
            new_ids = _json.loads(run.new_task_ids)
        except Exception:
            pass
    return {
        "id":           run.id,
        "project_name": run.project_name,
        "started_at":   run.started_at,
        "finished_at":  run.finished_at,
        "status":       run.status,
        "stall_reason": run.stall_reason,
        "actions_taken": actions,
        "new_task_ids": new_ids,
        "llm_id":       run.llm_id,
        "budget_id":    run.budget_id,
    }


@app.get("/api/projects/{project_name}/dreamer-runs", response_model=List[dict])
def list_dreamer_runs(project_name: str, limit: int = 20):
    """Return recent Dreamer run history for a project (newest first)."""
    from app.database import get_dreamer_runs as _get_runs
    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    runs = _get_runs(project_name, limit=max(1, min(limit, 100)))
    return [_dreamer_run_to_dict(r) for r in runs]


@app.get("/api/dreamer-runs/{run_id}", response_model=dict)
def get_single_dreamer_run(run_id: int):
    """Return a single Dreamer run by ID."""
    from app.database import get_dreamer_run as _get_run
    run = _get_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="Dreamer run not found")
    return _dreamer_run_to_dict(run)


@app.post("/api/projects/{project_name}/dreamer/trigger", response_model=dict)
def trigger_dreamer(project_name: str):
    """Manually trigger a Dreamer run for a project (bypasses stall check)."""
    from app.agent.scheduler import _active_dreamer_projects, _active_dreamer_lock, _start_dreamer_thread
    from app.database import get_llm as _get_llm

    project = get_project(project_name)
    if not project:
        raise HTTPException(status_code=404, detail=f"Project '{project_name}' not found")
    if not project.llm_id or not project.budget_id:
        raise HTTPException(
            status_code=422,
            detail="Project must have a default LLM and Budget configured to run Dreamer.",
        )

    with _active_dreamer_lock:
        if project_name in _active_dreamer_projects:
            return {"status": "already_running", "project": project_name}

    llm = _get_llm(project.llm_id)
    if not llm:
        raise HTTPException(status_code=422, detail="Project LLM record not found.")

    llm_base_url = f"http://{llm.address}:{llm.port}/v1"
    _start_dreamer_thread(
        project_name=project_name,
        project_path=project.path,
        llm_id=project.llm_id,
        budget_id=project.budget_id,
        llm_base_url=llm_base_url,
        llm_model=llm.model,
    )
    return {"status": "started", "project": project_name}


# ============================================
# LLM API Endpoints (global, not project-scoped)
# ============================================

@app.get("/api/llms", response_model=List[dict])
def list_llms():
    return [llm_to_dict(l) for l in get_all_llms()]


@app.get("/api/llms/{llm_id}", response_model=dict)
def read_llm(llm_id: int):
    llm = get_llm(llm_id)
    if not llm:
        raise HTTPException(status_code=404, detail="LLM not found")
    return llm_to_dict(llm)


@app.post("/api/llms", response_model=dict)
def create_new_llm(data: dict):
    from app.agent.config import MIN_PARALLEL_SESSIONS, MAX_PARALLEL_SESSIONS, MIN_CONTEXT_SIZE, MAX_CONTEXT_SIZE
    if not data.get('address') or not data.get('model'):
        raise HTTPException(status_code=400, detail="address and model are required")
    ps = data.get('parallel_sessions', 1)
    if not isinstance(ps, int) or ps < MIN_PARALLEL_SESSIONS or ps > MAX_PARALLEL_SESSIONS:
        raise HTTPException(status_code=400, detail=f"parallel_sessions must be {MIN_PARALLEL_SESSIONS}-{MAX_PARALLEL_SESSIONS}")
    mc = data.get('max_context', 4096)
    if not isinstance(mc, int) or mc < MIN_CONTEXT_SIZE or mc > MAX_CONTEXT_SIZE:
        raise HTTPException(status_code=400, detail=f"max_context must be {MIN_CONTEXT_SIZE}-{MAX_CONTEXT_SIZE}")
    llm = create_llm(
        address=data['address'],
        port=data.get('port', 8008),
        model=data['model'],
        settings=data.get('settings'),
        parallel_sessions=ps,
        max_context=mc,
        notes=data.get('notes', ''),
        cost_per_million_prompt_tokens=float(data.get('cost_per_million_prompt_tokens', 0.0)),
        cost_per_million_completion_tokens=float(data.get('cost_per_million_completion_tokens', 0.0)),
    )
    if not llm:
        raise HTTPException(status_code=409, detail="LLM with this address/port/model already exists")
    # Optionally assign a compute node
    if 'compute_node_id' in data:
        node_id = data['compute_node_id'] or None
        update_llm(llm.id, compute_node_id=node_id)
        llm = get_llm(llm.id)
    return llm_to_dict(llm)


def sync_update_llm_with_cache(llm_id: int, **kwargs):
    """Update LLM record in DB and propagate cache changes.

    - max_context change  → updates context-window cache in-place (no semaphore impact).
    - parallel_sessions change → clears capacity cache AND all endpoint semaphores so
      the next incoming request rebuilds the semaphore with the new slot count.
      In-flight callers hold a reference to the old semaphore object and release it
      normally when done; new callers get the fresh semaphore.  This is safe because
      threading.Semaphore cannot be resized in-place, but replacing it in the registry
      is atomic under _ep_lock.
    """
    from app.agent.llm_client import update_llm_context_cache, invalidate_llm_cache
    result = update_llm(llm_id, **kwargs)
    if result is None:
        return result
    if 'parallel_sessions' in kwargs:
        # Capacity changed: blow away the stale semaphore so it rebuilds with new count.
        invalidate_llm_cache(llm_id)
        # Re-seed context cache if max_context was also updated in the same request.
        if 'max_context' in kwargs:
            update_llm_context_cache(llm_id, kwargs['max_context'])
    elif 'max_context' in kwargs:
        update_llm_context_cache(llm_id, kwargs['max_context'])
    return result


def sync_delete_llm_with_cache(llm_id: int):
    """Delete LLM record from DB and clear all caches for this LLM ID."""
    from app.agent.llm_client import invalidate_llm_cache
    result = delete_llm(llm_id)
    invalidate_llm_cache(llm_id)
    return result


@app.put("/api/llms/{llm_id}", response_model=dict)
def update_existing_llm(llm_id: int, data: dict):
    allowed = ['address', 'port', 'model', 'settings', 'parallel_sessions', 'max_context', 'notes',
               'cost_per_million_prompt_tokens', 'cost_per_million_completion_tokens',
               'compute_node_id']
    updates = {k: v for k, v in data.items() if k in allowed}
    # Normalize compute_node_id: empty string or 0 → None
    if 'compute_node_id' in updates:
        raw = updates['compute_node_id']
        updates['compute_node_id'] = int(raw) if raw else None
    llm = sync_update_llm_with_cache(llm_id, **updates)

    if not llm:
        raise HTTPException(status_code=404, detail="LLM not found")
    return llm_to_dict(llm)


@app.delete("/api/llms/{llm_id}", response_model=bool)
def delete_llm_endpoint(llm_id: int):
    if not sync_delete_llm_with_cache(llm_id):
        raise HTTPException(status_code=404, detail="LLM not found")
    return True


# ============================================
# Budget API Endpoints (global, not project-scoped)
# ============================================

@app.get("/api/budgets", response_model=List[dict])
def list_budgets():
    return [budget_to_dict(b) for b in get_all_budgets()]


@app.get("/api/budgets/{budget_id}", response_model=dict)
def read_budget(budget_id: int):
    budget = get_budget(budget_id)
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget_to_dict(budget)


@app.post("/api/budgets", response_model=dict)
def create_new_budget(data: dict):
    if not data.get('name'):
        raise HTTPException(status_code=400, detail="name is required")
    budget = create_budget(
        name=data['name'],
        dollar_amount=float(data.get('dollar_amount', -1)),
        settings=data.get('settings'),
    )
    if not budget:
        raise HTTPException(status_code=409, detail="Budget with this name already exists")
    return budget_to_dict(budget)


@app.put("/api/budgets/{budget_id}", response_model=dict)
def update_existing_budget(budget_id: int, data: dict):
    allowed = ['name', 'dollar_amount', 'settings']
    updates = {k: v for k, v in data.items() if k in allowed}
    budget = update_budget(budget_id, **updates)
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    return budget_to_dict(budget)


@app.delete("/api/budgets/{budget_id}", response_model=bool)
def delete_budget_endpoint(budget_id: int):
    if not delete_budget(budget_id):
        raise HTTPException(status_code=404, detail="Budget not found")
    return True


@app.get("/api/budgets/{budget_id}/remaining", response_model=dict)
def get_budget_remaining(budget_id: int):
    from app.database import get_budget_spent_microcents, get_budget_remaining_microcents
    budget = get_budget(budget_id)
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    spent = get_budget_spent_microcents(budget_id)
    remaining = get_budget_remaining_microcents(budget_id)
    limit_uc = None if budget.dollar_amount == -1 else int(budget.dollar_amount * 100 * 1_000_000)
    return {
        "budget_id": budget_id,
        "dollar_amount": budget.dollar_amount,
        "infinite": budget.dollar_amount == -1,
        "limit_microcents": limit_uc,
        "spent_microcents": spent,
        "remaining_microcents": remaining,
        "spent_dollars": round(spent / 100_000_000, 6) if spent else 0.0,
        "remaining_dollars": round(remaining / 100_000_000, 6) if remaining is not None else None,
    }


# ============================================
# Compute Node API Endpoints (global, not project-scoped)
# ============================================

@app.get("/api/compute-nodes", response_model=List[dict])
def list_compute_nodes():
    return [compute_node_to_dict(n) for n in get_all_compute_nodes()]


@app.get("/api/compute-nodes/{node_id}", response_model=dict)
def read_compute_node(node_id: int):
    node = get_compute_node(node_id)
    if not node:
        raise HTTPException(status_code=404, detail="Compute node not found")
    return compute_node_to_dict(node)


@app.post("/api/compute-nodes", response_model=dict)
def create_new_compute_node(data: dict):
    if not data.get('name'):
        raise HTTPException(status_code=400, detail="name is required")
    mps = data.get('max_parallel_sessions', 1)
    if not isinstance(mps, int) or mps < 1:
        raise HTTPException(status_code=400, detail="max_parallel_sessions must be >= 1")
    mlm = data.get('max_loaded_models', 1)
    if not isinstance(mlm, int) or mlm < 1:
        raise HTTPException(status_code=400, detail="max_loaded_models must be >= 1")
    node = create_compute_node(
        name=data['name'],
        description=data.get('description'),
        max_parallel_sessions=mps,
        max_loaded_models=mlm,
    )
    if not node:
        raise HTTPException(status_code=409, detail="Compute node with this name already exists")
    return compute_node_to_dict(node)


@app.put("/api/compute-nodes/{node_id}", response_model=dict)
def update_existing_compute_node(node_id: int, data: dict):
    allowed = ['name', 'description', 'max_parallel_sessions', 'max_loaded_models']
    updates = {k: v for k, v in data.items() if k in allowed}
    node = update_compute_node(node_id, **updates)
    if not node:
        raise HTTPException(status_code=404, detail="Compute node not found")
    return compute_node_to_dict(node)


@app.delete("/api/compute-nodes/{node_id}", response_model=bool)
def delete_compute_node_endpoint(node_id: int):
    if not delete_compute_node(node_id):
        raise HTTPException(status_code=404, detail="Compute node not found")
    return True


# ============================================
# Budget Entry API Endpoints (usage tracking)
# ============================================

def budget_entry_to_dict(entry):
    first_tool = None
    first_tool_args = None
    if entry.response_data:
        try:
            import json as _json
            # response_data could be string or dict depending on how it was set
            rd = _json.loads(entry.response_data) if isinstance(entry.response_data, str) else entry.response_data
            tcs = rd.get("choices", [{}])[0].get("message", {}).get("tool_calls", [])
            if tcs:
                first_tool = tcs[0].get("function", {}).get("name")
                first_tool_args = tcs[0].get("function", {}).get("arguments")
        except:
            pass
    return {
        "id": entry.id,
        "llm_id": entry.llm_id,
        "budget_id": entry.budget_id,
        "task_id": entry.task_id,
        "prompt_cost": entry.prompt_cost,
        "generation_cost": entry.generation_cost,
        "tool_calls": entry.tool_calls,
        "first_tool": first_tool,
        "first_tool_args": first_tool_args,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "session_id": entry.session_id,
        "agent_name": entry.agent_name,
    }


@app.get("/api/budget-entries", response_model=List[dict])
def list_budget_entries(budget_id: int = None, llm_id: int = None, task_id: str = None,
                        limit: int = 100, offset: int = 0):
    """List budget entries with optional filters.

    Pass ``task_id=__file_summaries__`` to retrieve all entries that have no task
    association (project-level file summary jobs).
    """
    if task_id == "__file_summaries__":
        # Special sentinel: return entries where task_id IS NULL
        from app.database import SessionLocal as _SL, BudgetEntry as _BE
        db = _SL()
        try:
            q = db.query(_BE).filter(_BE.task_id.is_(None))
            if budget_id is not None:
                q = q.filter(_BE.budget_id == budget_id)
            if llm_id is not None:
                q = q.filter(_BE.llm_id == llm_id)
            entries = q.order_by(_BE.created_at.desc()).offset(offset).limit(limit).all()
        finally:
            db.close()
    else:
        entries = get_budget_entries(budget_id=budget_id, llm_id=llm_id, task_id=task_id,
                                    limit=limit, offset=offset)
    return [budget_entry_to_dict(e) for e in entries]


@app.get("/api/budget-entries/{entry_id}/full", response_model=dict)
def read_budget_entry_full(entry_id: int):
    """Get a single budget entry including full prompt/response payloads and expense cost data."""
    from database import SessionLocal, BudgetEntry as BE, Expense
    db = SessionLocal()
    try:
        entry = db.query(BE).filter(BE.id == entry_id).first()
        if not entry:
            raise HTTPException(status_code=404, detail="Budget entry not found")
        result = budget_entry_to_dict(entry)
        result["prompt_data"] = json.loads(entry.prompt_data) if entry.prompt_data else None
        result["response_data"] = json.loads(entry.response_data) if entry.response_data else None
        expense = db.query(Expense).filter(Expense.budget_entry_id == entry_id).first()
        result["expense"] = {
            "prompt_cost_microcents": expense.prompt_cost_microcents,
            "completion_cost_microcents": expense.completion_cost_microcents,
            "total_cost_microcents": expense.total_cost_microcents,
        } if expense else None
        return result
    finally:
        db.close()


@app.get("/api/budgets/{budget_id}/summary", response_model=dict)
def read_budget_summary(budget_id: int):
    """Get aggregate token usage for a budget."""
    budget = get_budget(budget_id)
    if not budget:
        raise HTTPException(status_code=404, detail="Budget not found")
    summary = get_budget_summary(budget_id)
    summary["budget_id"] = budget_id
    summary["budget_name"] = budget.name
    return summary


# ============================================
# Stats API Endpoints
# ============================================

@app.get("/api/stats/throughput", response_model=dict)
def get_stats_throughput(bucket_minutes: int = 5, hours: int = 24):
    """Time-bucketed token throughput + grand totals across all projects.

    Returns PP (prompt) and TG (generation) tokens per bucket, suitable for
    a time-series chart. Also returns all-time grand totals.
    """
    from sqlalchemy import text
    from datetime import datetime, timedelta
    db = SessionLocal()
    try:
        bucket_secs = bucket_minutes * 60
        cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()

        buckets = db.execute(text("""
            SELECT
                datetime(
                    CAST(strftime('%s', created_at) / :bsecs AS INTEGER) * :bsecs,
                    'unixepoch'
                ) AS bucket,
                COALESCE(SUM(prompt_cost), 0)     AS pp,
                COALESCE(SUM(generation_cost), 0) AS tg,
                COUNT(*)                           AS calls
            FROM budget_entries
            WHERE created_at >= :cutoff
            GROUP BY bucket
            ORDER BY bucket
        """), {"bsecs": bucket_secs, "cutoff": cutoff}).fetchall()

        totals = db.execute(text("""
            SELECT
                COALESCE(SUM(prompt_cost), 0)     AS total_pp,
                COALESCE(SUM(generation_cost), 0) AS total_tg,
                COUNT(*)                           AS total_calls,
                COUNT(DISTINCT task_id)            AS total_tasks
            FROM budget_entries
        """)).fetchone()

        return {
            "bucket_minutes": bucket_minutes,
            "hours": hours,
            "buckets": [
                {"t": str(r[0]) + "Z", "pp": r[1], "tg": r[2], "calls": r[3]}
                for r in buckets
            ],
            "totals": {
                "pp_tokens":   totals[0],
                "tg_tokens":   totals[1],
                "total_tokens": totals[0] + totals[1],
                "calls":       totals[2],
                "tasks":       totals[3],
            },
        }
    finally:
        db.close()


# ============================================
# Diagnostics API Endpoints
# ============================================

@app.get("/api/diagnostics/tasks", response_model=List[dict])
def list_diagnostic_tasks():
    """Tasks that have LLM activity, with aggregated token counts. Ordered by most-recent activity.

    Also includes a synthetic '__file_summaries__' row for budget entries that have no
    task_id (project-level file summary jobs fired by the scheduler prewarm).
    """
    from sqlalchemy import func, desc
    db = SessionLocal()
    try:
        rows = (
            db.query(
                Task.id,
                Task.title,
                Task.type,
                Project.name.label("project"),
                func.count(BudgetEntry.id).label("entry_count"),
                func.coalesce(func.sum(BudgetEntry.prompt_cost), 0).label("total_prompt_tokens"),
                func.coalesce(func.sum(BudgetEntry.generation_cost), 0).label("total_completion_tokens"),
                func.coalesce(func.sum(BudgetEntry.tool_calls), 0).label("total_tool_calls"),
                func.max(BudgetEntry.created_at).label("last_activity"),
            )
            .join(BudgetEntry, Task.id == BudgetEntry.task_id)
            .outerjoin(Project, Task.project_id == Project.id)
            .group_by(Task.id)
            .order_by(desc("last_activity"))
            .all()
        )
        result = [
            {
                "id": r.id,
                "title": r.title,
                "type": r.type,
                "project": r.project,
                "entry_count": r.entry_count,
                "total_prompt_tokens": r.total_prompt_tokens,
                "total_completion_tokens": r.total_completion_tokens,
                "total_tool_calls": r.total_tool_calls,
                "last_activity": r.last_activity.isoformat() if r.last_activity else None,
            }
            for r in rows
        ]

        # Synthetic row: budget entries with no task_id (scheduler prewarm file summaries)
        orphan = (
            db.query(
                func.count(BudgetEntry.id).label("entry_count"),
                func.coalesce(func.sum(BudgetEntry.prompt_cost), 0).label("total_prompt_tokens"),
                func.coalesce(func.sum(BudgetEntry.generation_cost), 0).label("total_completion_tokens"),
                func.coalesce(func.sum(BudgetEntry.tool_calls), 0).label("total_tool_calls"),
                func.max(BudgetEntry.created_at).label("last_activity"),
            )
            .filter(BudgetEntry.task_id.is_(None))
            .one()
        )
        if orphan.entry_count > 0:
            result.insert(0, {
                "id": "__file_summaries__",
                "title": "File Summaries",
                "type": "file_summary",
                "project": None,
                "entry_count": orphan.entry_count,
                "total_prompt_tokens": orphan.total_prompt_tokens,
                "total_completion_tokens": orphan.total_completion_tokens,
                "total_tool_calls": orphan.total_tool_calls,
                "last_activity": orphan.last_activity.isoformat() if orphan.last_activity else None,
            })

        return result
    finally:
        db.close()


@app.get("/diagnostics")
def read_diagnostics():
    return FileResponse("app/web/diagnostics.html")


@app.get("/stats")
def read_stats():
    return FileResponse("app/web/stats.html")


@app.get("/story")
def read_story():
    return FileResponse("app/web/story.html")


@app.get("/api/tasks/{task_id}/agent-sessions")
def get_task_agent_sessions(task_id: str):
    """All agent session records for a task, oldest first."""
    from app.database import get_agent_sessions_for_task as _get_sessions
    sessions = _get_sessions(task_id)
    result = []
    for s in sessions:
        ended_at = s.ended_at
        started_at = s.started_at
        duration_seconds = None
        if ended_at and started_at:
            try:
                from datetime import datetime, timezone
                t0 = datetime.fromisoformat(started_at)
                t1 = datetime.fromisoformat(ended_at)
                duration_seconds = round((t1 - t0).total_seconds(), 1)
            except Exception:
                pass
        result.append({
            "id": s.id,
            "task_id": s.task_id,
            "agent_type": s.agent_type,
            "started_at": s.started_at,
            "ended_at": s.ended_at,
            "duration_seconds": duration_seconds,
            "turn_count": s.turn_count,
            "max_turns": s.max_turns,
            "exit_reason": s.exit_reason,
            "exit_summary": s.exit_summary,
            "scheduler_reason": s.scheduler_reason,
            "llm_id": s.llm_id,
            "budget_id": s.budget_id,
            "prompt_tokens": s.prompt_tokens,
            "completion_tokens": s.completion_tokens,
        })
    return result


@app.get("/scheduler")
def read_scheduler():
    return FileResponse("app/web/scheduler.html")


@app.get("/tail.html")
def read_tail():
    """Session tail page — live log of active agent LLM calls."""
    return FileResponse("app/web/tail.html")


# ============================================
# Legacy Routes (Still Serving HTML Files)
# ============================================

@app.get("/")
def read_root():
    return FileResponse("app/web/index.html")


@app.get("/kanban.html")
def read_original():
    return FileResponse("app/web/kanban.html")


@app.get("/kanban2.html")
def read_new():
    return FileResponse("app/web/index.html")


# ============================================
# Agent Tools API
# ============================================

# Defines which tools each agent type has access to.
# Research agent: read-only tools only (no writes, no shell, no git mutations).
# Intake pipeline: no direct tools — uses LLM calls with structured prompts only.
# Scheduler: no direct tools — dispatches MaestroLoop instances.
AGENT_TOOL_ACCESS: dict = {
    "MaestroLoop": {
        "description": "Main agentic loop that drives Design → Implement → Test → Verify cycles. Has full tool access.",
        "tools": "*",  # All tools
    },
    "ResearchAgent": {
        "description": "Lightweight read-only investigator spawned by the intake pipeline when votes need clarification. Limited lives, restricted tools.",
        "tools": [
            "read_file", "read_file_metadata",
            "find_in_files", "find_files", "list_directory",
            "read_git_status", "read_git_diff", "read_git_log", "read_git_blame", "read_git_show",
            "get_task", "list_tasks",
        ],
    },
    "IntakePipeline": {
        "description": "4-stage voting pipeline (scope, static analysis, feasibility, conflict) for IDEA→PLANNING transitions. Uses LLM calls with structured prompts — no direct tool dispatch.",
        "tools": [],
    },
    "Scheduler": {
        "description": "Push-first eager scheduler that dispatches DAG-ready tasks to MaestroLoop instances based on LLM capacity. No direct tool dispatch.",
        "tools": [],
    },
    "SubdivisionAgent": {
        "description": "Decomposes oversized ideas into smaller sub-ideas when intake votes SUBDIVIDE_IDEA. Read-only tools, structured decomposition output.",
        "tools": [
            "read_file", "read_file_metadata",
            "find_in_files", "find_files", "list_directory",
            "read_git_status", "read_git_diff", "read_git_log", "read_git_blame", "read_git_show",
            "get_task", "list_tasks",
        ],
    },
    "PlanningPipeline": {
        "description": "5-stage planning pipeline (survey, best-of-N design, review panel, pitfall detection, consolidation). Uses LLM calls — no direct tool dispatch.",
        "tools": [],
    },
    "PlanningGate": {
        "description": "7-check deterministic gate (plus 1 LLM feasibility check) that validates planning output before advancing to development.",
        "tools": [],
    },
    "DevOrchestrator": {
        "description": "Batch execution orchestrator for development. Runs component loops in parallel with file write containment.",
        "tools": "*",
    },
    "ConceptualReviewPipeline": {
        "description": "4 deterministic + 4 LLM reviewers for conceptual review after development. No direct tool dispatch.",
        "tools": [],
    },
    "OptimizationPipeline": {
        "description": "Profile → propose → vote → implement → verify optimization pipeline. No direct tool dispatch.",
        "tools": [],
    },
    "SecurityPipeline": {
        "description": "3 parallel security reviewer agents with veto power. Uses allowlisted security scanner shell.",
        "tools": ["run_shell_security", "read_file", "search_files", "find_files", "list_directory"],
    },
    "FinalReviewPipeline": {
        "description": "4-agent final review (functional, code quality, integration, UX). Uses allowlisted review runner shell.",
        "tools": ["run_shell_review", "read_file", "search_files", "find_files", "list_directory"],
    },
    "MergeWorker": {
        "description": "Deterministic git merge workflow (no LLM). Verifies branch, merges --no-ff, runs test suite, pushes if configured.",
        "tools": [],
    },
}


@app.get("/api/agent/tools", response_model=dict)
def get_agent_tools():
    """
    Return all tool schemas and the agent-to-tool access tree.
    """
    from app.agent.tools import TOOL_SCHEMAS  # noqa: PLC0415
    from app.agent.config import RESEARCH_AGENT_TOOLS, SUBDIVISION_AGENT_TOOLS  # noqa: PLC0415

    # Build the access tree with live config (tools come from maestro.ini)
    access_tree = {}
    for agent_name, info in AGENT_TOOL_ACCESS.items():
        entry = {"description": info["description"]}
        if info["tools"] == "*":
            entry["tools"] = [s["function"]["name"] for s in TOOL_SCHEMAS]
        elif agent_name == "ResearchAgent":
            entry["tools"] = list(RESEARCH_AGENT_TOOLS)
        elif agent_name == "SubdivisionAgent":
            entry["tools"] = list(SUBDIVISION_AGENT_TOOLS)
        else:
            entry["tools"] = info["tools"]
        access_tree[agent_name] = entry

    return {
        "tool_schemas": TOOL_SCHEMAS,
        "agent_access": access_tree,
    }


# ============================================
# Agent API Endpoints
# ============================================

@_pipeline_session
def _run_loop_in_background(task_id: str) -> None:
    """
    Fire-and-forget coroutine runner for MaestroLoop.
    Creates a new event loop in the background thread if needed.
    """
    try:
        from app.agent.loop import MaestroLoop  # noqa: PLC0415

        # Resolve the task's assigned LLM endpoint
        task = get_task(task_id)
        llm_base_url = None
        llm_model = None
        max_context = None
        if task and task.llm_id:
            llm_record = get_llm(task.llm_id)
            if llm_record:
                llm_base_url = f"http://{llm_record.address}:{llm_record.port}/v1"
                llm_model = llm_record.model
                max_context = llm_record.max_context
                logger.info("[agent] Using LLM: %s model=%s ctx=%s", llm_base_url, llm_model, max_context)

        # Resolve project path for git tool isolation
        project_path = None
        if task and task.project:
            from app.database import get_project_path
            project_path = get_project_path(task.project)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            maestro = MaestroLoop(
                task_id=task_id,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                max_context=max_context,
                llm_id=task.llm_id if task else None,
                budget_id=task.budget_id if task else None,
                project_path=project_path,
            )
            loop.run_until_complete(maestro.run())
        finally:
            loop.close()
    except Exception as exc:
        logger.exception("[agent] Background loop for '%s' failed.", task_id)


@app.post("/api/agent/run/{task_id}", response_model=dict)
def start_agent_loop(task_id: str):
    """
    Start a MaestroLoop for the given task ID as a background task.
    The loop runs asynchronously; poll /api/agent/status/{task_id} for progress.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    from app.agent.loop import _ACTIVE_LOOPS  # noqa: PLC0415
    if task_id in _ACTIVE_LOOPS and not _ACTIVE_LOOPS[task_id].done():
        raise HTTPException(status_code=409, detail=f"A loop is already running for task '{task_id}'.")

    _start_bg(_run_loop_in_background, task_id)
    return {
        "task_id": task_id,
        "status": "STARTED",
        "message": f"MaestroLoop started for task '{task_id}'. Poll /api/agent/status/{task_id} for updates.",
    }


@app.get("/api/agent/status/{task_id}", response_model=dict)
def get_agent_status(task_id: str):
    """
    Return the current status of a MaestroLoop for the given task ID.
    Returns the last known status even after the loop completes.
    """
    from app.agent.loop import get_loop_status  # noqa: PLC0415
    status = get_loop_status(task_id)
    if status is None:
        raise HTTPException(status_code=404, detail=f"No loop found for task '{task_id}'.")
    return status


@app.post("/api/agent/stop/{task_id}", response_model=dict)
def stop_agent_loop(task_id: str):
    """
    Request graceful stop of a running MaestroLoop.
    The loop will terminate at its next opportunity.
    """
    from app.agent.loop import request_stop  # noqa: PLC0415
    stopped = request_stop(task_id)
    if not stopped:
        raise HTTPException(status_code=404, detail=f"No active loop found for task '{task_id}'.")
    return {"task_id": task_id, "status": "STOP_REQUESTED"}


# ============================================================
# Task Quick-Actions (toolbar buttons)
# ============================================================

@app.post("/api/tasks/{task_id}/demote", response_model=dict)
def demote_task(task_id: str, body: dict = {}):
    """Move a task one stage backward in the pipeline.

    Optional body: {"target": "<stage>"}  — force a specific target stage.
    Without a target the task drops one position in PIPELINE_COLUMN_ORDER.
    Records a demotion event in demotion_history.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    target = body.get("target") if body else None
    if target:
        if target not in PIPELINE_COLUMN_ORDER:
            raise HTTPException(status_code=400, detail=f"Unknown stage '{target}'")
    else:
        try:
            idx = PIPELINE_COLUMN_ORDER.index(task.type)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"Current stage '{task.type}' is not in the pipeline")
        if idx <= 0:
            raise HTTPException(status_code=400, detail="Task is already at the first stage")
        target = PIPELINE_COLUMN_ORDER[idx - 1]

    _record_demotion(task_id, from_stage=task.type, to_stage=target, reason="manual demote via toolbar")
    updated = update_task(task_id, type=target)
    return task_to_dict(updated)


@app.post("/api/tasks/{task_id}/set-stage", response_model=dict)
def set_task_stage(task_id: str, body: dict):
    """Manually force a task to any pipeline stage.

    Body: {"stage": "<stage>"}
    Does NOT record a demotion (use demote endpoint for that).
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    stage = body.get("stage")
    if not stage or stage not in PIPELINE_COLUMN_ORDER:
        raise HTTPException(status_code=400, detail=f"Invalid stage '{stage}'")
    updated = update_task(task_id, type=stage)
    return task_to_dict(updated)


@app.post("/api/tasks/{task_id}/reset-intake")
def reset_intake(task_id: str):
    """Clear intake_exhausted_at so the scheduler will retry the task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    update_task(task_id, intake_exhausted_at=None)
    append_task_history(task_id, "intake_reset", message="Intake exhaustion cleared by user.")
    return {"ok": True}


@app.get("/api/tasks/{task_id}/transition-history")
def get_transition_history(task_id: str):
    """All intake transition runs for a task in chronological order.

    Returns per-run tally narrative, trigger provenance, and per-vote details.
    Votes are matched to results via a time-window query (no direct FK).
    """
    from app.database import get_transition_votes_for_result, get_agent_sessions_for_task

    results = get_transition_results(task_id, transition="idea_to_planning")
    if not results:
        return []
    # Reverse to chronological order (get_transition_results returns DESC)
    results = list(reversed(results))

    # Intake sessions for trigger provenance
    sessions = [
        s for s in get_agent_sessions_for_task(task_id)
        if s.agent_type == "intake"
    ]

    output = []
    prev_created_at = None
    for run_index, result in enumerate(results):
        votes = get_transition_votes_for_result(
            task_id,
            from_dt=prev_created_at,
            to_dt=result.created_at,
        )

        # Infer trigger: find intake session whose started_at is closest before
        # this result's created_at.
        trigger = "scheduler"
        if sessions and result.created_at:
            for s in reversed(sessions):
                try:
                    sess_start = s.started_at
                    if isinstance(sess_start, str):
                        from datetime import datetime as _dt
                        sess_start = _dt.fromisoformat(sess_start.replace("Z", "+00:00"))
                    if sess_start <= result.created_at:
                        if getattr(s, "scheduler_reason", "scheduler") == "user_triggered":
                            trigger = "user"
                        break
                except Exception:
                    pass

        votes_data = [
            {
                "stage": v.stage,
                "verdict": v.verdict,
                "confidence": v.confidence,
                "justification": v.justification,
                "model": v.model or "",
                "prompt_tokens": v.prompt_tokens or 0,
                "completion_tokens": v.completion_tokens or 0,
            }
            for v in votes
        ]

        forced = bool(
            result.vote_summary and result.vote_summary.get("forced")
        ) if result.vote_summary else False

        output.append({
            "run": run_index + 1,
            "outcome": result.outcome,
            "created_at": result.created_at.isoformat() if result.created_at else None,
            "trigger": trigger,
            "tally_narrative": _compute_tally_narrative(votes_data, result.outcome, forced),
            "votes": votes_data,
            "total_prompt_tokens": result.total_prompt_tokens or 0,
            "total_completion_tokens": result.total_completion_tokens or 0,
            "forced": forced,
        })
        prev_created_at = result.created_at

    return output


@app.get("/api/tasks/{task_id}/planning-gate-results")
def get_planning_gate_results(task_id: str):
    """Planning gate check results for a task, chronological order."""
    results = get_transition_results(task_id, transition="planning_gate")
    if not results:
        return []
    results = list(reversed(results))  # chronological
    output = []
    for i, r in enumerate(results):
        vs = r.vote_summary or {}
        output.append({
            "run": i + 1,
            "created_at": r.created_at.isoformat() if r.created_at else None,
            "passed": r.outcome != "rejected",
            "llm_check_unavailable": vs.get("llm_check_unavailable", False),
            "checks": vs.get("checks", []),
            "prompt_tokens": r.total_prompt_tokens or 0,
            "completion_tokens": r.total_completion_tokens or 0,
        })
    return output


@app.get("/api/tasks/{task_id}/component-results")
def get_task_component_results(task_id: str):
    """Per-component DevOrchestrator results for a task."""
    from app.database import get_component_results
    rows = get_component_results(task_id)
    return [
        {
            "id": r.id,
            "component_name": r.component_name,
            "batch_number": r.batch_number,
            "step_order": r.step_order,
            "status": r.status,
            "files_changed": json.loads(r.files_changed or "[]"),
            "tests_passed": r.tests_passed,
            "turns_used": r.turns_used,
            "error_detail": r.error_detail,
            "prompt_tokens": r.prompt_tokens or 0,
            "completion_tokens": r.completion_tokens or 0,
            "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        }
        for r in rows
    ]


def _compute_tally_narrative(votes_data: list, outcome: str, forced: bool = False) -> str:
    """Plain-English explanation of why the tally produced the given outcome."""
    if forced:
        return "Forced subdivision after repeated rejections (no valid votes)."

    subdivide_v = [v for v in votes_data if v["verdict"] == "SUBDIVIDE_IDEA"]
    rejected_v = [v for v in votes_data if v["verdict"] == "REJECTED"]
    not_suitable_v = [v for v in votes_data if v["verdict"] == "NOT_SUITABLE"]
    needs_research_v = [v for v in votes_data if v["verdict"] == "NEEDS_RESEARCH"]
    llm_stage_count = sum(1 for v in votes_data if v["stage"] != "static_analysis")

    if subdivide_v:
        subdivide_threshold = max(2, (llm_stage_count // 2) + 1)
        stages = ", ".join(v["stage"] for v in subdivide_v)
        if len(subdivide_v) >= subdivide_threshold:
            return (
                f"Rule 0 fired: {len(subdivide_v)}/{llm_stage_count} LLM stages voted SUBDIVIDE_IDEA "
                f"({stages}) — threshold {subdivide_threshold} met."
            )
        else:
            return (
                f"Rule 0 not met: {len(subdivide_v)}/{llm_stage_count} LLM stages voted SUBDIVIDE_IDEA "
                f"({stages}) — threshold {subdivide_threshold} not reached, "
                f"outcome resolved by other rules."
            )

    if rejected_v:
        stage = rejected_v[0]["stage"]
        conf = rejected_v[0]["confidence"]
        return f"Rule 1 fired: {stage} voted REJECTED ({conf}%) \u2192 immediate rejection."

    n = len(votes_data)
    majority_threshold = (n // 2) + 1 if n > 0 else 1
    if len(not_suitable_v) >= majority_threshold:
        return (
            f"Rule 2 fired: {len(not_suitable_v)}/{n} stages voted NOT_SUITABLE "
            f"(majority threshold {majority_threshold})."
        )

    if needs_research_v:
        stages = ", ".join(v["stage"] for v in needs_research_v)
        return f"Rule 3 fired: {len(needs_research_v)} stage(s) need research ({stages})."

    if outcome == "tie":
        return "Rule 4 fired: equal split of pass-ish vs fail-ish votes."

    if outcome in ("passed", "conditional_pass"):
        return "All stages passed — no blocking votes."

    return f"Outcome: {outcome}."


@app.post("/api/tasks/{task_id}/clone", response_model=dict)
def clone_task(task_id: str):
    """Clone a task as a new IDEA in the same project.

    Copies title, description, tags, llm_id, budget_id.
    New task starts in the 'idea' stage with no history.
    """
    import uuid
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    new_task = create_task(
        id=str(uuid.uuid4()),
        title=f"[Clone] {task.title}",
        type="idea",
        description=task.description or "",
        owner=task.owner or "user",
        tags=list(task.tags or []),
        llm_id=task.llm_id,
        budget_id=task.budget_id,
        project=task.project or "TheMaestro",
    )
    if not new_task:
        raise HTTPException(status_code=500, detail="Failed to create clone")
    return task_to_dict(new_task)


@app.post("/api/tasks/{task_id}/pin", response_model=dict)
def pin_task(task_id: str):
    """Move a task to position 0 (top of its column)."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    updated = update_task(task_id, position=0)
    return task_to_dict(updated)


@app.post("/api/tasks/{task_id}/star", response_model=dict)
def star_task(task_id: str):
    """Toggle is_starred flag — starred tasks are dispatched ahead of the general queue."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from datetime import datetime as _dt
    updated = update_task(task_id, is_starred=not bool(task.is_starred),
                          last_progress_at=_dt.utcnow())
    return task_to_dict(updated)


class RunPlanningRequest(BaseModel):
    force: bool = False   # recompute with prior failure context injected
    fresh: bool = False   # recompute with no prior context at all


class CacheModeRequest(BaseModel):
    mode: str  # 'normal' | 'force_with_context' | 'force_fresh'


@app.post("/api/tasks/{task_id}/cache-mode", response_model=dict)
def set_task_cache_mode(task_id: str, body: CacheModeRequest):
    """Set the planning cache mode for a task.

    normal            — reuse cached planning result if spec unchanged (default)
    force_with_context — skip cache, recompute but inject prior failure context
    force_fresh        — skip cache, recompute with no prior context
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    valid = {'normal', 'force_with_context', 'force_fresh'}
    if body.mode not in valid:
        raise HTTPException(status_code=400, detail=f"mode must be one of {valid}")
    update_task(task_id, cache_mode=body.mode)
    return {"task_id": task_id, "cache_mode": body.mode}


@app.post("/api/tasks/{task_id}/run-planning", response_model=dict)
def run_planning_on_demand(task_id: str, body: Optional[RunPlanningRequest] = None):
    """Manually trigger the planning pipeline for a task (any stage).

    Optional body:
      force=true  — bypass cache, recompute with prior failure context
      fresh=true  — bypass cache, recompute with no prior data
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id or not task.budget_id:
        raise HTTPException(status_code=400, detail="Task needs an LLM endpoint and budget assigned.")
    if body and body.fresh:
        update_task(task_id, cache_mode='force_fresh')
    elif body and body.force:
        update_task(task_id, cache_mode='force_with_context')
    # Clear stopped state so the scheduler (and status API) no longer shows this as stopped.
    from app.agent.scheduler import clear_planning_stopped
    clear_planning_stopped(task_id)
    _start_bg(_run_planning_pipeline_bg, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "planning"}


@app.post("/api/tasks/{task_id}/run-review", response_model=dict)
def run_conceptual_review_on_demand(task_id: str):
    """Manually trigger the conceptual review pipeline for a task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id or not task.budget_id:
        raise HTTPException(status_code=400, detail="Task needs an LLM endpoint and budget assigned.")
    _start_bg(_advance_to_optimization, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "conceptual_review"}


@app.post("/api/tasks/{task_id}/run-optimization", response_model=dict)
def run_optimization_on_demand(task_id: str):
    """Manually trigger the optimization pipeline only (no security)."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id or not task.budget_id:
        raise HTTPException(status_code=400, detail="Task needs an LLM endpoint and budget assigned.")
    _start_bg(_run_optimization_only_bg, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "optimization"}


@app.post("/api/tasks/{task_id}/run-security", response_model=dict)
def run_security_on_demand(task_id: str):
    """Manually trigger the security review pipeline only (no optimization)."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id or not task.budget_id:
        raise HTTPException(status_code=400, detail="Task needs an LLM endpoint and budget assigned.")
    _start_bg(_run_security_only_bg, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "security"}


@app.post("/api/tasks/{task_id}/run-final-review", response_model=dict)
def run_final_review_on_demand(task_id: str):
    """Manually trigger the final review pipeline for a task."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.llm_id or not task.budget_id:
        raise HTTPException(status_code=400, detail="Task needs an LLM endpoint and budget assigned.")
    _start_bg(_run_final_review_pipeline_bg, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "final_review"}


# ===========================================================================
# PIP (Performance Improvement Plan) endpoints
# ===========================================================================

@app.get("/api/tasks/{task_id}/pips", response_model=List[dict])
def get_task_pips(task_id: str):
    """Return all PIPs for a task with full verification history per PIP."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.database import get_pips_for_task, get_pip_verifications_for_pip, pip_status_at_stage
    result = []
    for pip in get_pips_for_task(task_id):
        reqs = json.loads(pip.requirements) if pip.requirements else []
        verifications = get_pip_verifications_for_pip(pip.id)
        created_str = (
            pip.created_at.isoformat() if hasattr(pip.created_at, "isoformat")
            else str(pip.created_at)
        )
        result.append({
            "id": pip.id,
            "origin_stage": pip.origin_stage,
            "requirements": reqs,
            "created_at": created_str,
            "status": pip_status_at_stage(pip, task.type),
            "verifications": [
                {
                    "id": v.id,
                    "checked_at_stage": v.checked_at_stage,
                    "outcome": v.outcome,
                    "summary": v.summary,
                    "findings": json.loads(v.findings) if v.findings else [],
                    "created_at": v.created_at,
                }
                for v in verifications
            ],
        })
    return result


@app.get("/api/tasks/{task_id}/pips/{pip_id}/verifications", response_model=List[dict])
def get_pip_verifications(task_id: str, pip_id: int):
    """Return verification history for one PIP across all stages."""
    if not get_task(task_id):
        raise HTTPException(status_code=404, detail="Task not found")
    from app.database import get_pip_verifications_for_pip
    return [
        {
            "id": v.id,
            "checked_at_stage": v.checked_at_stage,
            "outcome": v.outcome,
            "summary": v.summary,
            "findings": json.loads(v.findings) if v.findings else [],
            "created_at": v.created_at,
        }
        for v in get_pip_verifications_for_pip(pip_id)
    ]


@app.post("/api/tasks/{task_id}/pips/{pip_id}/verify", response_model=dict)
def run_pip_verify(task_id: str, pip_id: int, body: dict = Body(default={})):
    """Manually trigger pre-flight for one PIP at the task's current stage.

    Runs synchronously and returns {outcome, summary, findings}.
    Also persists a pip_verification row.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.database import get_pips_for_task, create_pip_verification, get_project_path
    from app.agent.pip_agent import _check_single_pip
    from app.agent.project_snapshot import build_project_snapshot

    pips = get_pips_for_task(task_id)
    pip = next((p for p in pips if p.id == pip_id), None)
    if not pip:
        raise HTTPException(status_code=404, detail="PIP not found for this task")

    llm_id = body.get("llm_id") or task.llm_id
    budget_id = body.get("budget_id") or task.budget_id
    if not llm_id or not budget_id:
        raise HTTPException(status_code=400, detail="Task has no LLM or budget configured")

    project_path = get_project_path(task.project) if task.project else None
    try:
        snapshot = build_project_snapshot(project_path) if project_path else ""
    except Exception:
        snapshot = ""

    _loop = asyncio.new_event_loop()
    try:
        result = _loop.run_until_complete(
            _check_single_pip(pip, task, task.type, snapshot, llm_id, budget_id, project_path)
        )
    finally:
        _loop.close()

    create_pip_verification(
        pip_id=pip_id,
        task_id=task_id,
        stage=task.type,
        outcome=result["outcome"],
        summary=result["summary"],
        findings=json.dumps(result["findings"]),
    )
    return result


@app.post("/api/tasks/{task_id}/run-pip-resolution/{pip_id}", status_code=202)
def trigger_pip_resolution(task_id: str, pip_id: int, body: dict = Body(default={})):
    """Queue a PIP Resolution Agent for one PIP. Returns 202 Accepted.

    Creates (or returns existing) pip_resolution_job row; the scheduler
    dispatches the research + resolution agent on the next tick.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    from app.database import get_pips_for_task, create_pip_resolution_job
    pips = get_pips_for_task(task_id)
    if not any(p.id == pip_id for p in pips):
        raise HTTPException(status_code=404, detail="PIP not found for this task")
    job = create_pip_resolution_job(task_id, pip_id, task.type)
    return {"status": "accepted", "job_id": job.id if job else None}


@app.post("/api/tasks/{task_id}/merge", response_model=dict)
def run_merge_manually(task_id: str):
    """Manually trigger the final merge to main for a task in FINAL REVIEW."""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    _start_bg(_execute_merge_bg, task_id)
    return {"task_id": task_id, "status": "STARTED", "pipeline": "merge"}


@app.post("/api/tasks/{task_id}/unmerge", response_model=dict)
def unmerge_task(task_id: str):
    """Revert a completed merged task: git revert the merge commit and move back to human_review."""
    import subprocess as _subprocess
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.type != "completed":
        raise HTTPException(status_code=400, detail=f"Task is '{task.type}', not 'completed'")

    mr = get_merge_record(task_id)
    if not mr or not mr.merge_commit_sha:
        # No merge record — just move the card back without touching git
        _record_demotion(task_id, from_stage="completed", to_stage="human_review", reason="manual unmerge (no merge record)")
        updated = update_task(task_id, type="human_review")
        return {**task_to_dict(updated), "git": "skipped — no merge record found"}

    sha = mr.merge_commit_sha

    from app.database import get_project_path
    project_path = get_project_path(task.project) if task.project else None

    def _git(*args):
        r = _subprocess.run(
            ["git"] + list(args),
            capture_output=True, text=True, timeout=30,
            cwd=project_path,
        )
        return r.returncode, (r.stdout + r.stderr).strip()

    # Ensure we are on main/master before reverting
    rc, _ = _git("checkout", "main")
    if rc != 0:
        rc, _ = _git("checkout", "master")
        if rc != 0:
            raise HTTPException(status_code=500, detail="Cannot checkout main/master branch")

    rc, out = _git("revert", "-m", "1", "--no-edit", sha)
    if rc != 0:
        raise HTTPException(status_code=500, detail=f"git revert failed: {out[:500]}")

    _record_demotion(task_id, from_stage="completed", to_stage="human_review", reason=f"manual unmerge — reverted {sha[:8]}")
    updated = update_task(task_id, type="human_review")
    append_task_history(task_id, "unmerged", message=f"Merge commit {sha[:8]} reverted. Moved back to Human Review.")
    return {**task_to_dict(updated), "git": f"reverted {sha[:8]}"}


@app.get("/api/agent/tasks/ready", response_model=List[dict])
def get_ready_tasks():
    """
    Return all Kanban tasks that are DAG-ready (all prerequisites completed).
    Uses the DAGResolver to compute readiness.
    """
    from app.agent.dag import DAGResolver  # noqa: PLC0415
    all_tasks = get_all_tasks()
    task_dicts = [task_to_dict(t) for t in all_tasks]
    resolver = DAGResolver(task_dicts)
    return resolver.get_ready_tasks()


@app.get("/api/scheduler/status", response_model=dict)
def scheduler_status():
    """Return the current state of the push-first eager scheduler."""
    from app.agent.scheduler import get_scheduler_status  # noqa: PLC0415
    return get_scheduler_status()


@app.get("/api/scheduler/tail")
async def scheduler_tail():
    """SSE endpoint that streams budget entries for all active scheduler sessions.

    Shows one entry per active agent session (the latest LLM call for each).
    When a session's latest call updates, the entry is re-emitted so the client
    can replace its existing row for that session.

    Events:
      - entry: JSON blob for the latest entry of an active session
      - heartbeat: keepalive every 15s
      - error: JSON error message on failure
    """
    from app.database import SessionLocal, BudgetEntry  # noqa: PLC0415
    from app.agent.scheduler import _active_sessions, _active_sessions_lock  # noqa: PLC0415
    import time

    def event_stream():
        db = SessionLocal()
        # session_id -> max entry id emitted for that session
        last_emitted: dict[str, int] = {}
        heartbeat_counter = 0

        try:
            while True:
                # 1. Discover active session IDs from the scheduler
                active_task_ids: set[str] = set()
                with _active_sessions_lock:
                    active_task_ids = {
                        tid for tid, t in _active_sessions.items() if t.is_alive()
                    }

                if not active_task_ids:
                    time.sleep(1)
                    heartbeat_counter += 1
                    yield f"event: heartbeat\ndata: {{\"time\": {time.time()}}}\n\n"
                    continue

                # 2. Fetch all budget entries for active tasks since last poll
                min_id = max(last_emitted.values()) if last_emitted else 0
                entries = (
                    db.query(BudgetEntry)
                    .filter(
                        BudgetEntry.task_id.in_(list(active_task_ids)),
                        BudgetEntry.id > min_id,
                    )
                    .order_by(BudgetEntry.id.asc())
                    .all()
                )

                if entries:
                    # Group by session_id, keep only the latest entry per session
                    latest_by_session: dict[str, BudgetEntry] = {}
                    for entry in entries:
                        sid = entry.session_id or ""
                        if sid not in latest_by_session or entry.id > latest_by_session[sid].id:
                            latest_by_session[sid] = entry

                    for sid, entry in latest_by_session.items():
                        emitted_id = last_emitted.get(sid, 0)
                        if entry.id > emitted_id:
                            last_emitted[sid] = entry.id

                            # Extract finish_reason and content_preview from response_data
                            finish_reason = None
                            content_preview = ""
                            if entry.response_data:
                                try:
                                    resp = json.loads(entry.response_data)
                                    choices = resp.get("choices", [])
                                    if choices:
                                        msg = choices[0].get("message", {})
                                        content = msg.get("content", "")
                                        if content:
                                            content_preview = content[:500]
                                        finish_reason = choices[0].get("finish_reason")
                                except (json.JSONDecodeError, KeyError, IndexError):
                                    pass

                            task_title = ""
                            if entry.task_id:
                                try:
                                    task = db.query(Task).filter(Task.id == entry.task_id).first()
                                    if task:
                                        task_title = (task.title or entry.task_id)[:120]
                                except Exception:
                                    task_title = entry.task_id

                            payload = {
                                "id": entry.id,
                                "created_at": entry.created_at.isoformat() if entry.created_at else "",
                                "task_id": entry.task_id or "",
                                "task_title": task_title,
                                "agent_name": entry.agent_name or "",
                                "session_id": sid,
                                "prompt_tokens": entry.prompt_cost,
                                "completion_tokens": entry.generation_cost,
                                "tool_calls": entry.tool_calls,
                                "finish_reason": finish_reason,
                                "content_preview": content_preview,
                                "response_data": entry.response_data if entry.response_data else "",
                            }
                            yield f"event: entry\ndata: {json.dumps(payload, ensure_ascii=False)}\n\n"

                time.sleep(1)
                heartbeat_counter += 1
                if heartbeat_counter >= 15:
                    yield f"event: heartbeat\ndata: {{\"time\": {time.time()}}}\n\n"
                    heartbeat_counter = 0

        except GeneratorExit:
            pass
        finally:
            db.close()

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


# ---------------------------------------------------------------------------
# Admin routes
# ---------------------------------------------------------------------------

@app.post("/api/admin/restart", response_model=dict)
async def admin_restart():
    """Trigger a graceful process exit so the Launcher.ps1 loop can restart the server.

    Requires [server] allow_remote_restart = true in maestro.ini (default: false).
    SECURITY: never expose this endpoint to the public internet.

    Mechanism:
      1. Writes restart.flag to the project root.
      2. Returns the response immediately.
      3. Signals shutdown and waits up to 55s for active sessions (LLM calls/jobs)
          to finish naturally.
      4. Calls os._exit(0) — Launcher.ps1 detects the flag and relaunches uvicorn.
    """
    from app.agent.config import SERVER_ALLOW_REMOTE_RESTART, PROJECT_ROOT
    if not SERVER_ALLOW_REMOTE_RESTART:
        raise HTTPException(
            status_code=403,
            detail=(
                "Remote restart is disabled. "
                "Set allow_remote_restart = true under [server] in maestro.ini."
            ),
        )

    import pathlib
    flag_path = pathlib.Path(PROJECT_ROOT) / "restart.flag"
    flag_path.write_text("restart\n", encoding="utf-8")
    logger.warning("[admin] restart_server: flag written to %s — entering graceful shutdown (max 55s).", flag_path)

    async def _exit_soon():
        # Wait a bit to ensure the HTTP response is sent before we block/drain
        await asyncio.sleep(0.5)
        
        from app.agent.llm_client import signal_shutdown
        from app.agent.scheduler import stop_scheduler
        
        logger.info("[admin] Gentle shutdown: waiting up to 55s for active LLM sessions to finish.")
        signal_shutdown()
        
        # Total timeout 55s as requested for "gentle shutdown"
        # stop_scheduler already handles Phase 1/Phase 2 logging internally.
        stop_scheduler(wait_for_sessions=True, timeout=55.0)
        
        logger.warning("[admin] Gentle shutdown complete — exiting process now.")
        os._exit(0)

    asyncio.create_task(_exit_soon())
    return {
        "status": "restarting",
        "message": "Restart triggered. Server will drain active sessions (max 55s) and restart shortly.",
    }


# ---------------------------------------------------------------------------
# Inbox / notification routes
# ---------------------------------------------------------------------------

@app.get("/api/inbox", response_model=List[dict])
def list_inbox(unread: bool = False):
    """Return inbox messages, newest first. ?unread=true filters to unread only."""
    return get_inbox_messages(unread_only=unread)


@app.get("/api/inbox/unread-count", response_model=dict)
def inbox_unread_count():
    return {"count": count_unread_inbox()}


@app.get("/api/inbox/escalations", response_model=List[dict])
def inbox_escalations():
    """Return unread needs_human escalation messages, newest first."""
    return get_inbox_messages(unread_only=True, source_type="needs_human")


@app.post("/api/inbox", response_model=dict)
def create_inbox(payload: dict):
    """Create an inbox message. Body: {subject, source_type?, task_id?, task_title?, outcome?, data_json?}"""
    return create_inbox_message(
        subject=payload.get("subject", "Notification"),
        source_type=payload.get("source_type", "intake_result"),
        task_id=payload.get("task_id"),
        task_title=payload.get("task_title"),
        outcome=payload.get("outcome"),
        data_json=payload.get("data_json"),
    )


@app.patch("/api/inbox/{msg_id}", response_model=dict)
def update_inbox(msg_id: str, payload: dict):
    """Update an inbox message. Body: {read: bool}"""
    if "read" in payload:
        result = mark_inbox_read(msg_id, bool(payload["read"]))
        if result is None:
            raise HTTPException(status_code=404, detail=f"Inbox message '{msg_id}' not found.")
        return result
    raise HTTPException(status_code=400, detail="No supported fields in payload.")


@app.post("/api/inbox/mark-all-read", response_model=dict)
def inbox_mark_all_read():
    n = mark_all_inbox_read()
    return {"marked_read": n}


@app.delete("/api/inbox/{msg_id}", response_model=dict)
def delete_inbox(msg_id: str):
    ok = delete_inbox_message(msg_id)
    if not ok:
        raise HTTPException(status_code=404, detail=f"Inbox message '{msg_id}' not found.")
    return {"deleted": True}
