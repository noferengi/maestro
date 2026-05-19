"""
Schema drift detection — PostgreSQL edition.

Verifies that the live test database schema (produced by running all migrations)
matches the SQLAlchemy ORM model declarations.

A failure here means either:
  - A migration was added without updating the corresponding ORM model, or
  - An ORM model was changed/added without a corresponding migration.

Uses the conftest-managed test PostgreSQL database (already migrated).
No SQLite; no create_all; no extra connections needed.
"""

from sqlalchemy import inspect, text


def test_orm_matches_migrations():
    from app.database.session import Base, engine
    from app.database import models  # noqa: F401 — registers all models on Base

    insp = inspect(engine)

    # Tables present in the live (migrated) database
    db_tables = set(insp.get_table_names()) - {"schema_migrations"}

    # Tables declared in ORM models
    orm_tables = set(Base.metadata.tables.keys())

    assert db_tables == orm_tables, (
        f"Table set mismatch:\n"
        f"  in DB only:  {sorted(db_tables - orm_tables)}\n"
        f"  in ORM only: {sorted(orm_tables - db_tables)}"
    )

    column_mismatches = []
    for table in sorted(db_tables & orm_tables):
        db_cols = {c["name"] for c in insp.get_columns(table)}
        orm_cols = {c.name for c in Base.metadata.tables[table].columns}
        if db_cols != orm_cols:
            column_mismatches.append(
                f"  {table!r}:\n"
                f"    DB only:  {sorted(db_cols - orm_cols)}\n"
                f"    ORM only: {sorted(orm_cols - db_cols)}"
            )

    assert not column_mismatches, (
        "Column mismatches found:\n" + "\n".join(column_mismatches)
    )
