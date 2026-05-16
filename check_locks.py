import sqlalchemy
import os
from sqlalchemy import create_engine, text

ADMIN_DATABASE_URL = "postgresql://maestro_admin:fury26566488@arcbox:5432/maestro_db"

engine = create_engine(ADMIN_DATABASE_URL)

query = """
SELECT
    blocked_locks.pid     AS blocked_pid,
    blocking_locks.pid     AS blocking_pid,
    blocked_activity.query    AS blocked_statement,
    blocking_activity.query   AS blocking_statement
FROM  pg_catalog.pg_locks         blocked_locks
JOIN pg_catalog.pg_stat_activity blocked_activity  ON blocked_activity.pid = blocked_locks.pid
JOIN pg_catalog.pg_locks         blocking_locks 
    ON blocking_locks.locktype = blocked_locks.locktype
    AND blocking_locks.database IS NOT DISTINCT FROM blocked_locks.database
    AND blocking_locks.relation IS NOT DISTINCT FROM blocked_locks.relation
    AND blocking_locks.page IS NOT DISTINCT FROM blocked_locks.page
    AND blocking_locks.tuple IS NOT DISTINCT FROM blocked_locks.tuple
    AND blocking_locks.virtualxid IS NOT DISTINCT FROM blocked_locks.virtualxid
    AND blocking_locks.transactionid IS NOT DISTINCT FROM blocked_locks.transactionid
    AND blocking_locks.classid IS NOT DISTINCT FROM blocked_locks.classid
    AND blocking_locks.objid IS NOT DISTINCT FROM blocked_locks.objid
    AND blocking_locks.objsubid IS NOT DISTINCT FROM blocked_locks.objsubid
    AND blocking_locks.pid != blocked_locks.pid
JOIN pg_catalog.pg_stat_activity blocking_activity ON blocking_activity.pid = blocking_locks.pid
WHERE NOT blocked_locks.granted;
"""

try:
    with engine.connect() as conn:
        res = conn.execute(text(query)).fetchall()
        if not res:
            print("No blocking locks found.")
        for row in res:
            print(f"Blocked PID: {row[0]}")
            print(f"Blocking PID: {row[1]}")
            print(f"Blocked Query: {row[2]}")
            print(f"Blocking Query: {row[3]}")
            print("-" * 40)
except Exception as e:
    print(f"Error: {e}")
