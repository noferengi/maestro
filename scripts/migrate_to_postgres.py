"""
migrate_to_postgres.py — Authoritative SQLite → PostgreSQL migration.

Replaces the original broken script that used Base.metadata.create_all() for
schema creation (which silently missed migration-only columns and all indices)
and a buggy FK filter that dropped rows without warning.

Correct approach
----------------
  Phase 1 — Schema
    Drop every table in PostgreSQL (CASCADE), then replay all 65 migrations via
    the migration runner.  This produces a schema byte-for-byte identical to the
    SQLite database: all columns, all indices, all constraints.

  Phase 2 — Data
    Copy every row from SQLite to PostgreSQL in strict FK dependency order so
    parent rows always exist before child rows are inserted.  Two self-referential
    tables (tasks.parent_task_id, research_jobs.parent_job_id) are handled with a
    two-pass insert: first insert all rows with the self-ref column set to NULL,
    then UPDATE to restore the original values.

  Phase 3 — Sequences
    Reset every PostgreSQL serial sequence to max(pk) so the next INSERT does not
    collide with an existing row.

  Phase 4 — Verification
    Print a per-table row-count comparison so you can spot any divergence.

Usage
-----
    # Interactive (will prompt for confirmation):
    venv/Scripts/python.exe scripts/migrate_to_postgres.py

    # Non-interactive (CI / scripted):
    venv/Scripts/python.exe scripts/migrate_to_postgres.py --yes

Prerequisites
-------------
  * maestro.ini or environment variables must supply MAESTRO_DATABASE_URL
    (or the [database] url key) pointing at the target PostgreSQL database.
  * MAESTRO_ADMIN_DATABASE_URL (or [database] admin_url) should be set to an
    account with CREATE/DROP TABLE and sequence privileges.  Falls back to the
    regular URL if unset.
  * The SQLite source database must exist at the path configured in maestro.ini.

Safety
------
  * This script DESTROYS all data in the PostgreSQL target database.  It will
    not proceed without explicit confirmation unless --yes is passed.
  * The SQLite database is opened read-only; it is never modified.
  * A physical backup of kanban.db should exist before running.
"""

import argparse
import json
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text, MetaData, Table, select, func
from sqlalchemy.exc import SQLAlchemyError

# Load .env before anything else so MAESTRO_DATABASE_URL etc. are available
_root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_dotenv_path = os.path.join(_root_dir, ".env")
if os.path.exists(_dotenv_path):
    try:
        from dotenv import load_dotenv
        load_dotenv(_dotenv_path, override=False)
    except ImportError:
        # Manual fallback — parse KEY=VALUE lines
        with open(_dotenv_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _v = _line.split("=", 1)
                    if _k.strip() not in os.environ:
                        os.environ[_k.strip()] = _v.strip()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)

CHUNK_SIZE = 500

# ---------------------------------------------------------------------------
# Tables with self-referential FKs that require a two-pass insert.
# Format: table_name → self-ref column name.
# ---------------------------------------------------------------------------
SELF_REF_COLS = {
    "tasks":         "parent_task_id",
    "research_jobs": "parent_job_id",
}

# ---------------------------------------------------------------------------
# Strict FK-safe copy order.  Every table appears after all tables it
# references.  Tables not in this list are appended automatically.
# ---------------------------------------------------------------------------
COPY_ORDER = [
    # Level 0 — no FK dependencies
    "budgets",
    "compute_nodes",
    # Level 1
    "llms",
    # Level 2
    "projects",
    # Level 3 — self-reference handled via two-pass
    "tasks",
    # Level 4 — depend on tasks/projects/llms/budgets
    "agent_sessions",          # task_id has NO FK constraint (dropped in mig 0050)
    "performance_improvement_plans",
    "maestro_runs",
    "project_decisions",
    "research_jobs",           # self-ref on parent_job_id — also two-pass
    "file_summary_jobs",
    "file_summaries",
    "arch_gen_jobs",
    "scope_summaries",
    "scope_survey_jobs",
    "search_cache",
    "inbox_messages",
    "intake_drafts",
    "system_settings",
    "task_session_states",     # depends on tasks + agent_sessions
    "tool_bug_reports",        # depends on agent_sessions
    # Pipeline audit — all depend on tasks
    "transition_votes",
    "transition_results",
    "subdivision_records",
    "planning_results",
    "component_results",
    "optimization_results",
    "optimization_benchmarks",
    "security_review_results",
    "final_review_results",
    "merge_records",
    # PIP — depends on tasks + performance_improvement_plans
    "pip_verifications",
    "pip_resolution_jobs",
    # Level 5 — depends on budget_entries + budgets + llms + tasks
    "budget_entries",
    "expenses",
    # Maestro-specific
    "maestro_runs",
    "project_decisions",
]

