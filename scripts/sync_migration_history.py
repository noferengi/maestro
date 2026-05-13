import sqlite3
from sqlalchemy import create_engine, text
import os
import sys
from datetime import datetime

# Add project root to path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, PROJECT_ROOT)

from app.agent.config import ADMIN_DATABASE_URL

def sync_history():
    # 1. Get history from SQLite
    sqlite_conn = sqlite3.connect("data/kanban.db")
    res = sqlite_conn.execute("SELECT migration_id, applied_at FROM schema_migrations")
    history = res.fetchall()
    sqlite_conn.close()
    
    print(f"Found {len(history)} migration records in SQLite.")

    # 2. Insert into Postgres
    engine = create_engine(ADMIN_DATABASE_URL)
    with engine.begin() as pg_conn:
        # Clear existing (if any)
        pg_conn.execute(text("DELETE FROM schema_migrations"))
        
        # Insert
        for mid, at in history:
            pg_conn.execute(
                text("INSERT INTO schema_migrations (migration_id, applied_at) VALUES (:mid, :at)"),
                {"mid": mid, "at": at}
            )
    
    print("Successfully synced migration history to Postgres.")

if __name__ == "__main__":
    sync_history()
