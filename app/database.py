"""
Kanban Board Database Layer
SQLite-based persistence for Kanban tasks
"""

from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
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
    history = Column(JSON, nullable=True, default=list)  # Array of {status, timestamp}
    prerequisites = Column(JSON, nullable=True, default=list)  # List of prerequisite task IDs
    position = Column(Integer, nullable=True, default=0)  # Position within column (0 = first)
    project = Column(String, default='TheMaestro')  # Project this task belongs to
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


def create_task(title, task_type, description="", owner="user", tags=None, content=None, prerequisites=None, project='TheMaestro'):
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
        # Architecture tasks (immutable)
        seed_task(db, "arch-1", "Project Stack", "architecture", "Core technology stack for TheMaestro", "user", 
                  ["core", "infrastructure"], {"frontend": "HTML/CSS/JS", "backend": "FastAPI + Uvicorn", "database": "SQLite (development)", "style": "Bootstrap CSS"}, 0)

        seed_task(db, "arch-2", "Code Structure", "architecture", "Organizational structure of the codebase", "user",
                  ["core", "structure"], {"dags": "dags.py", "config": "config.py", "repl": "repl.py", "tests": "test_*.py"}, 1)

        # Planning tasks - Position 0, 1, 2
        seed_task(db, "planning-1", "Setup FastAPI development environment", "planning", "Configure Python virtual environment and install dependencies", "user",
                  ["backend", "setup"], None, 0)

        seed_task(db, "planning-2", "Create Kanban board UI mockup", "planning", "Design wireframes for the Kanban board interface", "user",
                  ["frontend", "design"], None, 1)

        seed_task(db, "planning-3", "Implement drag-and-drop", "planning", "Add drag-and-drop functionality for task reordering", "user",
                  ["feature", "frontend"], None, 2)

        # In Progress tasks (Development) - Position 0, 1
        seed_task(db, "dev-1", "Configure venv and install dependencies", "development", "Set up Python 3.13 virtual environment", "user",
                  ["setup", "backend"], None, 0)

        seed_task(db, "dev-2", "Create app structure and main.py", "development", "Set up FastAPI application with main entry point", "user",
                  ["structure", "backend"], None, 1)

        # In Review tasks - Position 0
        seed_task(db, "review-1", "Review requirements.txt", "review", "Verify all dependencies are properly listed", "user",
                  ["qa", "backend"], None, 0)

        # Completed tasks - Position 0, 1
        seed_task(db, "completed-1", "Initialize Git repository", "completed", "Create .gitignore and initial commit", "user",
                  ["setup", "devops"], None, 0)

        seed_task(db, "completed-2", "Create database schema", "completed", "Define SQLAlchemy models for tasks", "user",
                  ["database", "backend"], None, 1)

        print("Successfully seeded 10 sample tasks!")
        return True

    except Exception as e:
        db.rollback()
        print(f"Error seeding tasks: {e}")
        return False
    finally:
        db.close()


def seed_task(db, id, title, task_type, description="", owner="user", tags=None, content=None, position=0, project='TheMaestro'):
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
                 created_at, updated_at, prerequisites, project)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (*t, now, now, json.dumps([]), 'TheMaestro'),
        )
        print(f"  Seeded task: {t[0]} - {t[1]}")
    conn.commit()
    print("Successfully seeded 10 sample tasks (raw)!")


def reorder_tasks(task_id, new_position, task_type):
    """
    Reorder a task within its column
    new_position is the index where the task should be inserted
    Updates position and shifts other tasks accordingly
    """
    db = SessionLocal()
    try:
        # Get all tasks of this type
        tasks = db.query(Task).filter(Task.type == task_type).order_by(Task.position).all()

        # Find the task's current position
        task_to_move = None
        for task in tasks:
            if task.id == task_id:
                task_to_move = task
                break

        if not task_to_move:
            return False

        # Get current index in the list
        current_index = tasks.index(task_to_move)

        # Clamp new_position to valid range
        new_position = max(0, min(new_position, len(tasks) - 1))

        # Reorder the tasks list
        # Remove the task from its current position
        tasks.pop(current_index)
        
        # Insert at the new position
        tasks.insert(new_position, task_to_move)
        
        # Update all tasks' positions based on their new indices
        for i, task in enumerate(tasks):
            task.position = i

        db.commit()
        return True
    except Exception as e:
        db.rollback()
        print(f"Error reordering tasks: {e}")
        return False
    finally:
        db.close()
