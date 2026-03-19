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
    LLM, Budget, BudgetEntry, SubdivisionRecord,
    get_all_llms, get_llm, create_llm, update_llm, delete_llm,
    get_all_budgets, get_budget, create_budget, update_budget, delete_budget,
    TransitionVote, TransitionResult,
    create_transition_vote, get_transition_votes,
    create_transition_result, get_transition_results,
    get_budget_entries, get_budget_summary,
    create_subdivision_record, get_subdivision_records,
    get_child_tasks, get_active_child_tasks, count_total_sub_ideas,
    update_subdivision_record,
    get_descendant_tree, set_big_idea_flag, batch_reorder_tasks,
)
from database import (
    PlanningResult, ComponentResult, OptimizationResult,
    SecurityReviewResult, FullReviewResult, MergeRecord,
    create_planning_result, get_planning_result,
    create_component_result, get_component_results,
    create_optimization_result, get_optimization_result,
    create_security_review_result, get_security_review_results,
    create_full_review_result, get_full_review_results,
    create_merge_record, get_merge_record,
)

from app.agent.config import PIPELINE_COLUMN_ORDER, PIPELINE_DONE_STATUSES

app = FastAPI(title="Kanban Board API")

# Mount static files directory
app.mount("/static", StaticFiles(directory="app/web"), name="static")

# Initialize database and scheduler on startup
@app.on_event("startup")
def startup_event():
    init_db()
    seed_sample_tasks()
    from app.agent.scheduler import start_scheduler
    start_scheduler()


@app.on_event("shutdown")
def shutdown_event():
    from app.agent.scheduler import stop_scheduler
    stop_scheduler()


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

    # Check for completion rollup if task moved to completed
    if new_type and new_type.lower() in PIPELINE_DONE_STATUSES:
        _check_completion_rollup(task_id)

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


def _execute_subdivision(task, llm_base_url, llm_model, max_context, scope_vote, rejection_context, loop):
    """Run the SubdivisionAgent and return the result."""
    from app.agent.subdivide import run_subdivision

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
        )
    )


def _create_sub_idea_tasks(task, sub_result, generation):
    """Create child tasks from SubdivisionResult, return list of child IDs."""
    from datetime import datetime

    child_ids = []
    # Build a mapping from sub-N index to real task ID
    index_to_id = {}

    for i, sub_idea in enumerate(sub_result.sub_ideas):
        child_id = f"task-{datetime.now().timestamp()}-sub{i}"
        index_to_id[f"sub-{i}"] = child_id
        child_ids.append(child_id)

    # Create the actual tasks with resolved prerequisites
    for i, sub_idea in enumerate(sub_result.sub_ideas):
        prereqs = []
        for p in sub_idea.prerequisites:
            resolved = index_to_id.get(p)
            if resolved:
                prereqs.append(resolved)

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
        )
        # Update the just-created task with parent info
        # (create_task generates its own ID; we need to use the ID we planned)
        # Actually, create_task uses timestamp-based IDs, so we need to update
        # the last created task to set parent_task_id and subdivision_generation
        from database import SessionLocal, Task as TaskModel
        db = SessionLocal()
        try:
            # Find the most recently created task with this title
            latest = (db.query(TaskModel)
                      .filter(TaskModel.title == sub_idea.title,
                              TaskModel.owner == "system")
                      .order_by(TaskModel.created_at.desc())
                      .first())
            if latest:
                latest.parent_task_id = task.id
                latest.subdivision_generation = generation
                child_ids[i] = latest.id  # use the real ID
                if prereqs:
                    latest.prerequisites = prereqs
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
        print(f"[intake] Task '{task.id}' hit subdivision depth limit ({SUBDIVISION_MAX_DEPTH}). "
              f"Downgrading to rejected.")
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
        print(f"[intake] Total sub-ideas ({total_existing}) >= limit ({SUBDIVISION_MAX_TOTAL_SUB_IDEAS}). "
              f"Downgrading to rejected.")
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
        print(f"[intake] Subdivision agent returned low confidence ({sub_result.confidence}) "
              f"or no sub-ideas. Reverting to idea.")
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
        print(f"[intake] Subdivision produced cyclic DAG: {cycle_errors}. Reverting to idea.")
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

    print(f"[intake] Task '{task.id}' subdivided into {len(child_ids)} sub-ideas (generation {generation}).")


