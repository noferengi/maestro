from app.database import get_tasks_by_type

tasks = get_tasks_by_type('planning')
print("Planning tasks after reorder:")
for task in tasks:
    print(f"  {task.id}: position={task.position}")
