"""
CRUD operations for Task and related helpers.

Covers:
  - Task lifecycle: create, get, update, delete, reorder
  - Task history: get_task_history, append_task_history
  - Subdivision tree traversal: get_child_tasks, get_active_child_tasks,
    count_total_sub_ideas, get_descendant_tree
  - Big-idea flag: set_big_idea_flag
  - Project-scoped task query: get_tasks_by_project
  - DB initialisation and seeding: init_db, seed_sample_tasks,
    seed_task, seed_sample_tasks_raw
"""

from datetime import datetime, timezone
import json
import logging

from .session import SessionLocal, init_db_tables
from .models import Task, LLM, Budget, PerformanceImprovementPlan, PipVerification, PipResolutionJob

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PIP CRUD
# ---------------------------------------------------------------------------

def create_pip(task_id, origin_stage, requirements, llm_id=None, budget_id=None,
               prompt_tokens=0, completion_tokens=0, created_at_commit="none"):
    """Create a new Performance Improvement Plan for a task."""
    db = SessionLocal()
    try:
        pip = PerformanceImprovementPlan(
            task_id=task_id,
            origin_stage=origin_stage,
            requirements=requirements,
            llm_id=llm_id,
            budget_id=budget_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            created_at_commit=created_at_commit,
            status='active',
        )
        db.add(pip)
        db.commit()
        db.refresh(pip)
        return pip
    except Exception as e:
        db.rollback()
        logger.error("Error creating PIP: %s", e)
        return None
    finally:
        db.close()


def get_pips_for_task(task_id):
    """Get all PIPs for a specific task, newest first."""
    db = SessionLocal()
    try:
        return (db.query(PerformanceImprovementPlan)
                  .filter(PerformanceImprovementPlan.task_id == task_id)
                  .order_by(PerformanceImprovementPlan.created_at.desc())
                  .all())
    except Exception as e:
        logger.error("Error getting PIPs for task %s: %s", task_id, e)
        return []
    finally:
        db.close()


def satisfy_pips(task_id):
    """Mark all active PIPs for a task as satisfied (legacy — prefer verification rows)."""
    db = SessionLocal()
    try:
        (db.query(PerformanceImprovementPlan)
           .filter(PerformanceImprovementPlan.task_id == task_id,
                   PerformanceImprovementPlan.status == 'active')
           .update({"status": "satisfied", "verified_at": datetime.utcnow()},
                   synchronize_session=False))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error satisfying PIPs for task %s: %s", task_id, e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PipVerification CRUD
# ---------------------------------------------------------------------------

def create_pip_verification(pip_id, task_id, stage, outcome, summary,
                             findings=None, agent_session_id=None):
    """Record one pre-flight gate result for a PIP at a given pipeline stage."""
    db = SessionLocal()
    try:
        v = PipVerification(
            pip_id=pip_id,
            task_id=task_id,
            checked_at_stage=stage,
            outcome=outcome,
            summary=summary,
            findings=findings,
            agent_session_id=agent_session_id,
            created_at=datetime.utcnow().isoformat(),
        )
        db.add(v)
        db.commit()
        db.refresh(v)
        return v
    except Exception as e:
        db.rollback()
        logger.error("Error creating PipVerification pip=%s stage=%s: %s", pip_id, stage, e)
        return None
    finally:
        db.close()


def get_latest_pip_verification(pip_id, stage):
    """Return the most recent verification row for a PIP at a specific stage, or None."""
    db = SessionLocal()
    try:
        return (db.query(PipVerification)
                  .filter(PipVerification.pip_id == pip_id,
                          PipVerification.checked_at_stage == stage)
                  .order_by(PipVerification.created_at.desc())
                  .first())
    except Exception as e:
        logger.error("Error fetching latest PipVerification pip=%s stage=%s: %s", pip_id, stage, e)
        return None
    finally:
        db.close()


def get_pip_verification_map(task_id, stage):
    """Return {pip_id: outcome} for the most recent verification of each PIP at stage."""
    pips = get_pips_for_task(task_id)
    result = {}
    for pip in pips:
        v = get_latest_pip_verification(pip.id, stage)
        result[pip.id] = v.outcome if v else None
    return result