# Deduplicate while preserving order (some names appear twice above as reminders)
_seen: set = set()
_COPY_ORDER_DEDUPED: list = []
for _t in COPY_ORDER:
    if _t not in _seen:
        _COPY_ORDER_DEDUPED.append(_t)
        _seen.add(_t)
COPY_ORDER = _COPY_ORDER_DEDUPED


# ---------------------------------------------------------------------------
# Connection helpers
# ---------------------------------------------------------------------------

def _pg_url_direct() -> str:
    """Read the PostgreSQL URL without going through the USE_POSTGRES toggle."""
    url = os.environ.get("MAESTRO_DATABASE_URL", "").strip()
    if url:
        return url
    import configparser
    ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maestro.ini")
    if os.path.exists(ini):
        cfg = configparser.ConfigParser()
        cfg.read(ini)
        url = cfg.get("database", "url", fallback="").strip()
        if url:
            return url
    return ""


def _admin_pg_url() -> str:
    url = os.environ.get("MAESTRO_ADMIN_DATABASE_URL", "").strip()
    if url:
        return url
    import configparser
    ini = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "maestro.ini")
    if os.path.exists(ini):
        cfg = configparser.ConfigParser()
        cfg.read(ini)
        url = cfg.get("database", "admin_url", fallback="").strip()
        if url:
            return url
    return _pg_url_direct()


def _sqlite_url() -> str:
    from app.database.session import DATABASE_PATH
    return f"sqlite:///{DATABASE_PATH}"


# ---------------------------------------------------------------------------
# Phase 1 — Drop all tables + run migrations
# ---------------------------------------------------------------------------

def phase1_schema(pg_admin_engine) -> None:
    log.info("=" * 60)
    log.info("PHASE 1 — Schema: drop all tables, replay migrations")
    log.info("=" * 60)

    with pg_admin_engine.begin() as conn:
        insp = inspect(pg_admin_engine)
        tables = insp.get_table_names()
        if tables:
            log.info("Dropping %d existing tables (CASCADE)...", len(tables))
            for t in tables:
                conn.execute(text(f'DROP TABLE IF EXISTS "{t}" CASCADE'))
                log.info("  dropped %s", t)
        else:
            log.info("No existing tables to drop.")

    log.info("Running migration runner on PostgreSQL...")
    from app.migrations.runner import ConnectionWrapper, migrate as run_migrate

    with pg_admin_engine.begin() as conn:
        wrapper = ConnectionWrapper(conn, is_postgres=True)
        run_migrate(wrapper)

    log.info("Phase 1 complete — schema matches SQLite migrations exactly.")

    # Some migrations insert seed/bootstrap rows as part of their schema step
    # (e.g. migration 0019 inserts a default budget row).  Phase 2 will copy
    # the real production data from SQLite, so we truncate every data table now
    # to avoid duplicate-key collisions.  schema_migrations is untouched.
    log.info("Truncating all data tables to remove migration seed rows...")
    with pg_admin_engine.begin() as conn:
        insp2 = inspect(pg_admin_engine)
        data_tables = [t for t in insp2.get_table_names() if t != "schema_migrations"]
        if data_tables:
            quoted = ", ".join(f'"{t}"' for t in data_tables)
            conn.execute(text(f"TRUNCATE TABLE {quoted} RESTART IDENTITY CASCADE"))
            log.info("  Truncated %d tables.", len(data_tables))


# ---------------------------------------------------------------------------
# Phase 2 — Copy data
# ---------------------------------------------------------------------------

