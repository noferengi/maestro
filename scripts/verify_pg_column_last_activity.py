
import os
import psycopg2
from dotenv import load_dotenv

load_dotenv()

db_url = os.getenv("MAESTRO_ADMIN_DATABASE_URL")
if not db_url or "postgresql" not in db_url:
    print("PostgreSQL admin URL not found in .env")
    exit(1)

try:
    conn = psycopg2.connect(db_url)
    cur = conn.cursor()
    cur.execute("SELECT column_name, data_type FROM information_schema.columns WHERE table_name = 'agent_sessions' AND column_name = 'last_activity_at';")
    row = cur.fetchone()
    if row:
        print(f"Column found: {row[0]} ({row[1]})")
    else:
        print("Column last_activity_at NOT found in PostgreSQL table agent_sessions")
    
    cur.close()
    conn.close()
except Exception as e:
    print(f"Error connecting to PostgreSQL: {e}")
