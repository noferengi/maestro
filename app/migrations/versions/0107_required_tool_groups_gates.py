description = "required_tool_groups: gate Software Dev indev/security and fix Bug Triage regression_test"

import json

# Stage IDs targeted (unique, confirmed against live DB):
#   4  — indev        (Software Development)
#   7  — security     (Software Development)
#   56 — regression_test (Bug Triage)

_INDEV_CONFIG = {
    "required_tool_groups": [
        ["run_test_pytest", "run_test_unittest", "run_test_cargo", "run_test_go", "run_test_npm"],
    ],
}

_SECURITY_CONFIG = {
    "required_tool_groups": [
        ["run_audit_bandit", "run_audit_pip", "run_audit_semgrep", "run_audit_npm"],
    ],
}

# regression_test previously used the legacy "verifier": "run_pytest" key (migration 0073).
# That field is a subprocess verifier name, not a tool-success gate.
# Replace with the correct required_tool_successes field.
_REGRESSION_TEST_CONFIG = {
    "required_tool_successes": ["run_test_pytest"],
}

_REGRESSION_TEST_OLD_CONFIG = {
    "verifier": "run_pytest",
}


def up(conn) -> None:
    # indev — no prior config; set directly
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE id = :id",
        {"cfg": json.dumps(_INDEV_CONFIG), "id": 4},
    )
    # security — no prior config; set directly
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE id = :id",
        {"cfg": json.dumps(_SECURITY_CONFIG), "id": 7},
    )
    # regression_test — replace legacy verifier key
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE id = :id",
        {"cfg": json.dumps(_REGRESSION_TEST_CONFIG), "id": 56},
    )


def down(conn) -> None:
    conn.execute(
        "UPDATE pipeline_stages SET config = NULL WHERE id = :id",
        {"id": 4},
    )
    conn.execute(
        "UPDATE pipeline_stages SET config = NULL WHERE id = :id",
        {"id": 7},
    )
    conn.execute(
        "UPDATE pipeline_stages SET config = :cfg WHERE id = :id",
        {"cfg": json.dumps(_REGRESSION_TEST_OLD_CONFIG), "id": 56},
    )