def _handle_self_healing_rejection(task, result, llm_base_url, llm_model, max_context, loop):
    """Handle rejection of a system-generated sub-idea: retry subdivision if budget allows."""
    from app.agent.config import SUBDIVISION_MAX_RETRIES
    from app.agent.dag import DAGResolver

    parent_task = get_task(task.parent_task_id)
    if not parent_task:
        print(f"[intake] Parent task '{task.parent_task_id}' not found. Cannot self-heal.")
        return

    # Find the current active subdivision record
    records = get_subdivision_records(task.parent_task_id)
    active_record = next((r for r in records if r.status == "active"), None)
    if not active_record:
        print(f"[intake] No active subdivision record for parent '{task.parent_task_id}'.")
        return

    attempt = active_record.attempt_number
    if attempt >= SUBDIVISION_MAX_RETRIES:
        print(f"[intake] Subdivision retries exhausted ({attempt}/{SUBDIVISION_MAX_RETRIES}) "
              f"for parent '{task.parent_task_id}'. Reverting parent to idea.")
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
        print(f"[intake] Retry subdivision returned low confidence. Reverting parent to idea.")
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
        print(f"[intake] Retry subdivision produced cyclic DAG. Reverting parent to idea.")
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

    print(f"[intake] Self-healing: re-subdivided '{parent_task.id}' into {len(child_ids)} sub-ideas "
          f"(attempt {attempt + 1}).")


def _run_intake_pipeline(task_id: str) -> None:
    """Background runner for the intake pipeline."""
    try:
        import asyncio
        from app.agent.intake import run_intake_pipeline

        task = get_task(task_id)
        if not task:
            print(f"[intake] Task '{task_id}' not found.")
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        if llm_base_url:
            print(f"[intake] Using LLM: {llm_base_url} model={llm_model}")

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
                    llm_id=task.llm_id,
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                )
            )

            # Store the result
            _store_pipeline_result(task_id, result, task.budget_id)

            # Act on the result
            if result["outcome"] == "passed":
                update_task(task_id, type="planning")
                print(f"[intake] Task '{task_id}' advanced to PLANNING.")

            elif result["outcome"] == "subdivide":
                _handle_subdivision_outcome(
                    task, result, llm_base_url, llm_model, max_context, loop
                )

            elif result["outcome"] in ("rejected", "failed"):
                # Check if this is a system-generated sub-idea that should self-heal
                if task.parent_task_id:
                    print(f"[intake] System-generated task '{task_id}' rejected. Triggering self-healing.")
                    _handle_self_healing_rejection(
                        task, result, llm_base_url, llm_model, max_context, loop
                    )
                else:
                    print(f"[intake] Task '{task_id}' pipeline result: {result['outcome']}")

            else:
                print(f"[intake] Task '{task_id}' pipeline result: {result['outcome']}")

        finally:
            loop.close()
    except Exception as exc:
        import traceback
        print(f"[intake] Pipeline for '{task_id}' failed: {exc}")
        traceback.print_exc()


def _run_planning_pipeline_bg(task_id: str) -> None:
    """Background runner for the planning pipeline."""
    try:
        import asyncio
        from app.agent.planning import run_planning_pipeline
        from app.agent.planning_gate import run_planning_gate

        task = get_task(task_id)
        if not task:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        all_tasks = [task_to_dict(t) for t in get_all_tasks()]

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Run planning pipeline
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
                )
            )

            # Store transition result
            _store_pipeline_result_generic(task_id, result, task.budget_id, "planning_to_indev")

            if result.get("outcome") == "passed":
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
                    )
                )
                if gate_result.get("passed"):
                    update_task(task_id, type="indev")
                    print(f"[planning] Task '{task_id}' advanced to IN DEV.")
                else:
                    print(f"[planning] Task '{task_id}' failed planning gate.")
            else:
                print(f"[planning] Task '{task_id}' planning result: {result.get('outcome')}")
        finally:
            loop.close()
    except Exception as exc:
        import traceback
        print(f"[planning] Pipeline for '{task_id}' failed: {exc}")
        traceback.print_exc()


