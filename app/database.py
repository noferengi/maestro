"""
Kanban Board Database Layer
SQLite-based persistence for Kanban tasks
"""

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, ForeignKey, UniqueConstraint, Boolean, Float, event
from sqlalchemy.orm import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime, timezone
import logging
import os

logger = logging.getLogger(__name__)

# Database path - keep it in the project directory.
# MAESTRO_TEST_DB env var lets conftest.py redirect to a temp file per session.
DATABASE_PATH = (
    os.environ.get("MAESTRO_TEST_DB")
    or os.path.join(os.path.dirname(__file__), '..', 'data', 'kanban.db')
)

# Ensure data directory exists
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

# Create database engine
engine = create_engine(f"sqlite:///{DATABASE_PATH}", echo=False)


@event.listens_for(engine, "connect")
def set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


# Create session factory
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Base class for declarative models
Base = declarative_base()


class LLM(Base):
    """LLM endpoint configuration."""
    __tablename__ = "llms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String, nullable=False, default='localhost')
    port = Column(Integer, nullable=False, default=8008)
    model = Column(String, nullable=False, default='')
    settings = Column(JSON, nullable=True)
    parallel_sessions = Column(Integer, nullable=False, default=1)
    max_context = Column(Integer, nullable=False, default=4096)
    notes = Column(String, nullable=False, default='')

    __table_args__ = (
        UniqueConstraint('address', 'port', 'model', name='uq_llm_endpoint'),
    )

    @property
    def label(self):
        return f"{self.address}:{self.port} serving {self.model}"

    def __repr__(self):
        return f"<LLM(id={self.id}, {self.label})>"


class Budget(Base):
    """Budget configuration with extensible settings."""
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    settings = Column(JSON, nullable=True)

    def __repr__(self):
        return f"<Budget(id={self.id}, name='{self.name}')>"


