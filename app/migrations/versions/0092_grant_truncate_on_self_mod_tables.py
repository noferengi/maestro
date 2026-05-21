description = "Grant full DML privileges on self-mod tables to app users"

# The default ACL for tables created by maestro_admin does not include TRUNCATE
# for maestro_user. The conftest TRUNCATE fixture needs it. This migration
# explicitly grants all required privileges to both the prod and test app users.

_TABLES = ["revert_votes", "self_mod_merge_log"]
_ROLES = ["maestro_user", "maestro_test"]  # prod app user + test app user


def up(conn):
    for table in _TABLES:
        for role in _ROLES:
            try:
                conn.execute(
                    f"GRANT SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} TO {role}"
                )
            except Exception:
                # Role may not exist in this DB (e.g. maestro_test not in prod)
                pass


def down(conn):
    for table in _TABLES:
        for role in _ROLES:
            try:
                conn.execute(
                    f"REVOKE SELECT, INSERT, UPDATE, DELETE, TRUNCATE ON {table} FROM {role}"
                )
            except Exception:
                pass