def _run_dev_orchestrator_bg(task_id: str) -> None:
    """Background runner for the development orchestrator."""
    try:
        import asyncio
        from app.agent.dev_orchestrator import run_dev_orchestrator

        task = get_task(task_id)
        if not task:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)
        planning_result_obj = get_planning_result(task_id)

        if not planning_result_obj:
            print(f"[indev] No planning result for task '{task_id}'.")
            return

        # Reconstruct planning result dict
        planning_result = {
            "implementation_steps": json.loads(planning_result_obj.implementation_steps or "[]"),
            "file_manifest": json.loads(planning_result_obj.file_manifest or "[]"),
            "dependency_graph": json.loads(planning_result_obj.dependency_graph or "{}"),
            "interface_contracts": json.loads(planning_result_obj.interface_contracts or "[]"),
            "test_strategy": json.loads(planning_result_obj.test_strategy or "[]"),
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
                )
            )

            if result.get("status") == "ACCEPTED":
                update_task(task_id, type="conceptual_review")
                print(f"[indev] Task '{task_id}' advanced to CONCEPTUAL REVIEW.")
            else:
                update_task(task_id, type="planning")
                print(f"[indev] Task '{task_id}' reverted to PLANNING: {result.get('error_detail')}")
        finally:
            loop.close()
    except Exception as exc:
        import traceback
        print(f"[indev] Orchestrator for '{task_id}' failed: {exc}")
        traceback.print_exc()


def _advance_to_optimization(task_id: str) -> None:
    """Auto-advance from conceptual review to optimization."""
    try:
        import asyncio
        from app.agent.conceptual_review import run_conceptual_review

        task = get_task(task_id)
        if not task:
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
                )
            )
            _store_pipeline_result_generic(task_id, result, task.budget_id, "conceptual_to_optimization")

            if result.get("outcome") == "passed":
                update_task(task_id, type="optimization")
                print(f"[review] Task '{task_id}' advanced to OPTIMIZATION.")
            else:
                update_task(task_id, type="indev")
                print(f"[review] Task '{task_id}' demoted to IN DEV.")
        finally:
            loop.close()
    except Exception as exc:
        print(f"[review] Pipeline for '{task_id}' failed: {exc}")


def _run_security_pipeline_bg(task_id: str) -> None:
    """Background runner for security + optimization pipelines."""
    try:
        import asyncio
        from app.agent.optimization import run_optimization_pipeline
        from app.agent.security_review import run_security_pipeline

        task = get_task(task_id)
        if not task:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            # Run optimization first
            opt_result = loop.run_until_complete(
                run_optimization_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                )
            )
            print(f"[optimization] Task '{task_id}': {opt_result.get('outcome')}")

            # Then run security review
            sec_result = loop.run_until_complete(
                run_security_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                )
            )
            _store_pipeline_result_generic(task_id, sec_result, task.budget_id, "security_review")

            if sec_result.get("outcome") == "passed":
                update_task(task_id, type="security")
                # Auto-advance past security to full_review
                update_task(task_id, type="full_review")
                print(f"[security] Task '{task_id}' advanced to FULL REVIEW.")
            else:
                demotion = sec_result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "security", demotion, sec_result.get("summary", ""))
                print(f"[security] Task '{task_id}' demoted to {demotion}.")
        finally:
            loop.close()
    except Exception as exc:
        print(f"[security] Pipeline for '{task_id}' failed: {exc}")


def _run_full_review_bg(task_id: str) -> None:
    """Background runner for full review pipeline."""
    try:
        import asyncio
        from app.agent.full_review import run_full_review_pipeline

        task = get_task(task_id)
        if not task:
            return

        llm_base_url, llm_model, max_context = _resolve_llm_endpoint(task)

        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(
                run_full_review_pipeline(
                    task_id=task_id,
                    task_description=task.description or "",
                    llm_base_url=llm_base_url,
                    llm_model=llm_model,
                    llm_id=task.llm_id,
                    budget_id=task.budget_id,
                )
            )
            _store_pipeline_result_generic(task_id, result, task.budget_id, "full_review")

            if result.get("outcome") == "passed":
                # Auto-merge
                _execute_merge_bg(task_id)
            else:
                demotion = result.get("demotion_target", "indev")
                update_task(task_id, type=demotion)
                _record_demotion(task_id, "full_review", demotion, result.get("summary", ""))
                print(f"[full_review] Task '{task_id}' demoted to {demotion}.")
        finally:
            loop.close()
    except Exception as exc:
        print(f"[full_review] Pipeline for '{task_id}' failed: {exc}")


