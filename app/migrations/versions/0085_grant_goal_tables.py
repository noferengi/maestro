description = "Grant DML privileges on goal tables; document default-privilege policy"

# ALTER DEFAULT PRIVILEGES cannot be run inside a transaction in all drivers,
# and must be applied manually by a superuser once per database:
#
#   -- Production DB (run as superuser):
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public
#     GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO maestro_user;
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public
#     GRANT USAGE, SELECT ON SEQUENCES TO maestro_user;
#
#   -- Test DB (run as superuser):
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public
#     GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO maestro_test;
#   ALTER DEFAULT PRIVILEGES IN SCHEMA public
#     GRANT USAGE, SELECT ON SEQUENCES TO maestro_test;
#
# After that one-time setup, all future tables and sequences created by the
# migration runner will be automatically accessible — no per-migration grants needed.
#
# This migration handles the explicit back-fill for tables created before
# default privileges were configured.

_TABLES = ["maestro_goals", "goal_verification_jobs"]


def up(conn):
    for table in _TABLES:
        conn.execute(
            f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} TO maestro_user"
        )


def down(conn):
    for table in _TABLES:
        conn.execute(
            f"REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM maestro_user"
        )
