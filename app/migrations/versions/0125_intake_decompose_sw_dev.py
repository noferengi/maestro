description = "SW Dev: decompose intake_node into 5 sequential stages (Phase 7)"

import json as _json


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


def _insert_stage(conn, tid, stage_key, label, agent_type, position, config=None):
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, :key, :label, :atype, :pos, CAST(:cfg AS jsonb))",
        {
            "tid": tid, "key": stage_key, "label": label,
            "atype": agent_type, "pos": position,
            "cfg": _json.dumps(config or {}),
        },
    )
    return conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()["id"]


def _wire(conn, tid, from_id, to_id, condition):
    conn.execute(
        "INSERT INTO pipeline_transitions "
        "(template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, :cond, 0)",
        {"tid": tid, "from_id": from_id, "to_id": to_id, "cond": condition},
    )


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0125] WARNING: 'Software Development' template not found — skipping.")
        return

    idea_row = _get_stage(conn, tid, "idea")
    if not idea_row:
        print("[0125] WARNING: 'idea' stage not found — skipping.")
        return

    survey_row = _get_stage(conn, tid, "planning_survey")
    if not survey_row:
        print("[0125] WARNING: 'planning_survey' stage not found — skipping.")
        return

    idea_id  = idea_row["id"]
    idea_pos = idea_row["position"]   # = 1
    survey_id = survey_row["id"]

    # ------------------------------------------------------------------ #
    # 1. Shift all stages after idea by +4                                #
    # ------------------------------------------------------------------ #
    conn.execute(
        """
        UPDATE pipeline_stages
        SET position = position + 4
        WHERE template_id = :tid
          AND position > :cutoff
        """,
        {"tid": tid, "cutoff": idea_pos},
    )
    print("[0125] Shifted downstream stage positions +4.")

    # ------------------------------------------------------------------ #
    # 2. Rename 'idea' → 'intake_scope' (reuse same row)                  #
    # ------------------------------------------------------------------ #
    conn.execute(
        """
        UPDATE pipeline_stages
        SET stage_key  = 'intake_scope',
            label      = 'Intake: Scope',
            agent_type = 'intake_scope',
            config     = CAST(:cfg AS jsonb)
        WHERE id = :sid
        """,
        {"cfg": _json.dumps({}), "sid": idea_id},
    )
    scope_id = idea_id
    print(f"[0125] Renamed idea -> intake_scope at position {idea_pos}.")

    # ------------------------------------------------------------------ #
    # 3. Insert 4 new stages                                               #
    # ------------------------------------------------------------------ #
    static_id      = _insert_stage(conn, tid, "intake_static",      "Intake: Static",
                                   "intake_static",      idea_pos + 1)
    conflict_id    = _insert_stage(conn, tid, "intake_conflict",     "Intake: Conflict",
                                   "intake_conflict",    idea_pos + 2)
    feasibility_id = _insert_stage(conn, tid, "intake_feasibility",  "Intake: Feasibility",
                                   "intake_feasibility", idea_pos + 3)
    gate_id        = _insert_stage(conn, tid, "intake_gate",         "Intake: Gate",
                                   "intake_gate",        idea_pos + 4)
    print("[0125] Inserted 4 new intake stages.")

    # ------------------------------------------------------------------ #
    # 4. Delete old transitions for idea/scope                             #
    # ------------------------------------------------------------------ #
    conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": scope_id})
    conn.execute("DELETE FROM pipeline_transitions WHERE to_stage_id   = :sid", {"sid": scope_id})

    # ------------------------------------------------------------------ #
    # 5. Wire new transitions                                              #
    # ------------------------------------------------------------------ #
    _wire(conn, tid, scope_id,      static_id,      "pass")
    _wire(conn, tid, scope_id,      scope_id,       "fail")    # retry on LLM error
    _wire(conn, tid, static_id,     conflict_id,    "pass")
    _wire(conn, tid, static_id,     scope_id,       "fail")
    _wire(conn, tid, conflict_id,   feasibility_id, "pass")
    _wire(conn, tid, conflict_id,   scope_id,       "fail")
    _wire(conn, tid, feasibility_id, gate_id,       "pass")
    _wire(conn, tid, feasibility_id, scope_id,      "fail")
    _wire(conn, tid, gate_id,       survey_id,      "pass")   # → planning_survey
    _wire(conn, tid, gate_id,       scope_id,       "fail")   # rejected → retry
    print("[0125] Transitions wired for 5-stage intake chain.")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    scope_row = _get_stage(conn, tid, "intake_scope")
    if not scope_row:
        return

    scope_id  = scope_row["id"]
    scope_pos = scope_row["position"]

    static_row      = _get_stage(conn, tid, "intake_static")
    conflict_row    = _get_stage(conn, tid, "intake_conflict")
    feasibility_row = _get_stage(conn, tid, "intake_feasibility")
    gate_row        = _get_stage(conn, tid, "intake_gate")
    survey_row      = _get_stage(conn, tid, "planning_survey")

    for row in [static_row, conflict_row, feasibility_row, gate_row]:
        if row:
            conn.execute(
                "DELETE FROM pipeline_transitions "
                "WHERE from_stage_id = :sid OR to_stage_id = :sid",
                {"sid": row["id"]},
            )
            conn.execute("DELETE FROM pipeline_stages WHERE id = :sid", {"sid": row["id"]})

    conn.execute(
        """
        UPDATE pipeline_stages
        SET stage_key  = 'idea',
            label      = 'Idea',
            agent_type = 'intake_node',
            config     = NULL
        WHERE id = :sid
        """,
        {"sid": scope_id},
    )

    conn.execute(
        """
        UPDATE pipeline_stages
        SET position = position - 4
        WHERE template_id = :tid
          AND position > :cutoff
        """,
        {"tid": tid, "cutoff": scope_pos},
    )

    conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": scope_id})
    conn.execute("DELETE FROM pipeline_transitions WHERE to_stage_id   = :sid", {"sid": scope_id})
    if survey_row:
        _wire(conn, tid, scope_id, survey_row["id"], "pass")

    print("[0125 down] Reverted 5-stage intake chain → single intake_node (idea) stage.")
