import sqlite3, json

conn = sqlite3.connect('data/kanban.db')
c = conn.cursor()
c.execute("SELECT id, title, type, clarification_status, content FROM tasks WHERE type = 'idea' AND is_active = 1")
rows = c.fetchall()

for r in rows:
    print(f"ID: {r[0]}")
    print(f"Title: {r[1]}")
    print(f"Type: {r[2]}")
    print(f"Clarification Status: {r[3]}")
    print(f"Content: {json.dumps(r[4], indent=2)[:800]}")
    print("---")

conn.close()