def _execute_merge_bg(task_id: str) -> None:
    """Background runner for merge to main."""
    try:
        from app.agent.merge import execute_merge

        result = execute_merge(task_id)

        if result.status == "merged":
            print(f"[merge] Task '{task_id}' merged to main ({result.merge_commit_sha}).")
            _check_completion_rollup(task_id)
        elif result.status == "conflict":
            update_task(task_id, type="indev")
            _record_demotion(task_id, "merge", "indev", result.error_detail or "Merge conflict")
            print(f"[merge] Task '{task_id}' merge conflict. Demoted to IN DEV.")
        elif result.status == "test_failure":
            update_task(task_id, type="indev")
            _record_demotion(task_id, "merge", "indev", result.error_detail or "Tests failed")
            print(f"[merge] Task '{task_id}' tests failed after merge. Demoted to IN DEV.")
        else:
            print(f"[merge] Task '{task_id}' merge error: {result.error_detail}")
    except Exception as exc:
        print(f"[merge] Merge for '{task_id}' failed: {exc}")


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
    from datetime import datetime
    task = get_task(task_id)
    if not task:
        return
    history = task.demotion_history or []
    history.append({
        "from": from_stage,
        "to": to_stage,
        "reason": reason[:500],
        "timestamp": datetime.utcnow().isoformat(),
    })
    update_task(task_id, demotion_count=(task.demotion_count or 0) + 1, demotion_history=history)


# Pipeline handler dispatch table
ADVANCE_HANDLERS = {
    "idea": "_run_intake_pipeline",
    "planning": "_run_planning_pipeline_bg",
    "indev": "_run_dev_orchestrator_bg",
    "conceptual_review": "_advance_to_optimization",
    "optimization": "_run_security_pipeline_bg",
    "security": "_run_full_review_bg",
    "full_review": "_execute_merge_bg",
}


