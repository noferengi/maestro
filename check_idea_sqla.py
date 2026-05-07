import sys
sys.path.insert(0, 'D:\\workspace\\TheMaestro')

from app.database import SessionLocal
from app.database.models import Task

db = SessionLocal()
tasks = db.query(Task).filter(Task.type == 'idea', Task.is_active == True).all()
for t in tasks:
    print(f"ID: {t.id}")
    print(f"  Title: {t.title}")
    print(f"  type: {t.type}")
    print(f"  clarification_status: {t.clarification_status}")
    print(f"  clarification_status type: {type(t.clarification_status)}")
    print("---")
db.close()
