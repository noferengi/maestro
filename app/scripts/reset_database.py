"""
Database Management Script
Reset database, seed data, or inspect current state.

Usage:
    python reset_database.py reset       - Reset database and seed
    python reset_database.py inspect     - Show current database state
    python reset_database.py seed        - Seed database without reset
    python reset_database.py seed --force - Force seed even if data exists
"""

import argparse
import sys
import os

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from database import (
    DATABASE_PATH, engine, Base, Task,
    init_db, init_db_tables, seed_sample_tasks,
    get_all_tasks, get_tasks_by_type
)
from sqlalchemy import text


def reset_database():
    """
    Reset the database by dropping all tables and re-seeding.
    WARNING: This will delete all existing data!
    """
    print("=== Database Reset ===")
    print("WARNING: This will delete all existing data!")
    confirm = input("Continue? (yes/no): ").strip().lower()

    if confirm != 'yes':
        print("Reset cancelled.")
        return False

    # Drop all tables
    print("Dropping all tables...")
    Base.metadata.drop_all(bind=engine)

    # Create tables
    print("Creating tables...")
    init_db_tables()

    # Seed data
    print("Seeding sample tasks...")
    seed_sample_tasks()

    print("Database reset and seeded successfully!")
    return True


def inspect_database():
    """
    Inspect the current database state.
    """
    print("=== Database Inspection ===")
    print("DATABASE_PATH:", DATABASE_PATH)
    print("File exists:", os.path.exists(DATABASE_PATH))

    if not os.path.exists(DATABASE_PATH):
        print("Database file does not exist!")
        return

    # Check if tables exist
    print("\nChecking tables...")
    try:
        with engine.connect() as conn:
            count = conn.execute(text("SELECT COUNT(*) FROM tasks")).scalar()
        print("Tasks table exists with", count, "rows")
    except Exception as e:
        print("Tasks table does not exist:", e)
        return

    # Show tasks
    print("\n=== All Tasks ===")
    tasks = get_all_tasks()
    for task in tasks:
        print("\n  ID:", task.id)
        print("  Title:", task.title)
        print("  Type:", task.type)
        print("  Position:", task.position)
        print("  Created:", task.created_at)

    # Show by type
    print("\n=== Tasks by Type ===")
    for task_type in ['architecture', 'planning', 'development', 'review', 'completed']:
        type_tasks = get_tasks_by_type(task_type)
        print("\n", task_type.upper() + ":", len(type_tasks), "tasks")
        for task in type_tasks:
            print("    -", task.id + ":", task.title + " (position=" + str(task.position) + ")")


def seed_database(force=False):
    """
    Seed the database if it's empty, or force seed if --force is used.
    """
    print("=== Database Seeding ===")

    # Check if database is fresh
    is_fresh = init_db()

    if not is_fresh and not force:
        print("Database already has data, skipping seed.")
        return False

    if not is_fresh and force:
        print("Force seeding requested. Resetting database...")
        # Reset database
        Base.metadata.drop_all(bind=engine)
        init_db_tables()
        seed_sample_tasks()
        print("Database seeded successfully!")
        return True

    # Create tables
    init_db_tables()

    # Seed data
    print("Seeding sample tasks...")
    seed_sample_tasks()

    print("Database seeded successfully!")
    return True


def main():
    parser = argparse.ArgumentParser(description="Database management script")
    parser.add_argument(
        "action",
        choices=["reset", "inspect", "seed"],
        help="Action to perform: reset, inspect, or seed"
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Force operation (e.g., force seed even if data exists)"
    )

    args = parser.parse_args()

    if args.action == "reset":
        reset_database()
    elif args.action == "inspect":
        inspect_database()
    elif args.action == "seed":
        seed_database(force=args.force)


if __name__ == "__main__":
    main()
