description = "Grant USAGE/SELECT on self-mod table sequences to app users"

# Companion to 0092: the sequence grants were inadvertently omitted. Without these,
# INSERT operations fail with 'permission denied for sequence revert_votes_id_seq'.

_SEQUENCES = ["revert_votes_id_seq", "self_mod_merge_log_id_seq"]
_ROLES = ["maestro_user", "maestro_test"]


def up(conn):
    for seq in _SEQUENCES:
        for role in _ROLES:
            try:
                conn.execute(f"GRANT USAGE, SELECT ON SEQUENCE {seq} TO {role}")
            except Exception:
                pass  # role may not exist in this DB


def down(conn):
    for seq in _SEQUENCES:
        for role in _ROLES:
            try:
                conn.execute(f"REVOKE USAGE, SELECT ON SEQUENCE {seq} FROM {role}")
            except Exception:
                pass