def get_pip_verifications_for_pip(pip_id):
    """Return full verification history for one PIP across all stages, newest first."""
    db = SessionLocal()
    try:
        return (db.query(PipVerification)
                  .filter(PipVerification.pip_id == pip_id)
                  .order_by(PipVerification.created_at.desc())
                  .all())
    except Exception as e:
        logger.error("Error fetching PipVerifications for pip=%s: %s", pip_id, e)
        return []
    finally:
        db.close()


def pip_status_at_stage(pip_id, stage):
    """Derive the display status of a PIP at a given pipeline stage.

    Status is derived at read time from the latest verification row — there is
    no stored status column that needs manual updating.

    Returns one of: 'unverified' | 'satisfied' | 'unsatisfied' | 'checking'
    """
    v = get_latest_pip_verification(pip_id, stage)
    if v is None:
        return "unverified"
    if v.outcome == "passed":
        return "satisfied"
    if v.outcome == "failed":
        return "unsatisfied"
    if v.outcome == "pending":
        return "checking"
    return "unverified"


# ---------------------------------------------------------------------------
# PipResolutionJob CRUD
# ---------------------------------------------------------------------------

_PIP_RESOLUTION_ACTIVE_STATUSES = frozenset({"pending", "researching", "resolving"})


def create_pip_resolution_job(task_id, pip_id, stage_blocked_at):
    """Create a new PIP resolution job record. Returns None if an active job already
    exists for this pip (idempotent — prevents duplicates on consecutive pre-flight failures)."""
    db = SessionLocal()
    try:
        existing = (db.query(PipResolutionJob)
                      .filter(PipResolutionJob.pip_id == pip_id,
                              PipResolutionJob.status.in_(list(_PIP_RESOLUTION_ACTIVE_STATUSES)))
                      .first())
        if existing:
            return existing
        job = PipResolutionJob(
            task_id=task_id,
            pip_id=pip_id,
            stage_blocked_at=stage_blocked_at,
            status="pending",
            created_at=datetime.utcnow().isoformat(),
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error creating PipResolutionJob pip=%s: %s", pip_id, e)
        return None
    finally:
        db.close()


def get_pending_pip_resolution_jobs(limit=20):
    """Return active (pending/researching/resolving) resolution jobs, oldest first."""
    db = SessionLocal()
    try:
        return (db.query(PipResolutionJob)
                  .filter(PipResolutionJob.status.in_(list(_PIP_RESOLUTION_ACTIVE_STATUSES)))
                  .order_by(PipResolutionJob.created_at)
                  .limit(limit)
                  .all())
    except Exception as e:
        logger.error("Error fetching pending PipResolutionJobs: %s", e)
        return []
    finally:
        db.close()


def get_active_pip_resolution_jobs_for_task(task_id):
    """Return any active (not done/failed) resolution jobs for a task.

    Used by the scheduler to block stage re-dispatch while resolution is in progress.
    """
    db = SessionLocal()
    try:
        return (db.query(PipResolutionJob)
                  .filter(PipResolutionJob.task_id == task_id,
                          PipResolutionJob.status.in_(list(_PIP_RESOLUTION_ACTIVE_STATUSES)))
                  .all())
    except Exception as e:
        logger.error("Error fetching active PipResolutionJobs for task %s: %s", task_id, e)
        return []
    finally:
        db.close()


def update_pip_resolution_job(job_id, **kwargs):
    """Update fields on a PipResolutionJob row."""
    db = SessionLocal()
    try:
        (db.query(PipResolutionJob)
           .filter(PipResolutionJob.id == job_id)
           .update(kwargs, synchronize_session=False))
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error updating PipResolutionJob %s: %s", job_id, e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# DB initialisation (lives here — not session.py — because it queries Task)
# ---------------------------------------------------------------------------

def init_db():
    """
    Check if database is fresh (new or empty).
    Returns True if database is fresh (new or empty), False if it has data.
    """
    import os
    from .session import DATABASE_PATH

    should_seed = False

    # Check if file exists first
    if not os.path.exists(DATABASE_PATH):
        logger.debug("Database file not found, database is fresh (should_seed=True)")
        should_seed = True

    # Check if tables exist and are empty
    db = SessionLocal()
    try:
        try:
            existing_count = db.query(Task).count()
            if existing_count == 0:
                logger.debug("Database exists and is empty, tables are fresh (should_seed=True)")
                should_seed = True
            else:
                logger.debug("Database has %d tasks, not fresh (should_seed=False)", existing_count)
        except Exception as e:
            # Table doesn't exist — database is corrupted/empty
            logger.debug("Database file exists but tables are missing (corrupted), treating as fresh: %s", e)
            should_seed = True
    finally:
        db.close()

    return should_seed


def seed_sample_tasks():
    """
    Seed the database with sample tasks ONLY if the database is fresh.
    Uses init_db() to check if database needs seeding.
    """
    is_fresh = init_db()

    if not is_fresh:
        logger.debug("Database not fresh, skipping seed.")
        return False

    # Create tables for fresh database
    init_db_tables()

    logger.info("Seeding sample tasks...")
    db = SessionLocal()
    try:
        # Ensure default LLM and Budget exist
        default_llm = db.query(LLM).filter_by(address='localhost', port=8008, model='Qwen3p5-Omnicoder-9B').first()
        if not default_llm:
            default_llm = LLM(address='localhost', port=8008, model='Qwen3p5-Omnicoder-9B')
            db.add(default_llm)
            db.commit()
            db.refresh(default_llm)

        default_budget = db.query(Budget).filter_by(name='Default Budget').first()
        if not default_budget:
            default_budget = Budget(name='Default Budget')
            db.add(default_budget)
            db.commit()
            db.refresh(default_budget)

        lid = default_llm.id
        bid = default_budget.id

        # Architecture tasks (immutable)
        seed_task(db, "arch-1", "Project Stack", "architecture", "Core technology stack for TheMaestro", "user",
                  ["core", "infrastructure"], {"frontend": "HTML/CSS/JS", "backend": "FastAPI + Uvicorn", "database": "SQLite (development)", "style": "Bootstrap CSS"}, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "arch-2", "Code Structure", "architecture", "Organizational structure of the codebase", "user",
                  ["core", "structure"], {"dags": "dags.py", "config": "config.py", "repl": "repl.py", "tests": "test_*.py"}, llm_id=lid, budget_id=bid, position=1)

        # Planning tasks
        seed_task(db, "planning-1", "Setup FastAPI development environment", "planning", "Configure Python virtual environment and install dependencies", "user",
                  ["backend", "setup"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "planning-2", "Create Kanban board UI mockup", "planning", "Design wireframes for the Kanban board interface", "user",
                  ["frontend", "design"], None, llm_id=lid, budget_id=bid, position=1)

        seed_task(db, "planning-3", "Implement drag-and-drop", "planning", "Add drag-and-drop functionality for task reordering", "user",
                  ["feature", "frontend"], None, llm_id=lid, budget_id=bid, position=2)

        # In Progress tasks
        seed_task(db, "dev-1", "Configure venv and install dependencies", "indev", "Set up Python 3.13 virtual environment", "user",
                  ["setup", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "dev-2", "Create app structure and main.py", "indev", "Set up FastAPI application with main entry point", "user",
                  ["structure", "backend"], None, llm_id=lid, budget_id=bid, position=1)

        # In Review tasks
        seed_task(db, "review-1", "Review requirements.txt", "conceptual_review", "Verify all dependencies are properly listed", "user",
                  ["qa", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        # Completed tasks
        seed_task(db, "completed-1", "Initialize Git repository", "completed", "Create .gitignore and initial commit", "user",
                  ["setup", "devops"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "completed-2", "Create database schema", "completed", "Define SQLAlchemy models for tasks", "user",
                  ["database", "backend"], None, llm_id=lid, budget_id=bid, position=1)

        logger.info("Successfully seeded 10 sample tasks!")
        return True

    except Exception as e:
        db.rollback()
        logger.error("Error seeding tasks: %s", e)
        return False
    finally:
        db.close()


def seed_task(db, id, title, task_type, description="", owner="user", tags=None, content=None, llm_id=None, budget_id=None, position=0, project='TheMaestro'):
    """
    Helper function to create a sample task for seeding.
    Accepts database session as first parameter.
    """
    try:
        task = Task(
            id=id,
            title=title,
            type=task_type,
            description=description,
            owner=owner,
            tags=tags or [],
            content=content,
            llm_id=llm_id,
            budget_id=budget_id,
            history=[{"status": "created", "timestamp": datetime.now().isoformat()}],
            position=position,
            project=project
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        logger.debug("  Created sample task: %s - %s", id, title)
        return task
    except Exception as e:
        db.rollback()
        logger.error("  Error creating sample task %s: %s", id, e)
        return None


def seed_sample_tasks_raw(conn):
    """
    Seed the 10 canonical sample tasks using a raw sqlite3 connection.
    Called by the migration runner's reset command — no SQLAlchemy required.
    conn must already be connected to kanban.db with all migrations applied.
    """
    import json
    now = datetime.now(timezone.utc).isoformat()
    history = json.dumps([{"status": "created", "timestamp": now}])

    # Ensure default LLM and Budget rows exist
    conn.execute(
        "INSERT OR IGNORE INTO llms (address, port, model) VALUES ('localhost', 8008, 'Qwen3p5-Omnicoder-9B')"
    )
    conn.execute(
        "INSERT OR IGNORE INTO budgets (name) VALUES ('Default Budget')"
    )
    conn.commit()

    llm_id = conn.execute(
        "SELECT id FROM llms WHERE address='localhost' AND port=8008 AND model='Qwen3p5-Omnicoder-9B'"
    ).fetchone()[0]
    budget_id = conn.execute(
        "SELECT id FROM budgets WHERE name='Default Budget'"
    ).fetchone()[0]

    tasks = [
        ("arch-1",      "Project Stack",                          "architecture", "Core technology stack for TheMaestro",                          "user", json.dumps(["core", "infrastructure"]), json.dumps({"frontend": "HTML/CSS/JS", "backend": "FastAPI + Uvicorn", "database": "SQLite (development)", "style": "Bootstrap CSS"}), history, 0),
        ("arch-2",      "Code Structure",                         "architecture", "Organizational structure of the codebase",                       "user", json.dumps(["core", "structure"]),        json.dumps({"dags": "dags.py", "config": "config.py", "repl": "repl.py", "tests": "test_*.py"}), history, 1),
        ("planning-1",  "Setup FastAPI development environment",  "planning",     "Configure Python virtual environment and install dependencies",   "user", json.dumps(["backend", "setup"]),         None, history, 0),
        ("planning-2",  "Create Kanban board UI mockup",          "planning",     "Design wireframes for the Kanban board interface",                "user", json.dumps(["frontend", "design"]),       None, history, 1),
        ("planning-3",  "Implement drag-and-drop",                "planning",     "Add drag-and-drop functionality for task reordering",             "user", json.dumps(["feature", "frontend"]),      None, history, 2),
        ("dev-1",       "Configure venv and install dependencies","indev",        "Set up Python 3.13 virtual environment",                          "user", json.dumps(["setup", "backend"]),         None, history, 0),
        ("dev-2",       "Create app structure and main.py",       "indev",        "Set up FastAPI application with main entry point",                "user", json.dumps(["structure", "backend"]),     None, history, 1),
        ("review-1",    "Review requirements.txt",                "conceptual_review", "Verify all dependencies are properly listed",               "user", json.dumps(["qa", "backend"]),             None, history, 0),
        ("completed-1", "Initialize Git repository",              "completed",    "Create .gitignore and initial commit",                            "user", json.dumps(["setup", "devops"]),           None, history, 0),
        ("completed-2", "Create database schema",                 "completed",    "Define SQLAlchemy models for tasks",                              "user", json.dumps(["database", "backend"]),       None, history, 1),
    ]

    for t in tasks:
        conn.execute(
            """
            INSERT OR REPLACE INTO tasks
                (id, title, type, description, owner, tags, content, history, position,
                 created_at, updated_at, prerequisites, project, llm_id, budget_id)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*t, now, now, json.dumps([]), 'TheMaestro', llm_id, budget_id),
        )
        logger.debug("  Seeded task: %s - %s", t[0], t[1])
    conn.commit()
    logger.info("Successfully seeded 10 sample tasks (raw)!")


# ---------------------------------------------------------------------------
# Task CRUD
# ---------------------------------------------------------------------------

def create_task(title, task_type, description="", owner="user", tags=None, content=None, llm_id=None, budget_id=None, prerequisites=None, project='TheMaestro', position=None):
    """Create a new task."""
    db = SessionLocal()
    try:
        task = Task(
            id=f"task-{datetime.now().timestamp()}",
            title=title,
            type=task_type,
            description=description,
            owner=owner,
            tags=tags or [],
            content=content,
            llm_id=llm_id,
            budget_id=budget_id,
            prerequisites=prerequisites or [],
            project=project,
            position=position,
            history=[{"status": "created", "timestamp": datetime.now().isoformat()}]
        )
        db.add(task)
        db.commit()
        db.refresh(task)
        return task
    except Exception as e:
        db.rollback()
        logger.error("Error creating task: %s", e)
        return None
    finally:
        db.close()


def get_task(task_id):
    """Get a task by ID."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        return task
    except Exception as e:
        logger.error("Error getting task: %s", e)
        return None
    finally:
        db.close()


def get_tasks_by_type(task_type):
    """Get all active tasks of a specific type, ordered by position then created_at."""
    db = SessionLocal()
    try:
        tasks = (db.query(Task)
                 .filter(Task.type == task_type, Task.is_active == True)
                 .order_by(Task.position, Task.created_at).all())
        return tasks
    except Exception as e:
        logger.error("Error getting tasks by type: %s", e)
        return []
    finally:
        db.close()


def get_tasks_by_project(project_name):
    """Get all active tasks belonging to a specific project, ordered by position then created_at."""
    db = SessionLocal()
    try:
        tasks = (db.query(Task)
                 .filter(Task.project == project_name, Task.is_active == True)
                 .order_by(Task.position, Task.created_at).all())
        return tasks
    except Exception as e:
        logger.error("Error getting tasks by project: %s", e)
        return []
    finally:
        db.close()


def get_all_tasks():
    """Get all active tasks."""
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.is_active == True).order_by(Task.created_at).all()
        return tasks
    except Exception as e:
        logger.error("Error getting all tasks: %s", e)
        return []
    finally:
        db.close()


def update_task(task_id, **kwargs):
    """Update a task with provided fields."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None

        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)

        task.history.append({
            "status": kwargs.get('type') or 'updated',
            "timestamp": datetime.now().isoformat()
        })

        db.commit()
        db.refresh(task)
        return task
    except Exception as e:
        db.rollback()
        logger.error("Error updating task: %s", e)
        return None
    finally:
        db.close()


def batch_update_map_positions(updates):
    """
    Bulk-save map_x / map_y for a list of tasks without touching task history.

    updates: iterable of dicts, each with keys: id, map_x, map_y
    Returns the number of rows updated.
    """
    db = SessionLocal()
    try:
        count = 0
        for u in updates:
            task = db.query(Task).filter(Task.id == u['id']).first()
            if task:
                task.map_x = float(u['map_x'])
                task.map_y = float(u['map_y'])
                count += 1
        db.commit()
        return count
    except Exception as e:
        db.rollback()
        logger.error("Error batch-updating map positions: %s", e)
        return 0
    finally:
        db.close()


def delete_task(task_id):
    """Soft-delete a task and all its descendants by setting is_active=False.

    No rows are removed from the database.  All board queries filter on
    is_active=True, so deactivated tasks disappear from every view.
    Cascades to the full descendant tree (via parent_task_id) so deleting
    a Big Idea also hides its sub-ideas and their sub-ideas.
    Returns the number of tasks deactivated (>=1) or 0 if not found.
    """
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return 0

        # BFS over the parent_task_id tree to collect all descendants.
        ids_to_deactivate = [task_id]
        queue = [task_id]
        while queue:
            parent_id = queue.pop(0)
            children = (db.query(Task.id)
                        .filter(Task.parent_task_id == parent_id,
                                Task.is_active == True)
                        .all())
            for (child_id,) in children:
                ids_to_deactivate.append(child_id)
                queue.append(child_id)

        (db.query(Task)
           .filter(Task.id.in_(ids_to_deactivate))
           .update({"is_active": False}, synchronize_session=False))

        # Also cancel any background jobs associated with these tasks
        from .models import ResearchJob, FileSummaryJob
        (db.query(ResearchJob)
           .filter(ResearchJob.task_id.in_(ids_to_deactivate),
                   ResearchJob.status.in_(['pending', 'running']))
           .update({"status": "cancelled", "completed_at": datetime.now(timezone.utc)},
                   synchronize_session=False))
        (db.query(FileSummaryJob)
           .filter(FileSummaryJob.task_id.in_(ids_to_deactivate),
                   FileSummaryJob.status.in_(['pending', 'running']))
           .update({"status": "cancelled", "completed_at": datetime.now(timezone.utc)},
                   synchronize_session=False))

        db.commit()
        return len(ids_to_deactivate)
    except Exception as e:
        db.rollback()
        logger.error("Error soft-deleting task %s: %s", task_id, e)
        return 0
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task history
# ---------------------------------------------------------------------------

def get_task_history(task_id):
    """Get history for a specific task."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            return task.history
        return []
    except Exception as e:
        logger.error("Error getting task history: %s", e)
        return []
    finally:
        db.close()


def append_task_history(task_id, status, message=None):
    """Append a single history entry to a task without changing any other fields."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return
        entry = {"status": status, "timestamp": datetime.now().isoformat()}
        if message:
            entry["message"] = message
        history = list(task.history or [])
        history.append(entry)
        task.history = history
        db.commit()
    except Exception as e:
        db.rollback()
        logger.error("Error appending task history: %s", e)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Task reordering
# ---------------------------------------------------------------------------

def reorder_tasks(task_id, new_position, task_type):
    """
    Reorder a task within its column, or move it to a different column.
    new_position is the index where the task should be inserted.
    task_type is the destination column.
    """
    db = SessionLocal()
    try:
        task_to_move = db.query(Task).filter(Task.id == task_id).first()
        if not task_to_move:
            return False

        source_type = task_to_move.type
        is_cross_column = source_type != task_type

        project = task_to_move.project or 'TheMaestro'

        if is_cross_column:
            # Remove from source column and re-number it
            source_tasks = (
                db.query(Task)
                .filter(Task.type == source_type, Task.project == project, Task.id != task_id)
                .order_by(Task.position)
                .all()
            )
            for i, t in enumerate(source_tasks):
                t.position = i

            # Update the task's type to the destination column
            task_to_move.type = task_type

            # Insert into destination column
            dest_tasks = (
                db.query(Task)
                .filter(Task.type == task_type, Task.project == project, Task.id != task_id)
                .order_by(Task.position)
                .all()
            )
            new_position = max(0, min(new_position, len(dest_tasks)))
            dest_tasks.insert(new_position, task_to_move)
            for i, t in enumerate(dest_tasks):
                t.position = i
        else:
            # Same-column reorder
            tasks = db.query(Task).filter(Task.type == task_type, Task.project == project).order_by(Task.position).all()
            current_index = tasks.index(task_to_move)
            new_position = max(0, min(new_position, len(tasks) - 1))
            tasks.pop(current_index)
            tasks.insert(new_position, task_to_move)
            for i, t in enumerate(tasks):
                t.position = i

        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error reordering tasks: %s", e)
        return False
    finally:
        db.close()


def batch_reorder_tasks(moves):
    """Process multiple task reorders in a single transaction.
    moves: list of {'task_id': str, 'position': int, 'type': str}
    """
    db = SessionLocal()
    try:
        for move in moves:
            task = db.query(Task).filter(Task.id == move['task_id']).first()
            if task:
                task.position = move['position']
                if 'type' in move and move['type']:
                    task.type = move['type']
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error batch reordering tasks: %s", e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Big-idea / subdivision tree helpers
# ---------------------------------------------------------------------------

def set_big_idea_flag(task_id):
    """Set the is_big_idea flag on a task."""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.is_big_idea = True
            db.commit()
            return True
        return False
    except Exception as e:
        db.rollback()
        logger.error("Error setting big idea flag: %s", e)
        return False
    finally:
        db.close()


def get_child_tasks(parent_task_id):
    """Get all direct child tasks for a parent task."""
    db = SessionLocal()
    try:
        return (db.query(Task)
                .filter(Task.parent_task_id == parent_task_id)
                .order_by(Task.position, Task.created_at)
                .all())
    finally:
        db.close()


def get_active_child_tasks(parent_task_id):
    """Get non-cancelled child tasks for a parent."""
    db = SessionLocal()
    try:
        return (db.query(Task)
                .filter(Task.parent_task_id == parent_task_id)
                .filter(Task.type != 'cancelled')
                .order_by(Task.position, Task.created_at)
                .all())
    finally:
        db.close()


def count_total_sub_ideas(root_task_id):
    """Count all descendant tasks (at any depth) of a root task."""
    db = SessionLocal()
    try:
        count = 0
        queue = [root_task_id]
        while queue:
            parent_id = queue.pop(0)
            children = (db.query(Task)
                        .filter(Task.parent_task_id == parent_id)
                        .filter(Task.type != 'cancelled')
                        .all())
            count += len(children)
            for child in children:
                queue.append(child.id)
        return count
    finally:
        db.close()


def get_descendant_tree(root_task_id):
    """Return a flat list of all descendants with depth info.
    Each entry: {'id': ..., 'title': ..., 'type': ..., 'position': ..., 'depth': int, 'parent_task_id': ...}
    """
    db = SessionLocal()
    try:
        results = []
        queue = [(root_task_id, 0)]  # (parent_id, depth)
        while queue:
            parent_id, depth = queue.pop(0)
            children = (db.query(Task)
                        .filter(Task.parent_task_id == parent_id)
                        .order_by(Task.position, Task.created_at)
                        .all())
            for child in children:
                child_depth = depth + 1
                results.append({
                    'id': child.id,
                    'title': child.title,
                    'type': child.type,
                    'position': child.position,
                    'depth': child_depth,
                    'parent_task_id': child.parent_task_id,
                })
                queue.append((child.id, child_depth))
        return results
    finally:
        db.close()


def _pip_to_dict(pip, stage):
    """Serialize one PIP with its derived verification status for the given stage."""
    v = get_latest_pip_verification(pip.id, stage)
    if v is None:
        status = "unverified"
    elif v.outcome == "passed":
        status = "satisfied"
    elif v.outcome == "failed":
        status = "unsatisfied"
    elif v.outcome == "pending":
        status = "checking"
    else:
        status = "unverified"
    return {
        "id": pip.id,
        "origin_stage": pip.origin_stage,
        "requirements": json.loads(pip.requirements),
        "created_at": pip.created_at.isoformat() if pip.created_at else None,
        "created_at_commit": getattr(pip, "created_at_commit", "none"),
        "status": status,
        "last_summary": v.summary if v else None,
        "last_checked": v.created_at if v else None,
    }


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
        "map_x": getattr(task, "map_x", None),
        "map_y": getattr(task, "map_y", None),
        "is_active": bool(getattr(task, "is_active", True)),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "pips": [
            _pip_to_dict(p, task.type)
            for p in (get_pips_for_task(task.id) if hasattr(task, "id") else [])
        ]
    }
