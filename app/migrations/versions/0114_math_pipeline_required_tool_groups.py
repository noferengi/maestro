description = "Math pipeline: require run_lean4 or run_sympy before submit_work passes in CALIBRATION and COMPUTATIONAL_EXPLORATION"

import json as _json

_TEMPLATE_NAME = "Mathematics / Proof Exploration"

# Each stage gets a required_tool_groups gate: at least one of run_lean4 or
# run_sympy must succeed in the session before a pass signal is accepted.
# This prevents the agent from calling submit_work at ctx=2 with only text
# output and no actual computation.
_GATE = {
    "required_tool_groups": [
        ["run_lean4", "run_sympy"],
    ],
}

_STAGES = ["CALIBRATION", "COMPUTATIONAL_EXPLORATION"]


def up(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name",
        {"name": _TEMPLATE_NAME},
    ).fetchone()
    if not row:
        print(f"[0114] WARNING: template '{_TEMPLATE_NAME}' not found — skipping.")
        return
    tid = row["id"]

    updated = 0
    for stage_key in _STAGES:
        stage = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not stage:
            print(f"[0114] WARNING: stage '{stage_key}' not found — skipping.")
            continue
        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.update(_GATE)
        conn.execute(
            "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
            {"config": _json.dumps(cfg), "sid": stage["id"]},
        )
        print(f"[0114] Applied required_tool_groups gate to '{stage_key}' (id={stage['id']}).")
        updated += 1

    print(f"[0114] Done — {updated} stages updated.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name",
        {"name": _TEMPLATE_NAME},
    ).fetchone()
    if not row:
        return
    tid = row["id"]

    for stage_key in _STAGES:
        stage = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not stage:
            continue
        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.pop("required_tool_groups", None)
        conn.execute(
            "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
            {"config": _json.dumps(cfg), "sid": stage["id"]},
        )
        print(f"[0114] Removed required_tool_groups from '{stage_key}'.")

    print("[0114] Rolled back.")
