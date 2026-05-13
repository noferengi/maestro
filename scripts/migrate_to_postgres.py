"""
Data migration script from SQLite to PostgreSQL for Project Maestro.
Handles schema creation and memory-efficient chunked data transfer.
"""

import os
import sys
import logging
import json
from sqlalchemy import create_engine, MetaData, Table, select, insert, func, text
from sqlalchemy.orm import sessionmaker

# Add app directory to path for imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.agent.config import DATABASE_URL as TARGET_URL
from app.database.session import DATABASE_PATH, init_db_tables, Base

logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

CHUNK_SIZE = 1000

def migrate():
    # 1. Check if target is actually PostgreSQL
    if not TARGET_URL.startswith("postgresql"):
        logger.error("TARGET_URL in maestro.ini/env must be a postgresql:// URL.")
        return

    # 2. Source (SQLite) setup
    source_url = f"sqlite:///{DATABASE_PATH}"
    logger.info(f"Source: {source_url}")
    logger.info(f"Target: {TARGET_URL}")

    source_engine = create_engine(source_url)
    source_metadata = MetaData()
    source_metadata.reflect(bind=source_engine)

    # 3. Target (Postgres) setup - initialize tables
    logger.info("Initializing target tables...")
    # This creates the schema based on SQLAlchemy models
    init_db_tables()
    
    target_engine = create_engine(TARGET_URL)
    target_metadata = MetaData()
    target_metadata.reflect(bind=target_engine)

    # 4. Migrate data table by table
    # Only migrate tables that exist in both source and target
    source_tables = set(source_metadata.tables.keys())
    target_tables = set(target_metadata.tables.keys())
    common_tables = source_tables.intersection(target_tables)
    
    logger.info(f"Common tables found: {len(common_tables)}")
    
    # Priority order to satisfy foreign keys
    # Infrastructure first, then Projects, then Tasks, then everything else
    priority = [
        "budgets", "compute_nodes", "llms", "projects", "tasks",
        "agent_sessions", "budget_entries", "performance_improvement_plans"
    ]
    other_tables = [t for t in common_tables if t not in priority]
    ordered_tables = [t for t in priority if t in common_tables] + other_tables

    with target_engine.begin() as target_conn:
        # Step 4a: Clear existing data in REVERSE order to respect FKs
        logger.info("Clearing existing data in target database...")
        for table_name in reversed(ordered_tables):
            try:
                target_table = Table(table_name, target_metadata, autoload_with=target_engine)
                target_conn.execute(target_table.delete())
            except Exception as e:
                logger.warning(f"  Could not clear table {table_name}: {e}")

        # Cache of valid IDs for FK filtering
        valid_task_ids = set()
        valid_project_ids = set()
        valid_llm_ids = set()
        valid_budget_ids = set()
        valid_compute_node_ids = set()

        # Step 4b: Migrate data in FORWARD order
        for table_name in ordered_tables:
            logger.info(f"Migrating table: {table_name}...")
            
            source_table = Table(table_name, source_metadata, autoload_with=source_engine)
            target_table = Table(table_name, target_metadata, autoload_with=target_engine)
            
            # Fetch data in chunks
            with source_engine.connect() as source_conn:
                # Get total count for progress reporting
                total_count = source_conn.execute(select(func.count()).select_from(source_table)).scalar()
                logger.info(f"  Total rows to migrate: {total_count}")
                
                offset = 0
                while offset < total_count:
                    # Select chunk
                    rows = source_conn.execute(
                        select(source_table).offset(offset).limit(CHUNK_SIZE)
                    ).mappings().all()
                    
                    if not rows:
                        break
                    
                    # Convert rows to list of dicts for insertion
                    data = [dict(row) for row in rows]
                    
                    # Filter data to prevent FK violations
                    filtered_data = []
                    for d in data:
                        # Check task_id FK
                        if "task_id" in d and d["task_id"] and d["task_id"] not in valid_task_ids:
                            # Special case: budget_entries can have task_id=__file_summaries__
                            if table_name == "budget_entries" and d["task_id"] == "__file_summaries__":
                                pass
                            else:
                                continue
                        if "parent_task_id" in d and d["parent_task_id"] and d["parent_task_id"] not in valid_task_ids:
                            continue
                        
                        # Check project_id FK
                        if "project_id" in d and d["project_id"] and d["project_id"] not in valid_project_ids:
                            continue
                        
                        # Check llm_id FK
                        if "llm_id" in d and d["llm_id"] and d["llm_id"] not in valid_llm_ids:
                            continue
                        
                        # Check budget_id FK
                        if "budget_id" in d and d["budget_id"] and d["budget_id"] not in valid_budget_ids:
                            continue
                            
                        # Check compute_node_id FK
                        if "compute_node_id" in d and d["compute_node_id"] and d["compute_node_id"] not in valid_compute_node_ids:
                            continue

                        filtered_data.append(d)
                    
                    if not filtered_data:
                        offset += len(rows)
                        continue

                    # Insert chunk into target
                    try:
                        target_conn.execute(insert(target_table), filtered_data)
                    except Exception as e:
                        logger.error(f"  Error migrating chunk for {table_name}: {e}")
                        # If a chunk fails, try inserting rows one by one to identify the problem
                        # and keep as much data as possible
                        for row_data in filtered_data:
                            try:
                                target_conn.execute(insert(target_table), [row_data])
                            except Exception as row_e:
                                logger.debug(f"    Skipping row in {table_name} due to error: {row_e}")
                    
                    # If this was a primary ID table, add the new IDs to the valid set
                    if table_name == "tasks":
                        valid_task_ids.update(d["id"] for d in filtered_data)
                    elif table_name == "projects":
                        valid_project_ids.update(d["id"] for d in filtered_data)
                    elif table_name == "llms":
                        valid_llm_ids.update(d["id"] for d in filtered_data)
                    elif table_name == "budgets":
                        valid_budget_ids.update(d["id"] for d in filtered_data)
                    elif table_name == "compute_nodes":
                        valid_compute_node_ids.update(d["id"] for d in filtered_data)
                    
                    offset += len(rows)
                    if offset % 5000 == 0 or offset >= total_count:
                        logger.info(f"  Migrated {offset}/{total_count} rows...")

        # 5. Fix PostgreSQL sequences for auto-incrementing primary keys
        logger.info("Resetting PostgreSQL sequences...")
        for table_name in ordered_tables:
            try:
                target_table = Table(table_name, target_metadata, autoload_with=target_engine)
                # Find the primary key column (usually 'id')
                pk_cols = [c.name for c in target_table.primary_key.columns]
                if pk_cols:
                    pk = pk_cols[0]
                    # Check if max actually exists
                    max_id = target_conn.execute(text(f"SELECT max({pk}) FROM {table_name}")).scalar()
                    if max_id:
                        target_conn.execute(text(f"SELECT setval(pg_get_serial_sequence('{table_name}', '{pk}'), {max_id})"))
                        logger.info(f"  Reset sequence for {table_name}.{pk} to {max_id}")
            except Exception as e:
                # Some tables might not have serial/sequence PKs
                logger.debug(f"  Could not reset sequence for {table_name}: {e}")

    logger.info("Migration complete!")

if __name__ == "__main__":
    migrate()
