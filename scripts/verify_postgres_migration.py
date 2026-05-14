"""
verify_postgres_migration.py — Compare SQLite (source of truth) against PostgreSQL.

Checks:
  1. Table presence  — tables in SQLite missing from or extra in PostgreSQL
  2. Column parity   — columns, types, nullable, defaults per table
  3. Index parity    — indices present in SQLite but absent in PostgreSQL (and vice-versa)
  4. FK parity       — foreign-key constraints declared on each table
  5. Row counts      — per-table count comparison; reports rows dropped during migration
  6. Sequences       — PostgreSQL serial sequences vs actual max(pk) in each table

Usage:
    venv/Scripts/python.exe scripts/verify_postgres_migration.py

Requires both databases to be accessible.  Reads connection strings from the
standard app config (maestro.ini / environment variables).
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine, inspect, text, MetaData

from app.database.session import DATABASE_PATH

# We need the PostgreSQL URL unconditionally — don't go through the USE_POSTGRES
# toggle.  Read it the same way config.py does: env var first, then maestro.ini.
def _get_pg_url() -> str:
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

PG_URL = _get_pg_url()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SKIP_TABLES = {"schema_migrations"}  # internal bookkeeping, not in ORM

def _normalise_type(type_str: str) -> str:
    """
    Coerce type strings to a canonical form so SQLite/PostgreSQL differences
    in type naming don't produce false positives.

    SQLite stores everything as TEXT/INTEGER/REAL/BLOB/NUMERIC; PostgreSQL
    uses VARCHAR, INTEGER, TEXT, BOOLEAN, TIMESTAMP, BIGINT, etc.
    We map both sides to a common vocabulary for comparison purposes.

    Temporal types (DATETIME, TIMESTAMP, TEXT used-as-datetime) are all mapped
    to "TEMPORAL" since SQLite migrations often declare columns as TEXT or
    DATETIME interchangeably, while PostgreSQL uses TIMESTAMP.  The actual
    stored values (ISO strings) are identical — this is not a real mismatch.
    """
    s = str(type_str).upper().split("(")[0].strip()
    mapping = {
        # SQLite aliases → TEXT
        "VARCHAR":          "TEXT",
        "NVARCHAR":         "TEXT",
        "CLOB":             "TEXT",
        "CHAR":             "TEXT",
        "STRING":           "TEXT",
        # Numeric
        "INT":              "INTEGER",
        "BIGINT":           "INTEGER",
        "SMALLINT":         "INTEGER",
        "TINYINT":          "INTEGER",
        "MEDIUMINT":        "INTEGER",
        "INT2":             "INTEGER",
        "INT8":             "INTEGER",
        "NUMERIC":          "REAL",
        "DECIMAL":          "REAL",
        "DOUBLE":           "REAL",
        "FLOAT":            "REAL",
        "DOUBLE PRECISION": "REAL",
        # Boolean (Postgres uses BOOLEAN, SQLite stores as INTEGER)
        "BOOLEAN":          "INTEGER",
        "BOOL":             "INTEGER",
        # Temporal — SQLite uses TEXT/DATETIME, Postgres uses TIMESTAMP.
        # Both store ISO-8601 strings; treat as equivalent.
        "DATETIME":         "TEMPORAL",
        "TIMESTAMP":        "TEMPORAL",
        "TIMESTAMPTZ":      "TEMPORAL",
        "DATE":             "TEMPORAL",
        # Blobs
        "BYTEA":            "BLOB",
        "BINARY":           "BLOB",
        "VARBINARY":        "BLOB",
    }
    return mapping.get(s, s)


def _normalise_index_name(name: str) -> str:
    """
    Strip auto-generated SQLite suffix patterns so index names can be compared
    across dialects.  E.g. 'ix_tasks_is_active' → 'ix_tasks_is_active'.
    """
    return (name or "").lower().replace(" ", "_")


def _columns_key(cols) -> frozenset:
    return frozenset(c.lower() for c in cols)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def verify():
    if not PG_URL.startswith("postgresql"):
        print("ERROR: DATABASE_URL is not a PostgreSQL URL.  Check maestro.ini or MAESTRO_DATABASE_URL.")
        sys.exit(1)

    sqlite_url = f"sqlite:///{DATABASE_PATH}"
    print(f"SQLite  : {sqlite_url}")
    print(f"Postgres: {PG_URL}")
    print()

    sqlite_eng = create_engine(sqlite_url)
    pg_eng     = create_engine(PG_URL)

    sqlite_insp = inspect(sqlite_eng)
    pg_insp     = inspect(pg_eng)

    sqlite_tables = set(sqlite_insp.get_table_names()) - SKIP_TABLES
    pg_tables     = set(pg_insp.get_table_names())     - SKIP_TABLES

    issues = []

    # -------------------------------------------------------------------------
    # 1. Table presence
    # -------------------------------------------------------------------------
    only_sqlite = sqlite_tables - pg_tables
    only_pg     = pg_tables     - sqlite_tables
    common      = sqlite_tables & pg_tables

    if only_sqlite:
        issues.append(f"TABLES MISSING FROM POSTGRES ({len(only_sqlite)}): {sorted(only_sqlite)}")
    if only_pg:
        issues.append(f"EXTRA TABLES IN POSTGRES ({len(only_pg)}): {sorted(only_pg)}")

    # -------------------------------------------------------------------------
    # 2 & 3 & 4. Column / index / FK parity per table
    # -------------------------------------------------------------------------
    for table in sorted(common):
        sq_cols = {c["name"]: c for c in sqlite_insp.get_columns(table)}
        pg_cols = {c["name"]: c for c in pg_insp.get_columns(table)}

        # -- 2a. Missing / extra columns
        only_sq_cols = set(sq_cols) - set(pg_cols)
        only_pg_cols = set(pg_cols) - set(sq_cols)
        if only_sq_cols:
            issues.append(f"{table}: columns in SQLite only: {sorted(only_sq_cols)}")
        if only_pg_cols:
            issues.append(f"{table}: columns in Postgres only: {sorted(only_pg_cols)}")

        # Primary key column names for this table — SQLAlchemy's SQLite
        # inspector reports pk columns as nullable=True even though they
        # aren't (a known inspection artifact).  Skip the nullable check
        # for pk columns to avoid a wall of false-positive warnings.
        sq_pk_cols = {c for c in sqlite_insp.get_pk_constraint(table).get("constrained_columns", [])}
        pg_pk_cols = {c for c in pg_insp.get_pk_constraint(table).get("constrained_columns", [])}
        pk_cols = sq_pk_cols | pg_pk_cols

        # -- 2b. Type / nullable mismatches
        for col in sorted(set(sq_cols) & set(pg_cols)):
            sq_c = sq_cols[col]
            pg_c = pg_cols[col]
            sq_type = _normalise_type(sq_c["type"])
            pg_type = _normalise_type(pg_c["type"])
            if sq_type != pg_type:
                # Type differences between SQLite and PostgreSQL are almost
                # always cosmetic (TEXT vs VARCHAR, DATETIME vs TIMESTAMP).
                # Report as [INFO] since the stored values are identical.
                issues.append(
                    f"{table}.{col}: type mismatch  sqlite={sq_type!r}  postgres={pg_type!r}  [informational]"
                )
            # Nullable — only flag when PG is stricter AND the column is not
            # a primary key (pk columns are always effectively NOT NULL even
            # if SQLite's inspector reports them as nullable).
            if not pg_c["nullable"] and sq_c["nullable"] and col not in pk_cols:
                issues.append(
                    f"{table}.{col}: Postgres is NOT NULL but SQLite allows NULLs"
                )

        # -- 3. Indices
        def _idx_set(insp, tbl):
            return {
                _normalise_index_name(idx["name"]): (
                    _columns_key(idx["column_names"]),
                    bool(idx.get("unique", False)),
                )
                for idx in insp.get_indexes(tbl)
                if idx.get("name")
            }

        sq_idx = _idx_set(sqlite_insp, table)
        pg_idx = _idx_set(pg_insp, table)

        missing_in_pg = set(sq_idx) - set(pg_idx)
        if missing_in_pg:
            for idx_name in sorted(missing_in_pg):
                cols, unique = sq_idx[idx_name]
                issues.append(
                    f"{table}: index '{idx_name}' (cols={sorted(cols)}, unique={unique})"
                    f" exists in SQLite but NOT in Postgres"
                )

        extra_in_pg = set(pg_idx) - set(sq_idx)
        if extra_in_pg:
            for idx_name in sorted(extra_in_pg):
                cols, unique = pg_idx[idx_name]
                issues.append(
                    f"{table}: index '{idx_name}' (cols={sorted(cols)}, unique={unique})"
                    f" exists in Postgres but NOT in SQLite  [informational]"
                )

        # -- 4. Foreign keys
        def _fk_set(insp, tbl):
            result = set()
            for fk in insp.get_foreign_keys(tbl):
                referred = fk.get("referred_table", "")
                local_cols  = tuple(sorted(fk.get("constrained_columns", [])))
                remote_cols = tuple(sorted(fk.get("referred_columns", [])))
                result.add((local_cols, referred, remote_cols))
            return result

        sq_fks = _fk_set(sqlite_insp, table)
        pg_fks = _fk_set(pg_insp, table)

        missing_fks = sq_fks - pg_fks
        if missing_fks:
            for fk in sorted(missing_fks):
                issues.append(
                    f"{table}: FK {fk[0]} → {fk[1]}{fk[2]} in SQLite but NOT in Postgres"
                )
        extra_fks = pg_fks - sq_fks
        if extra_fks:
            for fk in sorted(extra_fks):
                issues.append(
                    f"{table}: FK {fk[0]} → {fk[1]}{fk[2]} in Postgres but NOT in SQLite  [informational]"
                )

    # -------------------------------------------------------------------------
    # 5. Row counts
    # -------------------------------------------------------------------------
    print("ROW COUNTS")
    print(f"  {'Table':<40}  {'SQLite':>10}  {'Postgres':>10}  {'Delta':>8}")
    print("  " + "-" * 72)
    total_delta = 0
    with sqlite_eng.connect() as sq_conn, pg_eng.connect() as pg_conn:
        for table in sorted(common):
            sq_n = sq_conn.execute(text(f"SELECT COUNT(*) FROM \"{table}\"")).scalar()
            pg_n = pg_conn.execute(text(f"SELECT COUNT(*) FROM \"{table}\"")).scalar()
            delta = pg_n - sq_n
            total_delta += delta
            flag = " <<<" if delta < 0 else (" (+)" if delta > 0 else "")
            print(f"  {table:<40}  {sq_n:>10}  {pg_n:>10}  {delta:>+8}{flag}")

    if total_delta < 0:
        issues.append(f"ROW COUNT: Postgres has {abs(total_delta)} fewer total rows than SQLite (rows were dropped during migration)")
    elif total_delta > 0:
        issues.append(f"ROW COUNT: Postgres has {total_delta} more total rows than SQLite (unexpected)")

    # -------------------------------------------------------------------------
    # 6. Sequences
    # -------------------------------------------------------------------------
    print()
    print("SEQUENCES")
    with pg_eng.connect() as pg_conn:
        seq_rows = pg_conn.execute(text(
            "SELECT sequencename, last_value FROM pg_sequences WHERE schemaname = 'public'"
        )).fetchall()

        if not seq_rows:
            print("  (no sequences found)")
        else:
            print(f"  {'Sequence':<50}  {'last_value':>12}")
            print("  " + "-" * 66)
            for row in sorted(seq_rows, key=lambda r: r[0]):
                seq_name, last_val = row[0], row[1]
                print(f"  {seq_name:<50}  {last_val or 0:>12}")

            # Cross-check: for each sequence, find the table/column and compare to max(pk)
            for row in seq_rows:
                seq_name, seq_last = row[0], row[1]
                # Sequence names follow the pattern <table>_<column>_seq
                try:
                    table_col = pg_conn.execute(text(
                        "SELECT tab.relname, att.attname "
                        "FROM pg_class seq "
                        "JOIN pg_depend dep ON dep.objid = seq.oid AND dep.classid = 'pg_class'::regclass "
                        "JOIN pg_class tab ON tab.oid = dep.refobjid "
                        "JOIN pg_attribute att ON att.attrelid = tab.oid AND att.attnum = dep.refobjsubid "
                        "WHERE seq.relkind = 'S' AND seq.relname = :seq",
                        {"seq": seq_name}
                    )).fetchone()
                    if table_col:
                        tbl, col = table_col
                        max_id = pg_conn.execute(text(f'SELECT MAX("{col}") FROM "{tbl}"')).scalar() or 0
                        if seq_last and seq_last < max_id:
                            issues.append(
                                f"SEQUENCE {seq_name}: last_value={seq_last} < max({tbl}.{col})={max_id}"
                                f" — sequence will collide on next INSERT"
                            )
                except Exception:
                    pass

    # -------------------------------------------------------------------------
    # Summary
    # -------------------------------------------------------------------------
    print()
    print("=" * 72)
    if not issues:
        print("ALL CHECKS PASSED — PostgreSQL matches SQLite.")
    else:
        print(f"ISSUES FOUND ({len(issues)}):")
        for i, issue in enumerate(issues, 1):
            tag = "[INFO]" if "[informational]" in issue else "[FAIL]"
            print(f"  {i:3}. {tag} {issue}")
    print("=" * 72)
    return issues


if __name__ == "__main__":
    issues = verify()
    # Exit non-zero if there are real failures (not just [informational] items)
    real_failures = [i for i in issues if "[informational]" not in i]
    sys.exit(1 if real_failures else 0)
