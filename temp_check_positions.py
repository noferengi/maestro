from app.database import get_all_tasks

tasks = get_all_tasks()
print("All tasks with positions:")
for t in tasks:
    print(f"  {t.id}: type={t.type}, position={t.position}")