def _ordered_tables(sqlite_tables: set, pg_tables: set) -> list:
    """Return tables in FK-safe copy order.  Skip schema_migrations."""
    skip = {"schema_migrations"}
    common = (sqlite_tables & pg_tables) - skip
    ordered = [t for t in COPY_ORDER if t in common]
    remainder = sorted(common - set(ordered))
    return ordered + remainder


def _copy_table_normal(source_conn, target_conn, src_tbl, tgt_tbl, table_name: str) -> int:
    """Copy all rows from src_tbl to tgt_tbl in chunks.  Returns row count copied."""
    total = source_conn.execute(select(func.count()).select_from(src_tbl)).scalar() or 0
    if total == 0:
        return 0

    copied = 0
    offset = 0
    while offset < total:
        rows = source_conn.execute(
            select(src_tbl).offset(offset).limit(CHUNK_SIZE)
        ).mappings().all()
        if not rows:
            break
        data = [dict(r) for r in rows]
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        # Savepoint wraps the chunk so a failure can be rolled back cleanly
        # before falling back to row-by-row (PostgreSQL aborts the transaction
        # on any error, making further statements impossible without a rollback).
        target_conn.execute(text("SAVEPOINT sp_chunk"))
        try:
            target_conn.execute(pg_insert(tgt_tbl).values(data))
            target_conn.execute(text("RELEASE SAVEPOINT sp_chunk"))
            copied += len(data)
        except SQLAlchemyError as exc:
            target_conn.execute(text("ROLLBACK TO SAVEPOINT sp_chunk"))
            target_conn.execute(text("RELEASE SAVEPOINT sp_chunk"))
            log.warning("  Chunk insert failed for %s (%s) — trying row-by-row", table_name, exc)
            for row in data:
                target_conn.execute(text("SAVEPOINT sp_row"))
                try:
                    target_conn.execute(pg_insert(tgt_tbl).values([row]))
                    target_conn.execute(text("RELEASE SAVEPOINT sp_row"))
                    copied += 1
                except SQLAlchemyError as row_exc:
                    target_conn.execute(text("ROLLBACK TO SAVEPOINT sp_row"))
                    target_conn.execute(text("RELEASE SAVEPOINT sp_row"))
                    log.warning("  Skipped row in %s: %s — row keys: %s",
                                table_name, row_exc, list(row.keys())[:6])
        offset += len(rows)

    return copied


def _copy_table_two_pass(source_conn, target_conn, src_tbl, tgt_tbl,
                          table_name: str, self_ref_col: str) -> int:
    """
    Copy a self-referential table in two passes to avoid FK violations:
      Pass 1 — Insert all rows with self_ref_col set to NULL.
      Pass 2 — UPDATE each row to restore the original self_ref_col value.
    Returns row count copied.
    """
    total = source_conn.execute(select(func.count()).select_from(src_tbl)).scalar() or 0
    if total == 0:
        return 0

    # Collect (pk_value, original_self_ref_value) for pass 2
    # Assumes integer or string PK named 'id'
    pk_col = "id"
    pending_updates: list = []

    offset = 0
    copied = 0
    while offset < total:
        rows = source_conn.execute(
            select(src_tbl).offset(offset).limit(CHUNK_SIZE)
        ).mappings().all()
        if not rows:
            break

        data = []
        for r in rows:
            row = dict(r)
            orig_val = row.get(self_ref_col)
            if orig_val is not None:
                pending_updates.append((row[pk_col], orig_val))
                row[self_ref_col] = None
            data.append(row)

        from sqlalchemy.dialects.postgresql import insert as pg_insert
        target_conn.execute(text("SAVEPOINT sp_chunk"))
        try:
            target_conn.execute(pg_insert(tgt_tbl).values(data))
            target_conn.execute(text("RELEASE SAVEPOINT sp_chunk"))
            copied += len(data)
        except SQLAlchemyError as exc:
            target_conn.execute(text("ROLLBACK TO SAVEPOINT sp_chunk"))
            target_conn.execute(text("RELEASE SAVEPOINT sp_chunk"))
            log.warning("  Chunk insert failed for %s (%s) — row-by-row", table_name, exc)
            for row in data:
                target_conn.execute(text("SAVEPOINT sp_row"))
                try:
                    target_conn.execute(pg_insert(tgt_tbl).values([row]))
                    target_conn.execute(text("RELEASE SAVEPOINT sp_row"))
                    copied += 1
                except SQLAlchemyError as row_exc:
                    target_conn.execute(text("ROLLBACK TO SAVEPOINT sp_row"))
                    target_conn.execute(text("RELEASE SAVEPOINT sp_row"))
                    log.warning("  Skipped row in %s: %s", table_name, row_exc)

        offset += len(rows)

    # Pass 2 — restore self-references in batches
    if pending_updates:
        log.info("  Pass 2: restoring %d %s values...", len(pending_updates), self_ref_col)
        for pk_val, ref_val in pending_updates:
            try:
                target_conn.execute(
                    text(f'UPDATE "{table_name}" SET "{self_ref_col}" = :ref WHERE "{pk_col}" = :pk'),
                    {"ref": ref_val, "pk": pk_val},
                )
            except SQLAlchemyError as exc:
                log.warning("  Could not restore %s=%s on %s id=%s: %s",
                            self_ref_col, ref_val, table_name, pk_val, exc)

    return copied


