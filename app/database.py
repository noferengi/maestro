"""
Kanban Board Database Layer
SQLite-based persistence for Kanban tasks
"""

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON, ForeignKey, UniqueConstraint
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from datetime import datetime
import os

# Database path - keep it in the project directory
DATABASE_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'kanban.db')

# Ensure data directory exists
os.makedirs(os.path.dirname(DATABASE_PATH), exist_ok=True)

# Create database engine
engine = create_engine(f"sqlite:///{DATABASE_PATH}", echo=False)

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
    status = Column(String, nullable=False, default='active')  # active | superseded | failed
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SubdivisionRecord(id={self.id}, parent={self.parent_task_id}, attempt={self.attempt_number}, status={self.status})>"


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
        print(f"Database file not found, database is fresh (should_seed=True)")
        should_seed = True

    # Check if tables exist and are empty
    db = SessionLocal()
    try:
        # Try to query tasks table - if it doesn't exist, treat as fresh
        try:
            existing_count = db.query(Task).count()
            if existing_count == 0:
                print(f"Database exists and is empty, tables are fresh (should_seed=True)")
                should_seed = True
            else:
                print(f"Database has {existing_count} tasks, not fresh (should_seed=False)")
        except Exception as e:
            # Table doesn't exist - database is corrupted/empty
            print(f"Database file exists but tables are missing (corrupted), treating as fresh: {e}")
            should_seed = True
    finally:
        db.close()

    return should_seed

def init_db_tables():
    """Initialize database tables (internal use)"""
    Base.metadata.create_all(bind=engine)
    print(f"Database tables initialized at: {DATABASE_PATH}")


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
        print(f"Error creating task: {e}")
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
        print(f"Error getting task: {e}")
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
        print(f"Error getting tasks by type: {e}")
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
        print(f"Error updating task: {e}")
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
        print(f"Error deleting task: {e}")
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
        print(f"Error getting all tasks: {e}")
        return []
    finally:
        db.close()


def get_tasks_by_project(project_name):
    """Get all tasks belonging to a specific project, ordered by position then created_at"""
    db = SessionLocal()
    try:
        tasks = db.query(Task).filter(Task.project == project_name).order_by(Task.position, Task.created_at).all()
        return tasks
    except Exception as e:
        print(f"Error getting tasks by project: {e}")
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
        print(f"Error getting task history: {e}")
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
        print("Database not fresh, skipping seed.")
        return False
    
    # Create tables for fresh database
    init_db_tables()
    
    print("Seeding sample tasks...")
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
        seed_task(db, "dev-1", "Configure venv and install dependencies", "development", "Set up Python 3.13 virtual environment", "user",
                  ["setup", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "dev-2", "Create app structure and main.py", "development", "Set up FastAPI application with main entry point", "user",
                  ["structure", "backend"], None, llm_id=lid, budget_id=bid, position=1)

        # In Review tasks - Position 0
        seed_task(db, "review-1", "Review requirements.txt", "review", "Verify all dependencies are properly listed", "user",
                  ["qa", "backend"], None, llm_id=lid, budget_id=bid, position=0)

        # Completed tasks - Position 0, 1
        seed_task(db, "completed-1", "Initialize Git repository", "completed", "Create .gitignore and initial commit", "user",
                  ["setup", "devops"], None, llm_id=lid, budget_id=bid, position=0)

        seed_task(db, "completed-2", "Create database schema", "completed", "Define SQLAlchemy models for tasks", "user",
                  ["database", "backend"], None, llm_id=lid, budget_id=bid, position=1)

        print("Successfully seeded 10 sample tasks!")
        return True

    except Exception as e:
        db.rollback()
        print(f"Error seeding tasks: {e}")
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
        print(f"  Created sample task: {id} - {title}")
        return task
    except Exception as e:
        db.rollback()
        print(f"  Error creating sample task {id}: {e}")
        return None


def seed_sample_tasks_raw(conn):
    """
    Seed the 10 canonical sample tasks using a raw sqlite3 connection.
    Called by the migration runner's reset command — no SQLAlchemy required.
    conn must already be connected to kanban.db with all migrations applied.
    """
    import json
    now = datetime.utcnow().isoformat()
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
        ("dev-1",       "Configure venv and install dependencies","development",  "Set up Python 3.13 virtual environment",                          "user", json.dumps(["setup", "backend"]),         None, history, 0),
        ("dev-2",       "Create app structure and main.py",       "development",  "Set up FastAPI application with main entry point",                "user", json.dumps(["structure", "backend"]),     None, history, 1),
        ("review-1",    "Review requirements.txt",                "review",       "Verify all dependencies are properly listed",                     "user", json.dumps(["qa", "backend"]),             None, history, 0),
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
        print(f"  Seeded task: {t[0]} - {t[1]}")
    conn.commit()
    print("Successfully seeded 10 sample tasks (raw)!")


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
        print(f"Error reordering tasks: {e}")
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
        print(f"Error creating LLM: {e}")
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
        print(f"Error updating LLM: {e}")
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
        print(f"Error deleting LLM: {e}")
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
        print(f"Error creating budget: {e}")
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
        print(f"Error updating budget: {e}")
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
        print(f"Error deleting budget: {e}")
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
        print(f"Error creating transition vote: {e}")
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
        print(f"Error creating transition result: {e}")
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
        print(f"Error creating budget entry: {e}")
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
                               completion_tokens=0, status='active'):
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
            status=status,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        print(f"Error creating subdivision record: {e}")
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
        print(f"Error updating subdivision record: {e}")
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
