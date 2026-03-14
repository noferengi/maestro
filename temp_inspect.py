from app.database import DATABASE_PATH, engine
import os

print("=== Database Inspection ===")
print(f"DATABASE_PATH: {DATABASE_PATH}")
print(f"File exists: {os.path.exists(DATABASE_PATH)}")

if os.path.exists(DATABASE_PATH):
    import sqlite3
    conn = sqlite3.connect(DATABASE_PATH)
    cursor = conn.cursor()
    
    # Get all table names
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    tables = [row[0] for row in cursor.fetchall()]
    print(f"Tables in database: {tables}")
    
    # Check for tasks table
    if 'tasks' in tables:
        cursor.execute("SELECT COUNT(*) FROM tasks")
        count = cursor.fetchone()[0]
        print(f"Tasks table exists with {count} rows")
        
        # Show first 5 tasks
        cursor.execute("SELECT id, title, type, position FROM tasks ORDER BY position, created_at LIMIT 5")
        tasks = cursor.fetchall()
        print("\nFirst 5 tasks:")
        for task in tasks:
            print(f"  - {task[0]}: {task[1]} ({task[2]}) position={task[3]}")
    else:
        print("Tasks table does NOT exist (corrupted/incomplete database)")
    
    conn.close()
else:
    print("Database file does NOT exist - ready for fresh seed!")

print("\nDone!")
