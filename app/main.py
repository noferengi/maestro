import sys
import os

# Add app directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastapi import FastAPI, HTTPException, Body, BackgroundTasks
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session
from typing import List
import asyncio
import json
from pydantic import BaseModel
from database import (
    init_db, get_db, create_task, get_task, get_tasks_by_type,
    update_task, delete_task, get_all_tasks, get_task_history, reorder_tasks, seed_sample_tasks,
    get_tasks_by_project,
    LLM, Budget,
    get_all_llms, get_llm, create_llm, update_llm, delete_llm,
    get_all_budgets, get_budget, create_budget, update_budget, delete_budget,
    TransitionVote, TransitionResult,
    create_transition_vote, get_transition_votes,
    create_transition_result, get_transition_results,
)

app = FastAPI(title="Kanban Board API")

# Mount static files directory
app.mount("/static", StaticFiles(directory="app/web"), name="static")

# Initialize database on startup
@app.on_event("startup")
def startup_event():
    init_db()
    seed_sample_tasks()


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


_HUMAN_CREATABLE_TYPES = {'idea', 'architecture'}


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

    return task_to_dict(task)


# Column ordering for advancement detection
_COLUMN_ORDER = ['architecture', 'idea', 'planning', 'development', 'review', 'completed']


def _is_advancing(old_type: str, new_type: str) -> bool:
    """True when the type change moves a task forward in the pipeline."""
    try:
        return _COLUMN_ORDER.index(new_type) > _COLUMN_ORDER.index(old_type)
    except ValueError:
        return False


@app.put("/api/tasks/{task_id}", response_model=dict)
def update_existing_task(task_id: str, task_data: dict):
    """Update an existing task"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Only allow updating specific fields
    allowed_fields = ['title', 'description', 'owner', 'tags', 'content', 'llm_id', 'budget_id', 'type', 'prerequisites']
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

    return task_to_dict(updated_task)


@app.delete("/api/tasks/{task_id}", response_model=bool)
def delete_task_endpoint(task_id: str):
    """Delete a task"""
    result = delete_task(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="Task not found")
    return result


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


def _run_intake_pipeline(task_id: str) -> None:
    """Background runner for the intake pipeline."""
    try:
        import asyncio
        from app.agent.intake import run_intake_pipeline

        task = get_task(task_id)
        if not task:
            print(f"[intake] Task '{task_id}' not found.")
            return

        all_tasks = get_all_tasks()
        task_dicts = [task_to_dict(t) for t in all_tasks]

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
                )
            )

            # Store the result
            create_transition_result(
                task_id=task_id,
                transition="idea_to_planning",
                outcome=result["outcome"],
                vote_summary=result,
                total_prompt_tokens=result.get("total_prompt_tokens", 0),
                total_completion_tokens=result.get("total_completion_tokens", 0),
            )

            # Store individual votes
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
                    budget_id=task.budget_id,
                )

            # Act on the result
            if result["outcome"] == "passed":
                update_task(task_id, type="planning")
                print(f"[intake] Task '{task_id}' advanced to PLANNING.")
            else:
                print(f"[intake] Task '{task_id}' pipeline result: {result['outcome']}")

        finally:
            loop.close()
    except Exception as exc:
        print(f"[intake] Pipeline for '{task_id}' failed: {exc}")


@app.post("/api/tasks/{task_id}/advance", response_model=dict)
def advance_task(task_id: str, background_tasks: BackgroundTasks):
    """
    Request advancement of a task to the next column.
    For IDEA -> PLANNING, this triggers the intake pipeline.
    The pipeline runs asynchronously; poll /api/tasks/{task_id}/transition-status for progress.
    """
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail=f"Task '{task_id}' not found.")

    if task.type != 'idea':
        raise HTTPException(
            status_code=422,
            detail=f"Task is in '{task.type}' column. Only IDEA tasks can be advanced via this endpoint."
        )

    # Check required fields
    if not task.description:
        raise HTTPException(status_code=422, detail="Task must have a description before advancing.")
    if not task.llm_id:
        raise HTTPException(status_code=422, detail="Task must have an LLM endpoint assigned before advancing.")
    if not task.budget_id:
        raise HTTPException(status_code=422, detail="Task must have a budget assigned before advancing.")

    # Launch pipeline in background
    background_tasks.add_task(_run_intake_pipeline, task_id)

    return {
        "task_id": task_id,
        "status": "PIPELINE_STARTED",
        "message": f"Intake pipeline started for task '{task_id}'. Poll /api/tasks/{task_id}/transition-status for updates."
    }


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


# ============================================
# Helper Functions
# ============================================

def task_to_dict(task):
    """Convert SQLAlchemy Task model to dictionary"""
    llm_obj = getattr(task, 'llm_ref', None)
    budget_obj = getattr(task, 'budget_ref', None)
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
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None
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
    }


def budget_to_dict(budget):
    """Convert SQLAlchemy Budget model to dictionary"""
    return {
        "id": budget.id,
        "name": budget.name,
        "settings": budget.settings,
    }


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
    if not data.get('address') or not data.get('model'):
        raise HTTPException(status_code=400, detail="address and model are required")
    llm = create_llm(
        address=data['address'],
        port=data.get('port', 8008),
        model=data['model'],
        settings=data.get('settings'),
    )
    if not llm:
        raise HTTPException(status_code=409, detail="LLM with this address/port/model already exists")
    return llm_to_dict(llm)


@app.put("/api/llms/{llm_id}", response_model=dict)
def update_existing_llm(llm_id: int, data: dict):
    allowed = ['address', 'port', 'model', 'settings']
    updates = {k: v for k, v in data.items() if k in allowed}
    llm = update_llm(llm_id, **updates)
    if not llm:
        raise HTTPException(status_code=404, detail="LLM not found")
    return llm_to_dict(llm)


@app.delete("/api/llms/{llm_id}", response_model=bool)
def delete_llm_endpoint(llm_id: int):
    if not delete_llm(llm_id):
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
        settings=data.get('settings'),
    )
    if not budget:
        raise HTTPException(status_code=409, detail="Budget with this name already exists")
    return budget_to_dict(budget)


@app.put("/api/budgets/{budget_id}", response_model=dict)
def update_existing_budget(budget_id: int, data: dict):
    allowed = ['name', 'settings']
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
# Agent API Endpoints
# ============================================

def _run_loop_in_background(task_id: str) -> None:
    """
    Fire-and-forget coroutine runner for MaestroLoop.
    Creates a new event loop in the background thread if needed.
    """
    try:
        from app.agent.loop import MaestroLoop  # noqa: PLC0415
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            maestro = MaestroLoop(task_id=task_id)
            loop.run_until_complete(maestro.run())
        finally:
            loop.close()
    except Exception as exc:
        print(f"[agent] Background loop for '{task_id}' failed: {exc}")


@app.post("/api/agent/run/{task_id}", response_model=dict)
def start_agent_loop(task_id: str, background_tasks: BackgroundTasks):
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

    background_tasks.add_task(_run_loop_in_background, task_id)
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
