import sqlite3
from pathlib import Path

DB_PATH = Path("data/kanban.db")

try:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, timeout=30)
    conn.row_factory = sqlite3.Row
    res = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()
    print(f"Connection successful. Task count: {res[0]}")
except Exception as e:
    print(f"Connection failed: {e}")
finally:
    if 'conn' in locals():
        conn.close()