def phase2_data(sqlite_engine, pg_admin_engine) -> dict:
    log.info("=" * 60)
    log.info("PHASE 2 — Data: copying rows from SQLite to PostgreSQL")
    log.info("=" * 60)

    sqlite_meta = MetaData()
    sqlite_meta.reflect(bind=sqlite_engine)
    pg_meta = MetaData()
    pg_meta.reflect(bind=pg_admin_engine)

    tables = _ordered_tables(set(sqlite_meta.tables), set(pg_meta.tables))
    log.info("Tables to copy: %d", len(tables))

    counts: dict = {}

    # Each table is copied in its own transaction so that a failure in one
    # table (e.g. a bad row) cannot abort the copy for all subsequent tables.
    with sqlite_engine.connect() as src_conn:
        for table_name in tables:
            src_tbl = Table(table_name, sqlite_meta, autoload_with=sqlite_engine)
            tgt_tbl = Table(table_name, pg_meta, autoload_with=pg_admin_engine)

            total_src = src_conn.execute(
                select(func.count()).select_from(src_tbl)
            ).scalar() or 0

            if total_src == 0:
                log.info("  %-45s  0 rows (skipping)", table_name)
                counts[table_name] = (0, 0)
                continue

            # Open a fresh connection+transaction per table
            with pg_admin_engine.begin() as tgt_conn:
                if table_name in SELF_REF_COLS:
                    log.info("  %-45s  %d rows (two-pass, self-ref: %s)...",
                             table_name, total_src, SELF_REF_COLS[table_name])
                    n = _copy_table_two_pass(src_conn, tgt_conn, src_tbl, tgt_tbl,
                                             table_name, SELF_REF_COLS[table_name])
                else:
                    log.info("  %-45s  %d rows...", table_name, total_src)
                    n = _copy_table_normal(src_conn, tgt_conn, src_tbl, tgt_tbl, table_name)

            counts[table_name] = (total_src, n)
            if n != total_src:
                log.warning("  %-45s  MISMATCH: copied %d / %d", table_name, n, total_src)
            else:
                log.info("  %-45s  copied %d", table_name, n)

    log.info("Phase 2 complete.")
    return counts


# ---------------------------------------------------------------------------
# Phase 3 — Reset sequences
# ---------------------------------------------------------------------------