class TransitionVote(Base):
    __tablename__ = "transition_votes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    transition = Column(String, nullable=False)
    stage = Column(String, nullable=False)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    raw_response = Column(JSON, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    model = Column(String, nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TransitionResult(Base):
    __tablename__ = "transition_results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    transition = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    vote_summary = Column(JSON, nullable=True)
    total_prompt_tokens = Column(Integer, nullable=True)
    total_completion_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BudgetEntry(Base):
    """Individual LLM call log entry for cost tracking and dataset building."""
    __tablename__ = "budget_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=True)
    prompt_cost = Column(Integer, nullable=False, default=0)        # total prompt tokens
    generation_cost = Column(Integer, nullable=False, default=0)    # total completion tokens
    tool_calls = Column(Integer, nullable=False, default=0)         # total LLM turns
    prompt_data = Column(Text, nullable=True)                       # full prompt messages (JSON)
    response_data = Column(Text, nullable=True)                     # full response (JSON)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BudgetEntry(id={self.id}, llm={self.llm_id}, budget={self.budget_id}, task={self.task_id}, prompt={self.prompt_cost}, gen={self.generation_cost})>"


class SubdivisionRecord(Base):
    """Audit trail for task subdivision attempts."""
    __tablename__ = "subdivision_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    generation = Column(Integer, nullable=False, default=1)
    child_task_ids = Column(JSON, nullable=False)
    rejection_context = Column(JSON, nullable=True)
    agent_vote = Column(JSON, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    interface_contracts = Column(Text, nullable=True)  # JSON: interface contracts from subdivision agent
    status = Column(String, nullable=False, default='active')  # active | superseded | failed
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SubdivisionRecord(id={self.id}, parent={self.parent_task_id}, attempt={self.attempt_number}, status={self.status})>"


class PlanningResult(Base):
    """Audit trail for planning pipeline results."""
    __tablename__ = "planning_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    file_manifest = Column(Text, nullable=True)
    dependency_graph = Column(Text, nullable=True)
    interface_contracts = Column(Text, nullable=True)
    test_strategy = Column(Text, nullable=True)
    implementation_steps = Column(Text, nullable=True)
    mermaid_diagrams = Column(Text, nullable=True)
    pitfalls_identified = Column(Text, nullable=True)
    review_votes = Column(Text, nullable=True)
    codebase_survey = Column(Text, nullable=True)
    best_of_n_designs = Column(Text, nullable=True)
    selected_design_index = Column(Integer, nullable=True)
    selection_justification = Column(Text, nullable=True)
    confidence = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    status = Column(String, nullable=False, default='active')
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PlanningResult(id={self.id}, task={self.task_id}, status={self.status})>"


class ComponentResult(Base):
    """Audit trail for component-level development agents."""
    __tablename__ = "component_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    component_name = Column(String, nullable=False)
    step_order = Column(Integer, nullable=False)
    batch_number = Column(Integer, nullable=False)
    status = Column(String, nullable=False, default='pending')
    files_changed = Column(Text, nullable=True)
    tests_passed = Column(Integer, default=0)
    turns_used = Column(Integer, default=0)
    error_detail = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ComponentResult(id={self.id}, task={self.task_id}, component={self.component_name})>"


class OptimizationResult(Base):
    """Audit trail for optimization pipeline."""
    __tablename__ = "optimization_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    baseline_report = Column(Text, nullable=True)
    proposals = Column(Text, nullable=True)
    judge_scores = Column(Text, nullable=True)
    winning_proposal_index = Column(Integer, nullable=True)
    winning_score = Column(Integer, nullable=True)  # stored as int to avoid Float import
    post_report = Column(Text, nullable=True)
    improvement_summary = Column(Text, nullable=True)
    outcome = Column(String, nullable=False)
    total_prompt_tokens = Column(Integer, default=0)
    total_completion_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<OptimizationResult(id={self.id}, task={self.task_id}, outcome={self.outcome})>"


class SecurityReviewResult(Base):
    """Security review findings with veto power."""
    __tablename__ = "security_review_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    reviewer_type = Column(String, nullable=False)
    owasp_findings = Column(Text, nullable=True)
    secrets_detected = Column(Text, nullable=True)
    dependency_vulnerabilities = Column(Text, nullable=True)
    data_flow_map = Column(Text, nullable=True)
    compliance_findings = Column(Text, nullable=True)
    optimization_regressions = Column(Text, nullable=True)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    critical_count = Column(Integer, default=0)
    high_count = Column(Integer, default=0)
    raw_response = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    model = Column(String, nullable=True)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SecurityReviewResult(id={self.id}, task={self.task_id}, type={self.reviewer_type})>"


class FullReviewResult(Base):
    """Full/final review findings."""
    __tablename__ = "full_review_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    reviewer_type = Column(String, nullable=False)
    test_results = Column(Text, nullable=True)
    quality_findings = Column(Text, nullable=True)
    requirements_mapping = Column(Text, nullable=True)
    integration_checks = Column(Text, nullable=True)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    model = Column(String, nullable=True)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FullReviewResult(id={self.id}, task={self.task_id}, type={self.reviewer_type})>"


class MergeRecord(Base):
    """Audit trail for merge-to-main operations."""
    __tablename__ = "merge_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    branch_name = Column(String, nullable=False)
    merge_commit_sha = Column(String, nullable=True)
    status = Column(String, nullable=False)
    test_output = Column(Text, nullable=True)
    error_detail = Column(Text, nullable=True)
    security_review_ids = Column(Text, nullable=True)
    full_review_ids = Column(Text, nullable=True)
    total_pipeline_tokens = Column(Integer, default=0)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MergeRecord(id={self.id}, task={self.task_id}, status={self.status})>"


class ResearchJob(Base):
    """Background research job — tracks inline and queued agent investigations."""
    __tablename__ = "research_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    parent_job_id = Column(Integer, ForeignKey('research_jobs.id'), nullable=True)
    question = Column(Text, nullable=False)
    context = Column(Text, nullable=True)           # JSON string
    status = Column(String, nullable=False, default='pending')
    priority = Column(Float, nullable=False, default=0.0)   # lower = higher priority
    depth = Column(Integer, nullable=False, default=0)
    verdict = Column(Text, nullable=True)           # JSON vote dict
    findings = Column(Text, nullable=True)
    lives_used = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ResearchJob(id={self.id}, task={self.task_id}, status={self.status})>"


class OptimizationBenchmark(Base):
    """Before/after profiling metrics for optimization sub-tasks."""
    __tablename__ = "optimization_benchmarks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    benchmark_type = Column(String, nullable=False)     # 'before' | 'after'
    metrics = Column(Text, nullable=False)              # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<OptimizationBenchmark(id={self.id}, task={self.task_id}, type={self.benchmark_type})>"


class Project(Base):
    """
    Project registry — maps a project name to its filesystem root.

    Every task references a project by name (tasks.project).  This table
    stores the canonical filesystem path so the agent can run git operations
    in the correct repository instead of Maestro's own source tree.
    """
    __tablename__ = "projects"

    name = Column(String, primary_key=True)
    path = Column(String, nullable=True)       # Absolute path to the project root
    description = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Project(name='{self.name}', path='{self.path}')>"


class Task(Base):
    """
    Kanban Task Model
    Represents a single task on the Kanban board with full history tracking
    """
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, index=True)
    title = Column(String, nullable=False)
    type = Column(String, nullable=False, index=True)  # planning, development, review, completed, architecture
    description = Column(Text, nullable=True)
    owner = Column(String, default="user")
    tags = Column(JSON, nullable=True, default=list)
    content = Column(JSON, nullable=True)  # For architecture tasks: frontend, backend, etc.
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    llm_ref = relationship('LLM', lazy='joined')
    budget_ref = relationship('Budget', lazy='joined')
    history = Column(JSON, nullable=True, default=list)  # Array of {status, timestamp}
    prerequisites = Column(JSON, nullable=True, default=list)  # List of prerequisite task IDs
    position = Column(Integer, nullable=True, default=0)  # Position within column (0 = first)
    project = Column(String, default='TheMaestro')  # Project this task belongs to
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=True)  # Links sub-ideas to origin
    subdivision_generation = Column(Integer, nullable=False, default=0)  # Recursion depth (0=human)
    is_big_idea = Column(Boolean, nullable=False, default=False)  # Flagged when subdivision produces children
    interface_contracts = Column(Text, nullable=True)  # JSON: API contracts between sub-ideas
    review_notes = Column(Text, nullable=True)
    demotion_count = Column(Integer, nullable=False, default=0)
    demotion_history = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Task(id={self.id}, title='{self.title}', type='{self.type}', project='{self.project}', position={self.position})>"


def get_db():
    """Get database session"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    """
    Check if database is fresh (new or empty).
    Returns True if database is fresh (new or empty), False if it has data.
    """
    should_seed = False

    # Check if file exists first
    if not os.path.exists(DATABASE_PATH):
        logger.debug("Database file not found, database is fresh (should_seed=True)")
        should_seed = True

    # Check if tables exist and are empty
    db = SessionLocal()
    try:
        # Try to query tasks table - if it doesn't exist, treat as fresh
        try:
            existing_count = db.query(Task).count()
            if existing_count == 0:
                logger.debug("Database exists and is empty, tables are fresh (should_seed=True)")
                should_seed = True
            else:
                logger.debug("Database has %d tasks, not fresh (should_seed=False)", existing_count)
        except Exception as e:
            # Table doesn't exist - database is corrupted/empty
            logger.debug("Database file exists but tables are missing (corrupted), treating as fresh: %s", e)
            should_seed = True
    finally:
        db.close()

    return should_seed

def init_db_tables():
    """Initialize database tables (internal use)"""
    Base.metadata.create_all(bind=engine)
    logger.info("Database tables initialized at: %s", DATABASE_PATH)


def create_task(title, task_type, description="", owner="user", tags=None, content=None, llm_id=None, budget_id=None, prerequisites=None, project='TheMaestro'):
    """Create a new task"""
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
    """Get a task by ID"""
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
    """Get all tasks of a specific type, ordered by position then created_at"""
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.type == task_type).order_by(Task.position, Task.created_at).all()
        return tasks
    except Exception as e:
        logger.error("Error getting tasks by type: %s", e)
        return []
    finally:
        db.close()


def update_task(task_id, **kwargs):
    """Update a task with provided fields"""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            return None
        
        # Update fields
        for key, value in kwargs.items():
            if hasattr(task, key):
                setattr(task, key, value)
        
        # Add to history
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


def delete_task(task_id):
    """Delete a task"""
    db = SessionLocal()
    try:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            db.delete(task)
            db.commit()
            return True
        return False
    except Exception as e:
        db.rollback()
        logger.error("Error deleting task: %s", e)
        return False
    finally:
        db.close()


def get_all_tasks():
    """Get all tasks"""
    db = SessionLocal()
    try:
        tasks = db.query(Task).order_by(Task.created_at).all()
        return tasks
    except Exception as e:
        logger.error("Error getting all tasks: %s", e)
        return []
    finally:
        db.close()


# ============================================
# ResearchJob CRUD
# ============================================

def create_research_job(task_id, question, context=None, priority=0.0, depth=0,
                        llm_id=None, budget_id=None, parent_job_id=None):
    """Create a new research job record."""
    db = SessionLocal()
    try:
        job = ResearchJob(
            task_id=task_id,
            question=question,
            context=context,
            priority=priority,
            depth=depth,
            llm_id=llm_id,
            budget_id=budget_id,
            parent_job_id=parent_job_id,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error creating research job: %s", e)
        return None
    finally:
        db.close()


def get_research_job(job_id):
    """Get a research job by ID."""
    db = SessionLocal()
    try:
        return db.query(ResearchJob).filter(ResearchJob.id == job_id).first()
    except Exception as e:
        logger.error("Error getting research job %s: %s", job_id, e)
        return None
    finally:
        db.close()


def get_pending_research_jobs(limit=10):
    """Return pending research jobs ordered by priority ASC, created_at ASC."""
    db = SessionLocal()
    try:
        return (
            db.query(ResearchJob)
            .filter(ResearchJob.status == 'pending')
            .order_by(ResearchJob.priority, ResearchJob.created_at)
            .limit(limit)
            .all()
        )
    except Exception as e:
        logger.error("Error getting pending research jobs: %s", e)
        return []
    finally:
        db.close()


def update_research_job(job_id, **kwargs):
    """Update a research job with provided fields."""
    db = SessionLocal()
    try:
        job = db.query(ResearchJob).filter(ResearchJob.id == job_id).first()
        if not job:
            return None
        for key, value in kwargs.items():
            if hasattr(job, key):
                setattr(job, key, value)
        if kwargs.get('status') in ('completed', 'failed', 'cancelled'):
            job.completed_at = datetime.now(timezone.utc)
        db.commit()
        db.refresh(job)
        return job
    except Exception as e:
        db.rollback()
        logger.error("Error updating research job %s: %s", job_id, e)
        return None
    finally:
        db.close()


def get_research_jobs_for_task(task_id):
    """Return all research jobs for a task, most recent first."""
    db = SessionLocal()
    try:
        return (
            db.query(ResearchJob)
            .filter(ResearchJob.task_id == task_id)
            .order_by(ResearchJob.created_at.desc())
            .all()
        )
    except Exception as e:
        logger.error("Error getting research jobs for task '%s': %s", task_id, e)
        return []
    finally:
        db.close()


def count_pending_research_jobs():
    """Return the number of pending research jobs."""
    db = SessionLocal()
    try:
        return db.query(ResearchJob).filter(ResearchJob.status == 'pending').count()
    except Exception as e:
        logger.error("Error counting pending research jobs: %s", e)
        return 0
    finally:
        db.close()


# ============================================
# OptimizationBenchmark CRUD
# ============================================

def create_optimization_benchmark(task_id, parent_task_id, benchmark_type, metrics):
    """Record a before/after benchmark for an optimization sub-task."""
    db = SessionLocal()
    try:
        bench = OptimizationBenchmark(
            task_id=task_id,
            parent_task_id=parent_task_id,
            benchmark_type=benchmark_type,
            metrics=metrics if isinstance(metrics, str) else __import__('json').dumps(metrics),
        )
        db.add(bench)
        db.commit()
        db.refresh(bench)
        return bench
    except Exception as e:
        db.rollback()
        logger.error("Error creating optimization benchmark: %s", e)
        return None
    finally:
        db.close()


def get_optimization_benchmarks(parent_task_id):
    """Return all benchmarks for a parent task, ordered by created_at."""
    db = SessionLocal()
    try:
        return (
            db.query(OptimizationBenchmark)
            .filter(OptimizationBenchmark.parent_task_id == parent_task_id)
            .order_by(OptimizationBenchmark.created_at)
            .all()
        )
    except Exception as e:
        logger.error("Error getting benchmarks for task '%s': %s", parent_task_id, e)
        return []
    finally:
        db.close()


# ============================================
# Project CRUD
# ============================================

def get_all_projects():
    """Return all projects ordered by name."""
    db = SessionLocal()
    try:
        return db.query(Project).order_by(Project.name).all()
    except Exception as e:
        logger.error("Error getting projects: %s", e)
        return []
    finally:
        db.close()


def get_project(name: str):
    """Return a single project by name, or None if not found."""
    db = SessionLocal()
    try:
        return db.query(Project).filter(Project.name == name).first()
    except Exception as e:
        logger.error("Error getting project '%s': %s", name, e)
        return None
    finally:
        db.close()


def get_project_path(project_name: str) -> str | None:
    """
    Return the filesystem path for a project, or None if unknown.

    This is the primary helper used by the agent to resolve which git
    repository to operate on for a given task.
    """
    project = get_project(project_name)
    return project.path if project else None


def upsert_project(name: str, path: str | None = None, description: str | None = None):
    """
    Create or update a project.  ``path`` is the absolute filesystem root of
    the project's git repository.  Passing path=None leaves an existing path
    unchanged (use empty string to explicitly clear it).
    """
    db = SessionLocal()
    try:
        existing = db.query(Project).filter(Project.name == name).first()
        if existing:
            if path is not None:
                existing.path = path or None
            if description is not None:
                existing.description = description
            db.commit()
            db.refresh(existing)
            return existing
        else:
            project = Project(name=name, path=path or None, description=description)
            db.add(project)
            db.commit()
            db.refresh(project)
            return project
    except Exception as e:
        db.rollback()
        logger.error("Error upserting project '%s': %s", name, e)
        return None
    finally:
        db.close()


def delete_project(name: str) -> bool:
    """Delete a project record (does not affect tasks that reference it)."""
    db = SessionLocal()
    try:
        project = db.query(Project).filter(Project.name == name).first()
        if not project:
            return False
        db.delete(project)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting project '%s': %s", name, e)
        return False
    finally:
        db.close()


def get_tasks_by_project(project_name):
    """Get all tasks belonging to a specific project, ordered by position then created_at"""
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.project == project_name).order_by(Task.position, Task.created_at).all()
        return tasks
    except Exception as e:
        logger.error("Error getting tasks by project: %s", e)
        return []
    finally:
        db.close()


def get_task_history(task_id):
    """Get history for a specific task"""
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


def seed_sample_tasks():
    """
    Seed the database with sample tasks ONLY if the database is fresh.
    Uses init_db() to check if database needs seeding.
    """
    # Check if database is fresh (new or empty)
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

        # Planning tasks - Position 0, 1, 2
        seed_task(db, "planning-1", "Setup FastAPI development environment", "planning", "Configure Python virtual environment and install dependencies", "user",
                  ["backend", "setup"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "planning-2", "Create Kanban board UI mockup", "planning", "Design wireframes for the Kanban board interface", "user",
                  ["frontend", "design"], None, llm_id=lid, budget_id=bid, position=1)

        seed_task(db, "planning-3", "Implement drag-and-drop", "planning", "Add drag-and-drop functionality for task reordering", "user",
                  ["feature", "frontend"], None, llm_id=lid, budget_id=bid, position=2)

        # In Progress tasks (Development) - Position 0, 1
        seed_task(db, "dev-1", "Configure venv and install dependencies", "indev", "Set up Python 3.13 virtual environment", "user",
                  ["setup", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "dev-2", "Create app structure and main.py", "indev", "Set up FastAPI application with main entry point", "user",
                  ["structure", "backend"], None, llm_id=lid, budget_id=bid, position=1)

        # In Review tasks - Position 0
        seed_task(db, "review-1", "Review requirements.txt", "conceptual_review", "Verify all dependencies are properly listed", "user",
                  ["qa", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        # Completed tasks - Position 0, 1
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


# ============================================
# LLM CRUD
# ============================================

def get_all_llms():
    db = SessionLocal()
    try:
        return db.query(LLM).order_by(LLM.id).all()
    finally:
        db.close()


def get_llm(llm_id):
    db = SessionLocal()
    try:
        return db.query(LLM).filter(LLM.id == llm_id).first()
    finally:
        db.close()


def create_llm(address, port, model, settings=None, parallel_sessions=1, max_context=4096, notes=''):
    db = SessionLocal()
    try:
        llm = LLM(address=address, port=port, model=model, settings=settings,
                   parallel_sessions=parallel_sessions, max_context=max_context, notes=notes)
        db.add(llm)
        db.commit()
        db.refresh(llm)
        return llm
    except Exception as e:
        db.rollback()
        logger.error("Error creating LLM: %s", e)
        return None
    finally:
        db.close()


def update_llm(llm_id, **kwargs):
    db = SessionLocal()
    try:
        llm = db.query(LLM).filter(LLM.id == llm_id).first()
        if not llm:
            return None
        for key, value in kwargs.items():
            if hasattr(llm, key):
                setattr(llm, key, value)
        db.commit()
        db.refresh(llm)
        return llm
    except Exception as e:
        db.rollback()
        logger.error("Error updating LLM: %s", e)
        return None
    finally:
        db.close()


def delete_llm(llm_id):
    db = SessionLocal()
    try:
        llm = db.query(LLM).filter(LLM.id == llm_id).first()
        if not llm:
            return False
        db.delete(llm)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting LLM: %s", e)
        return False
    finally:
        db.close()


# ============================================
# Budget CRUD
# ============================================

def get_all_budgets():
    db = SessionLocal()
    try:
        return db.query(Budget).order_by(Budget.id).all()
    finally:
        db.close()


def get_budget(budget_id):
    db = SessionLocal()
    try:
        return db.query(Budget).filter(Budget.id == budget_id).first()
    finally:
        db.close()


def create_budget(name, settings=None):
    db = SessionLocal()
    try:
        budget = Budget(name=name, settings=settings)
        db.add(budget)
        db.commit()
        db.refresh(budget)
        return budget
    except Exception as e:
        db.rollback()
        logger.error("Error creating budget: %s", e)
        return None
    finally:
        db.close()


def update_budget(budget_id, **kwargs):
    db = SessionLocal()
    try:
        budget = db.query(Budget).filter(Budget.id == budget_id).first()
        if not budget:
            return None
        for key, value in kwargs.items():
            if hasattr(budget, key):
                setattr(budget, key, value)
        db.commit()
        db.refresh(budget)
        return budget
    except Exception as e:
        db.rollback()
        logger.error("Error updating budget: %s", e)
        return None
    finally:
        db.close()


def delete_budget(budget_id):
    db = SessionLocal()
    try:
        budget = db.query(Budget).filter(Budget.id == budget_id).first()
        if not budget:
            return False
        db.delete(budget)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting budget: %s", e)
        return False
    finally:
        db.close()


# ============================================
# TransitionVote CRUD
# ============================================

def create_transition_vote(task_id, transition, stage, verdict, confidence, justification=None, raw_response=None, prompt_tokens=None, completion_tokens=None, model=None, budget_id=None):
    db = SessionLocal()
    try:
        vote = TransitionVote(
            task_id=task_id, transition=transition, stage=stage,
            verdict=verdict, confidence=confidence, justification=justification,
            raw_response=raw_response, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, model=model, budget_id=budget_id
        )
        db.add(vote)
        db.commit()
        db.refresh(vote)
        return vote
    except Exception as e:
        db.rollback()
        logger.error("Error creating transition vote: %s", e)
        return None
    finally:
        db.close()


def get_transition_votes(task_id, transition=None):
    db = SessionLocal()
    try:
        q = db.query(TransitionVote).filter(TransitionVote.task_id == task_id)
        if transition:
            q = q.filter(TransitionVote.transition == transition)
        return q.order_by(TransitionVote.created_at).all()
    finally:
        db.close()


# ============================================
# TransitionResult CRUD
# ============================================

def create_transition_result(task_id, transition, outcome, vote_summary=None, total_prompt_tokens=None, total_completion_tokens=None):
    db = SessionLocal()
    try:
        result = TransitionResult(
            task_id=task_id, transition=transition, outcome=outcome,
            vote_summary=vote_summary, total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating transition result: %s", e)
        return None
    finally:
        db.close()


def get_transition_results(task_id, transition=None):
    db = SessionLocal()
    try:
        q = db.query(TransitionResult).filter(TransitionResult.task_id == task_id)
        if transition:
            q = q.filter(TransitionResult.transition == transition)
        return q.order_by(TransitionResult.created_at.desc()).all()
    finally:
        db.close()


# ============================================
# BudgetEntry CRUD
# ============================================

def create_budget_entry(llm_id=None, budget_id=None, task_id=None,
                        prompt_cost=0, generation_cost=0, tool_calls=0,
                        prompt_data=None, response_data=None):
    db = SessionLocal()
    try:
        entry = BudgetEntry(
            llm_id=llm_id, budget_id=budget_id, task_id=task_id,
            prompt_cost=prompt_cost, generation_cost=generation_cost,
            tool_calls=tool_calls, prompt_data=prompt_data, response_data=response_data,
        )
        db.add(entry)
        db.commit()
        db.refresh(entry)
        return entry
    except Exception as e:
        db.rollback()
        logger.error("Error creating budget entry: %s", e)
        return None
    finally:
        db.close()


def get_budget_entries(budget_id=None, llm_id=None, task_id=None, limit=100, offset=0):
    db = SessionLocal()
    try:
        q = db.query(BudgetEntry)
        if budget_id is not None:
            q = q.filter(BudgetEntry.budget_id == budget_id)
        if llm_id is not None:
            q = q.filter(BudgetEntry.llm_id == llm_id)
        if task_id is not None:
            q = q.filter(BudgetEntry.task_id == task_id)
        return q.order_by(BudgetEntry.created_at.desc()).offset(offset).limit(limit).all()
    finally:
        db.close()


def get_budget_entry(entry_id):
    """Get a single budget entry by ID."""
    db = SessionLocal()
    try:
        return db.query(BudgetEntry).filter(BudgetEntry.id == entry_id).first()
    finally:
        db.close()


# ============================================
# SubdivisionRecord CRUD
# ============================================

def create_subdivision_record(parent_task_id, child_task_ids, generation=1,
                               attempt_number=1, rejection_context=None,
                               agent_vote=None, prompt_tokens=0,
                               completion_tokens=0, status='active',
                               interface_contracts=None):
    db = SessionLocal()
    try:
        record = SubdivisionRecord(
            parent_task_id=parent_task_id,
            attempt_number=attempt_number,
            generation=generation,
            child_task_ids=child_task_ids,
            rejection_context=rejection_context,
            agent_vote=agent_vote,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            interface_contracts=interface_contracts,
            status=status,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error creating subdivision record: %s", e)
        return None
    finally:
        db.close()


def get_subdivision_records(parent_task_id):
    """Get all subdivision records for a parent task, ordered by creation time."""
    db = SessionLocal()
    try:
        return (db.query(SubdivisionRecord)
                .filter(SubdivisionRecord.parent_task_id == parent_task_id)
                .order_by(SubdivisionRecord.created_at.desc())
                .all())
    finally:
        db.close()


def update_subdivision_record(record_id, **kwargs):
    """Update a subdivision record."""
    db = SessionLocal()
    try:
        record = db.query(SubdivisionRecord).filter(SubdivisionRecord.id == record_id).first()
        if not record:
            return None
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error updating subdivision record: %s", e)
        return None
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


def get_budget_summary(budget_id=None):
    """Aggregate totals for a budget (or all budgets if None)."""
    from sqlalchemy import func
    db = SessionLocal()
    try:
        q = db.query(
            func.count(BudgetEntry.id).label('total_entries'),
            func.coalesce(func.sum(BudgetEntry.prompt_cost), 0).label('total_prompt_tokens'),
            func.coalesce(func.sum(BudgetEntry.generation_cost), 0).label('total_generation_tokens'),
            func.coalesce(func.sum(BudgetEntry.tool_calls), 0).label('total_tool_calls'),
        )
        if budget_id is not None:
            q = q.filter(BudgetEntry.budget_id == budget_id)
        row = q.one()
        return {
            'total_entries': row.total_entries,
            'total_prompt_tokens': row.total_prompt_tokens,
            'total_generation_tokens': row.total_generation_tokens,
            'total_tool_calls': row.total_tool_calls,
        }
    finally:
        db.close()


# ============================================
# PlanningResult CRUD
# ============================================

def create_planning_result(task_id, **kwargs):
    db = SessionLocal()
    try:
        result = PlanningResult(task_id=task_id, **kwargs)
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating planning result: %s", e)
        return None
    finally:
        db.close()


def get_planning_result(task_id):
    """Get the latest active planning result for a task."""
    db = SessionLocal()
    try:
        return (db.query(PlanningResult)
                .filter(PlanningResult.task_id == task_id, PlanningResult.status == 'active')
                .order_by(PlanningResult.created_at.desc())
                .first())
    finally:
        db.close()


def update_planning_result(db, result_id, **kwargs):
    """Update a planning result by ID."""
    try:
        result = db.query(PlanningResult).filter(PlanningResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating planning result: %s", e)
        return None


# ============================================
# ComponentResult CRUD
# ============================================

def create_component_result(task_id, component_name, step_order, batch_number, **kwargs):
    db = SessionLocal()
    try:
        result = ComponentResult(
            task_id=task_id, component_name=component_name,
            step_order=step_order, batch_number=batch_number, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating component result: %s", e)
        return None
    finally:
        db.close()


def get_component_results(task_id):
    db = SessionLocal()
    try:
        return (db.query(ComponentResult)
                .filter(ComponentResult.task_id == task_id)
                .order_by(ComponentResult.batch_number, ComponentResult.step_order)
                .all())
    finally:
        db.close()


def update_component_result(result_id, **kwargs):
    db = SessionLocal()
    try:
        result = db.query(ComponentResult).filter(ComponentResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating component result: %s", e)
        return None
    finally:
        db.close()


# ============================================
# OptimizationResult CRUD
# ============================================

def create_optimization_result(task_id, outcome, **kwargs):
    db = SessionLocal()
    try:
        result = OptimizationResult(task_id=task_id, outcome=outcome, **kwargs)
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating optimization result: %s", e)
        return None
    finally:
        db.close()


def get_optimization_result(task_id):
    db = SessionLocal()
    try:
        return (db.query(OptimizationResult)
                .filter(OptimizationResult.task_id == task_id)
                .order_by(OptimizationResult.created_at.desc())
                .first())
    finally:
        db.close()


def update_optimization_result(db, result_id, **kwargs):
    """Update an optimization result by ID."""
    try:
        result = db.query(OptimizationResult).filter(OptimizationResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating optimization result: %s", e)
        return None


# ============================================
# SecurityReviewResult CRUD
# ============================================

def create_security_review_result(task_id, reviewer_type, verdict, confidence, **kwargs):
    db = SessionLocal()
    try:
        result = SecurityReviewResult(
            task_id=task_id, reviewer_type=reviewer_type,
            verdict=verdict, confidence=confidence, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating security review result: %s", e)
        return None
    finally:
        db.close()


def get_security_review_results(task_id):
    db = SessionLocal()
    try:
        return (db.query(SecurityReviewResult)
                .filter(SecurityReviewResult.task_id == task_id)
                .order_by(SecurityReviewResult.created_at.desc())
                .all())
    finally:
        db.close()


def update_security_review_result(db, result_id, **kwargs):
    """Update a security review result by ID."""
    try:
        result = db.query(SecurityReviewResult).filter(SecurityReviewResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating security review result: %s", e)
        return None


# ============================================
# FullReviewResult CRUD
# ============================================

def create_full_review_result(task_id, reviewer_type, verdict, confidence, **kwargs):
    db = SessionLocal()
    try:
        result = FullReviewResult(
            task_id=task_id, reviewer_type=reviewer_type,
            verdict=verdict, confidence=confidence, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating full review result: %s", e)
        return None
    finally:
        db.close()


def get_full_review_results(task_id):
    db = SessionLocal()
    try:
        return (db.query(FullReviewResult)
                .filter(FullReviewResult.task_id == task_id)
                .order_by(FullReviewResult.created_at.desc())
                .all())
    finally:
        db.close()


def update_full_review_result(db, result_id, **kwargs):
    """Update a full review result by ID."""
    try:
        result = db.query(FullReviewResult).filter(FullReviewResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating full review result: %s", e)
        return None


# ============================================
# MergeRecord CRUD
# ============================================

def create_merge_record(task_id, branch_name, status, **kwargs):
    db = SessionLocal()
    try:
        record = MergeRecord(
            task_id=task_id, branch_name=branch_name,
            status=status, **kwargs
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error creating merge record: %s", e)
        return None
    finally:
        db.close()


def get_merge_record(task_id):
    db = SessionLocal()
    try:
        return (db.query(MergeRecord)
                .filter(MergeRecord.task_id == task_id)
                .order_by(MergeRecord.created_at.desc())
                .first())
    finally:
        db.close()


def update_merge_record(db, record_id, **kwargs):
    """Update a merge record by ID."""
    try:
        record = db.query(MergeRecord).filter(MergeRecord.id == record_id).first()
        if not record:
            return None
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error updating merge record: %s", e)
        return None
