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
    get_tasks_by_project
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


@app.post("/api/tasks", response_model=dict)
def create_new_task(task_data: dict):
    """Create a new task"""
    if not task_data.get('title'):
        raise HTTPException(status_code=400, detail="Title is required")

    task = create_task(
        title=task_data['title'],
        task_type=task_data['type'],
        description=task_data.get('description', ''),
        owner=task_data.get('owner', 'user'),
        tags=task_data.get('tags', []),
        content=task_data.get('content'),
        prerequisites=task_data.get('prerequisites', []),
        project=task_data.get('project', 'TheMaestro')
    )

    if not task:
        raise HTTPException(status_code=500, detail="Failed to create task")

    return task_to_dict(task)


@app.put("/api/tasks/{task_id}", response_model=dict)
def update_existing_task(task_id: str, task_data: dict):
    """Update an existing task"""
    task = get_task(task_id)
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Only allow updating specific fields
    allowed_fields = ['title', 'description', 'owner', 'tags', 'content', 'type', 'prerequisites']
    update_data = {key: value for key, value in task_data.items() if key in allowed_fields}

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


# ============================================
# Helper Functions
# ============================================

def task_to_dict(task):
    """Convert SQLAlchemy Task model to dictionary"""
    return {
        "id": task.id,
        "title": task.title,
        "type": task.type,
        "description": task.description,
        "owner": task.owner,
        "tags": task.tags,
        "content": task.content,
        "history": task.history,
        "prerequisites": getattr(task, "prerequisites", None) or [],
        "position": task.position,
        "project": getattr(task, "project", None) or "TheMaestro",
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None
    }


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
