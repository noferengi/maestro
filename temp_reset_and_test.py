"""
Reset database and test drag-and-drop persistence.
"""

import requests
from app.database import get_all_tasks, seed_sample_tasks, init_db, init_db_tables

print("=== Resetting Database ===")

# Reset database
is_fresh = init_db()
if is_fresh:
    init_db_tables()
    seed_sample_tasks()
    print("Database reset successfully")
else:
    print("Database already has data")

# Get initial state
print("\n=== Initial State ===")
from app.database import get_tasks_by_type
planning_tasks = get_tasks_by_type('planning')
for task in planning_tasks:
    print(f"  {task.id}: position={task.position}")

# Simulate drag-and-drop: Drag planning-1 to position 2
print("\n=== Simulating Drag: planning-1 to position 2 ===")
response = requests.post(
    "http://localhost:8000/api/tasks/planning-1/reorder",
    json={"position": 2, "type": "planning"}
)
print(f"Response: {response.json()}")

# Verify
print("\n=== After Reorder ===")
planning_tasks = get_tasks_by_type('planning')
for task in planning_tasks:
    print(f"  {task.id}: position={task.position}")

print("\n=== Test Complete ===")
