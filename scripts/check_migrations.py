from sqlalchemy import create_engine, text
import os
import sys

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from app.agent.config import ADMIN_DATABASE_URL

def check():
    engine = create_engine(ADMIN_DATABASE_URL)
    with engine.connect() as conn:
        print("Schema Migrations in Postgres:")
        res = conn.execute(text("SELECT migration_id, applied_at FROM schema_migrations ORDER BY migration_id"))
        rows = res.fetchall()
        print(f"Total rows: {len(rows)}")
        for row in rows[:5]:
            print(f"  {row[0]} - {row[1]}")

if __name__ == "__main__":
    check()
