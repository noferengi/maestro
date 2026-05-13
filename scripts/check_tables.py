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
        print("Tables in Postgres:")
        res = conn.execute(text("SELECT tablename FROM pg_catalog.pg_tables WHERE schemaname = 'public'"))
        for row in res:
            print(f"  {row[0]}")

if __name__ == "__main__":
    check()
