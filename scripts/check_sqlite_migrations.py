import sqlite3
import os

def check():
    db_path = "data/kanban.db"
    if not os.path.exists(db_path):
        print(f"{db_path} not found")
        return
    conn = sqlite3.connect(db_path)
    res = conn.execute("SELECT migration_id FROM schema_migrations ORDER BY migration_id")
    rows = res.fetchall()
    print(f"Total migrations in SQLite: {len(rows)}")
    for row in rows[:5]:
        print(f"  {row[0]}")

if __name__ == "__main__":
    check()
