description = "Set ALTER DEFAULT PRIVILEGES FOR ROLE maestro_admin so all future tables/sequences are auto-accessible to app users"

# Previously these grants were run as the postgres superuser (no FOR ROLE clause),
# which only covers objects created by postgres — not maestro_admin. Since the
# migration runner connects as maestro_admin, every CREATE TABLE is owned by
# maestro_admin and the old grants never fired. This migration fixes that.
#
# After this runs, no per-table GRANT migrations (like 0092/0093) are needed.

_GRANTS = [
    ("maestro_user", "maestro_test"),
]


def up(conn):
    for role in ["maestro_user", "maestro_test"]:
        try:
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE maestro_admin IN SCHEMA public "
                f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES TO {role}"
            )
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE maestro_admin IN SCHEMA public "
                f"GRANT USAGE, SELECT ON SEQUENCES TO {role}"
            )
        except Exception:
            pass  # role may not exist in this DB (e.g. maestro_test not in prod)


def down(conn):
    for role in ["maestro_user", "maestro_test"]:
        try:
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE maestro_admin IN SCHEMA public "
                f"REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON TABLES FROM {role}"
            )
            conn.execute(
                f"ALTER DEFAULT PRIVILEGES FOR ROLE maestro_admin IN SCHEMA public "
                f"REVOKE USAGE, SELECT ON SEQUENCES FROM {role}"
            )
        except Exception:
            pass
