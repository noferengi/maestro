description = "SW Dev: insert json_schema_gate and planning_correction stages between planning and indev"

import json as _json

_GATE_CONFIG = {
    "source": "planning_result",
    "required_fields": [
        {"key": "file_manifest",        "validator": "non_empty_list", "hard_fail": True},
        {"key": "implementation_steps", "validator": "non_empty_list", "hard_fail": True},
        {"key": "interface_contracts",  "validator": "non_empty_list", "hard_fail": False},
        {"key": "dependency_graph",     "validator": "valid_dag",      "hard_fail": True},
        {"key": "test_strategy",        "validator": "non_empty_list", "hard_fail": True},
    ],
    "on_pass":          "pass",
    "on_fail":          "fail",
    "max_retries":      3,
    "retry_condition":  "retry",
    "output_key":       "gate_result",
}

_CORRECTION_CONFIG = {
    "max_turns": 20,
}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id, position FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def up(conn):
    # Extend the condition check constraint to include 'retry'
    conn.execute(
        "ALTER TABLE pipeline_transitions "
        "DROP CONSTRAINT IF EXISTS pipeline_transitions_condition_check"
    )
    conn.execute(
        "ALTER TABLE pipeline_transitions "
        "ADD CONSTRAINT pipeline_transitions_condition_check "
        "CHECK (condition IN ('pass', 'fail', 'reject', 'always', 'skip', 'retry'))"
    )
    print("[0121] Extended pipeline_transitions_condition_check to include 'retry'.")

    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0121] WARNING: 'Software Development' template not found — skipping.")
        return

    planning_row = _get_stage(conn, tid, "planning")
    indev_row = _get_stage(conn, tid, "indev")
    if not planning_row or not indev_row:
        print("[0121] WARNING: planning or indev stage not found — skipping.")
        return

    planning_id = planning_row["id"]
    planning_pos = float(planning_row["position"])
    indev_pos = float(indev_row["position"])
    indev_id = indev_row["id"]

    # Position gate between planning and indev; correction between gate and indev
    gate_pos = planning_pos + (indev_pos - planning_pos) / 3.0
    correction_pos = planning_pos + 2 * (indev_pos - planning_pos) / 3.0

    # 1. Insert json_schema_gate stage
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, 'json_schema_gate', 'Plan Gate', 'json_schema_gate', :pos, CAST(:cfg AS jsonb))",
        {"tid": tid, "pos": gate_pos, "cfg": _json.dumps(_GATE_CONFIG)},
    )
    gate_row = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'json_schema_gate'",
        {"tid": tid},
    ).fetchone()
    gate_id = gate_row["id"]
    print(f"[0121] Inserted json_schema_gate stage at position {gate_pos:.4f}.")

    # 2. Insert planning_correction stage
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, 'planning_correction', 'Plan Correction', 'planning_correction_stage', "
        "        :pos, CAST(:cfg AS jsonb))",
        {"tid": tid, "pos": correction_pos, "cfg": _json.dumps(_CORRECTION_CONFIG)},
    )
    corr_row = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'planning_correction'",
        {"tid": tid},
    ).fetchone()
    corr_id = corr_row["id"]
    print(f"[0121] Inserted planning_correction stage at position {correction_pos:.4f}.")

    # 3. Delete old planning pass → indev transition
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE from_stage_id = :from_id AND to_stage_id = :to_id AND condition = 'pass'",
        {"from_id": planning_id, "to_id": indev_id},
    )

    # 4. Wire planning → json_schema_gate (pass)
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": tid, "from_id": planning_id, "to_id": gate_id},
    )

    # 5. Wire json_schema_gate transitions
    # pass → indev
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": tid, "from_id": gate_id, "to_id": indev_id},
    )
    # retry → planning_correction
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'retry', 0)",
        {"tid": tid, "from_id": gate_id, "to_id": corr_id},
    )
    # fail → planning (exhausted retries → back to planning for a full redo)
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'fail', 0)",
        {"tid": tid, "from_id": gate_id, "to_id": planning_id},
    )

    # 6. Wire planning_correction transitions
    # pass → json_schema_gate (back-edge: re-run the gate after correction)
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": tid, "from_id": corr_id, "to_id": gate_id},
    )
    # fail → planning (correction stalled → full re-plan)
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'fail', 0)",
        {"tid": tid, "from_id": corr_id, "to_id": planning_id},
    )

    print("[0121] Transitions wired: planning -> json_schema_gate -> indev (with correction back-edge).")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    planning_row = _get_stage(conn, tid, "planning")
    indev_row = _get_stage(conn, tid, "indev")
    gate_row = _get_stage(conn, tid, "json_schema_gate")
    corr_row = _get_stage(conn, tid, "planning_correction")

    gate_id = gate_row["id"] if gate_row else None
    corr_id = corr_row["id"] if corr_row else None
    planning_id = planning_row["id"] if planning_row else None
    indev_id = indev_row["id"] if indev_row else None

    # Remove inserted stages and their transitions
    for sid in (gate_id, corr_id):
        if sid:
            conn.execute(
                "DELETE FROM pipeline_transitions WHERE from_stage_id = :sid OR to_stage_id = :sid",
                {"sid": sid},
            )
            conn.execute("DELETE FROM pipeline_stages WHERE id = :sid", {"sid": sid})

    # Restore planning pass → indev
    if planning_id and indev_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
            {"tid": tid, "from_id": planning_id, "to_id": indev_id},
        )

    print("[0121 down] Removed json_schema_gate and planning_correction stages; restored planning → indev.")
