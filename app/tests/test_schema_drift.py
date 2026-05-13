"""
Schema drift detection.

Verifies that running all 65 migrations on a fresh database produces the
same table/column structure as declaring all SQLAlchemy ORM models.

A failure here means either:
  - A migration was added without updating the corresponding ORM model, or
  - An ORM model was changed/added without a corresponding migration being
    written.
"""

from sqlalchemy import create_engine, inspect


def test_orm_matches_migrations():
    from app.database.session import Base
    from app.database import models  # noqa: F401 — registers all models on Base
    from migrations.runner import migrate as run_migrate, ConnectionWrapper

    # Schema A: produced by replaying all migrations in order
    eng_mig = create_engine("sqlite:///:memory:")
    with eng_mig.begin() as conn:
        run_migrate(ConnectionWrapper(conn, is_postgres=False))

    # Schema B: produced by ORM model declarations alone
    eng_orm = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(eng_orm)

    insp_mig = inspect(eng_mig)
    insp_orm = inspect(eng_orm)

    mig_tables = set(insp_mig.get_table_names()) - {"schema_migrations"}
    orm_tables = set(insp_orm.get_table_names())

    assert mig_tables == orm_tables, (
        f"Table set mismatch:\n"
        f"  in migrations only: {sorted(mig_tables - orm_tables)}\n"
        f"  in ORM only:        {sorted(orm_tables - mig_tables)}"
    )

    column_mismatches = []
    for table in sorted(mig_tables & orm_tables):
        mig_cols = {c["name"] for c in insp_mig.get_columns(table)}
        orm_cols = {c["name"] for c in insp_orm.get_columns(table)}
        if mig_cols != orm_cols:
            column_mismatches.append(
                f"  {table!r}:\n"
                f"    migrations only: {sorted(mig_cols - orm_cols)}\n"
                f"    ORM only:        {sorted(orm_cols - mig_cols)}"
            )

    assert not column_mismatches, (
        "Column mismatches found:\n" + "\n".join(column_mismatches)
    )
