"""
Temporary script to seed the database if needed.
Only seeds if database is empty or doesn't exist.
"""

from app.database import DATABASE_PATH, engine, init_db, init_db_tables, seed_sample_tasks, Task
from sqlalchemy import inspect
import os

print("=== Database Seeding Script ===")
print(f"DATABASE_PATH: {DATABASE_PATH}")
print(f"File exists: {os.path.exists(DATABASE_PATH)}")

# Check if database needs seeding
should_seed = init_db()
print(f"Should seed: {should_seed}")

if should_seed:
    print("Database is fresh, initializing tables and seeding...")
    init_db_tables()
    seed_sample_tasks()
    print("\n✅ Database seeded successfully!")
else:
    print("Database already has data, skipping seed.")
    
    # Just inspect what's there
    from app.database import get_all_tasks
    tasks = get_all_tasks()
    print(f"\nCurrent tasks in database: {len(tasks)}")
    for task in tasks[:5]:
        print(f"  - {task.id}: {task.title} (type={task.type}, position={task.position})")

print("\nDone!")