@app.post("/api/tasks/{task_id}/advance", response_model=dict)
def advance_task(task_id: str, background_tasks: BackgroundTasks):
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

    handler_name = ADVANCE_HANDLERS[current_type]

    # Dispatch to appropriate handler
    if handler_name == "_run_intake_pipeline":
        background_tasks.add_task(_run_intake_pipeline, task_id)
    elif handler_name == "_run_planning_pipeline_bg":
        background_tasks.add_task(_run_planning_pipeline_bg, task_id)
    elif handler_name == "_run_dev_orchestrator_bg":
        background_tasks.add_task(_run_dev_orchestrator_bg, task_id)
    elif handler_name == "_advance_to_optimization":
        background_tasks.add_task(_advance_to_optimization, task_id)
    elif handler_name == "_run_security_pipeline_bg":
        background_tasks.add_task(_run_security_pipeline_bg, task_id)
    elif handler_name == "_run_full_review_bg":
        background_tasks.add_task(_run_full_review_bg, task_id)
    elif handler_name == "_execute_merge_bg":
        background_tasks.add_task(_execute_merge_bg, task_id)

    return {
        "task_id": task_id,
        "status": "PIPELINE_STARTED",
        "message": f"Pipeline started for task '{task_id}' (from {current_type}). Poll /api/tasks/{task_id}/transition-status for updates."
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


@app.get("/api/tasks/{task_id}/planning-result", response_model=dict)
def get_task_planning_result(task_id: str):
    """Get the planning result for a task."""
    result = get_planning_result(task_id)
    if not result:
        raise HTTPException(status_code=404, detail="No planning result found")
    return {
        "id": result.id, "task_id": result.task_id,
        "file_manifest": json.loads(result.file_manifest) if result.file_manifest else None,
        "dependency_graph": json.loads(result.dependency_graph) if result.dependency_graph else None,
        "implementation_steps": json.loads(result.implementation_steps) if result.implementation_steps else None,
        "confidence": result.confidence,
        "selected_design_index": result.selected_design_index,
        "status": result.status,
        "created_at": result.created_at.isoformat() if result.created_at else None,
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


@app.get("/api/tasks/{task_id}/full-review-status", response_model=list)
def get_task_full_review_status(task_id: str):
    """Get full review findings for a task."""
    results = get_full_review_results(task_id)
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
        "full_reviews": [],
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

    # Full reviews
    fr_reviews = get_full_review_results(task_id)
    for f in fr_reviews:
        trail["full_reviews"].append({
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
        print(f"[rollup] All children of '{parent.id}' completed. Parent marked completed.")
        # Recurse upward
        _check_completion_rollup(parent.id)


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
        "parent_task_id": getattr(task, "parent_task_id", None),
        "subdivision_generation": getattr(task, "subdivision_generation", 0) or 0,
        "is_big_idea": bool(getattr(task, "is_big_idea", False)),
        "interface_contracts": json.loads(task.interface_contracts) if getattr(task, "interface_contracts", None) else None,
        "review_notes": getattr(task, "review_notes", None),
        "demotion_count": getattr(task, "demotion_count", 0) or 0,
        "demotion_history": getattr(task, "demotion_history", None),
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
        "parallel_sessions": llm.parallel_sessions,
        "max_context": llm.max_context,
        "notes": llm.notes,
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
    )
    if not llm:
        raise HTTPException(status_code=409, detail="LLM with this address/port/model already exists")
    return llm_to_dict(llm)


@app.put("/api/llms/{llm_id}", response_model=dict)
def update_existing_llm(llm_id: int, data: dict):
    allowed = ['address', 'port', 'model', 'settings', 'parallel_sessions', 'max_context', 'notes']
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
# Budget Entry API Endpoints (usage tracking)
# ============================================

def budget_entry_to_dict(entry):
    return {
        "id": entry.id,
        "llm_id": entry.llm_id,
        "budget_id": entry.budget_id,
        "task_id": entry.task_id,
        "prompt_cost": entry.prompt_cost,
        "generation_cost": entry.generation_cost,
        "tool_calls": entry.tool_calls,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


@app.get("/api/budget-entries", response_model=List[dict])
def list_budget_entries(budget_id: int = None, llm_id: int = None, task_id: str = None,
                        limit: int = 100, offset: int = 0):
    """List budget entries with optional filters."""
    entries = get_budget_entries(budget_id=budget_id, llm_id=llm_id, task_id=task_id,
                                limit=limit, offset=offset)
    return [budget_entry_to_dict(e) for e in entries]


@app.get("/api/budget-entries/{entry_id}/full", response_model=dict)
def read_budget_entry_full(entry_id: int):
    """Get a single budget entry including full prompt/response payloads."""
    from database import SessionLocal, BudgetEntry as BE
    db = SessionLocal()
    try:
        entry = db.query(BE).filter(BE.id == entry_id).first()
        if not entry:
            raise HTTPException(status_code=404, detail="Budget entry not found")
        result = budget_entry_to_dict(entry)
        result["prompt_data"] = json.loads(entry.prompt_data) if entry.prompt_data else None
        result["response_data"] = json.loads(entry.response_data) if entry.response_data else None
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
            "read_file", "read_file_lines", "count_lines",
            "search_files", "find_files", "list_directory",
            "git_status", "git_diff", "git_log", "git_blame", "git_show",
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
            "read_file", "read_file_lines", "count_lines",
            "search_files", "find_files", "list_directory",
            "git_status", "git_diff", "git_log", "git_blame", "git_show",
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
        "tools": ["run_shell_security", "read_file", "read_file_lines", "search_files", "find_files", "list_directory"],
    },
    "FullReviewPipeline": {
        "description": "4-agent final review (functional, code quality, integration, UX). Uses allowlisted review runner shell.",
        "tools": ["run_shell_review", "read_file", "read_file_lines", "search_files", "find_files", "list_directory"],
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
                print(f"[agent] Using LLM: {llm_base_url} model={llm_model} ctx={max_context}")

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
            )
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


@app.get("/api/scheduler/status", response_model=dict)
def scheduler_status():
    """Return the current state of the push-first eager scheduler."""
    from app.agent.scheduler import get_scheduler_status  # noqa: PLC0415
    return get_scheduler_status()