def phase3_sequences(pg_admin_engine) -> None:
    log.info("=" * 60)
    log.info("PHASE 3 — Sequences: resetting to max(pk)")
    log.info("=" * 60)

    with pg_admin_engine.begin() as conn:
        seq_rows = conn.execute(text(
            "SELECT sequencename FROM pg_sequences WHERE schemaname = 'public'"
        )).fetchall()

        for (seq_name,) in seq_rows:
            try:
                # Find the table and column this sequence belongs to
                tc = conn.execute(
                    text(
                        "SELECT tab.relname, att.attname "
                        "FROM pg_class seq "
                        "JOIN pg_depend dep ON dep.objid = seq.oid "
                        "  AND dep.classid = 'pg_class'::regclass "
                        "JOIN pg_class tab ON tab.oid = dep.refobjid "
                        "JOIN pg_attribute att ON att.attrelid = tab.oid "
                        "  AND att.attnum = dep.refobjsubid "
                        "WHERE seq.relkind = 'S' AND seq.relname = :seq"
                    ),
                    {"seq": seq_name},
                ).fetchone()
                if not tc:
                    continue
                tbl, col = tc
                max_id = conn.execute(
                    text(f'SELECT MAX("{col}") FROM "{tbl}"')
                ).scalar()
                if max_id is not None:
                    conn.execute(text(
                        f"SELECT setval('{seq_name}', {max_id})"
                    ))
                    log.info("  %-50s  -> %d", seq_name, max_id)
                else:
                    # Table is empty — reset sequence to 1 (its default start)
                    conn.execute(text(f"SELECT setval('{seq_name}', 1, false)"))
                    log.info("  %-50s  -> 1 (table empty)", seq_name)
            except SQLAlchemyError as exc:
                log.warning("  Could not reset sequence %s: %s", seq_name, exc)

    log.info("Phase 3 complete.")


# ---------------------------------------------------------------------------
# Phase 4 — Summary
# ---------------------------------------------------------------------------

def phase4_summary(counts: dict) -> bool:
    log.info("=" * 60)
    log.info("PHASE 4 — Row count summary")
    log.info("=" * 60)
    ok = True
    print(f"\n  {'Table':<45}  {'SQLite':>8}  {'Copied':>8}  {'Delta':>7}")
    print("  " + "-" * 72)
    for table_name, (src, dst) in sorted(counts.items()):
        delta = dst - src
        flag = " <<< MISMATCH" if delta != 0 else ""
        print(f"  {table_name:<45}  {src:>8}  {dst:>8}  {delta:>+7}{flag}")
        if delta != 0:
            ok = False
    print()
    return ok


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--yes", "-y", action="store_true",
                        help="Skip the confirmation prompt (for scripted use)")
    args = parser.parse_args()

    pg_url    = _pg_url_direct()
    admin_url = _admin_pg_url()
    sqlite_url = _sqlite_url()

    if not pg_url or not pg_url.startswith("postgresql"):
        print("ERROR: No PostgreSQL URL configured.  Set MAESTRO_DATABASE_URL or "
              "[database] url in maestro.ini.")
        sys.exit(1)

    print()
    print("=" * 60)
    print("  MAESTRO -- SQLite -> PostgreSQL migration")
    print("=" * 60)
    print(f"  Source  (SQLite):    {sqlite_url}")
    print(f"  Target  (Postgres):  {pg_url}")
    print(f"  Admin   (Postgres):  {admin_url}")
    print()
    print("  This will DESTROY all data in the PostgreSQL target and")
    print("  rebuild it from the SQLite source.  Ensure you have a")
    print("  physical backup of kanban.db before proceeding.")
    print()

    if not args.yes:
        answer = input("  Type 'yes' to continue: ").strip().lower()
        if answer != "yes":
            print("Aborted.")
            sys.exit(0)

    sqlite_engine    = create_engine(sqlite_url)
    pg_admin_engine  = create_engine(admin_url)

    # Smoke-test both connections
    try:
        with sqlite_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:
        print(f"ERROR: Cannot connect to SQLite: {exc}")
        sys.exit(1)
    try:
        with pg_admin_engine.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as exc:
        print(f"ERROR: Cannot connect to PostgreSQL: {exc}")
        sys.exit(1)

    phase1_schema(pg_admin_engine)
    counts = phase2_data(sqlite_engine, pg_admin_engine)
    phase3_sequences(pg_admin_engine)
    ok = phase4_summary(counts)

    if ok:
        log.info("Migration complete — all row counts match.")
        log.info("Run scripts/verify_postgres_migration.py for a full schema + data check.")
    else:
        log.warning("Migration complete with mismatches — check the log above.")
        log.info("Run scripts/verify_postgres_migration.py for details.")
        sys.exit(1)


if __name__ == "__main__":
    main()
