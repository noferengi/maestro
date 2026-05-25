description = "SW Dev: decompose planning_node into 6 sequential stages (Phase 6)"

import json as _json

# ---------------------------------------------------------------------------
# Stage configs
# ---------------------------------------------------------------------------

_SURVEY_CONFIG: dict = {}  # planning_survey_node reads task fields directly

_PROPOSE_CONFIG = {
    "required_input_keys": ["survey_summary"],
    "personas": [
        {
            "name": "correctness",
            "system_prompt": (
                "Your primary concern is correctness and testability. "
                "Design for explicit, predictable error handling and well-defined failure modes. "
                "Create clean test seams — each component must be independently verifiable without "
                "needing to wire up the whole system. Prefer explicit over implicit. "
                "In your design_rationale, explain how the structure makes the system easy to test "
                "and how errors propagate clearly. "
                "Call submit_work with your full design JSON."
            ),
        },
        {
            "name": "security",
            "system_prompt": (
                "Your primary concern is security and defensive design. "
                "Minimise the attack surface. Validate all inputs at every trust boundary. "
                "Use safe defaults and fail closed on unexpected conditions. "
                "Avoid over-privileged components — each module should access only what it needs. "
                "Think through what can go wrong and design around it. "
                "In your design_rationale, explain the key trust boundaries, what is validated where, "
                "and how the design degrades safely under adversarial or unexpected input. "
                "Call submit_work with your full design JSON."
            ),
        },
        {
            "name": "clarity",
            "system_prompt": (
                "Your primary concern is code clarity and consistency with the existing codebase. "
                "Study the survey carefully: match the naming conventions, file layout, module "
                "structure, and idioms already present. A contributor familiar with the existing code "
                "should be able to predict every design choice you make before reading it. "
                "Prefer conventional structure over clever structure. Avoid introducing new patterns "
                "when existing ones already solve the problem. "
                "In your design_rationale, describe specifically how your design mirrors the patterns "
                "you observed in the codebase survey. "
                "Call submit_work with your full design JSON."
            ),
        },
        {
            "name": "performance",
            "system_prompt": (
                "Your primary concern is performance and resource efficiency. "
                "Minimise unnecessary computation, I/O, and database round-trips on the critical path. "
                "Consider caching strategies and async opportunities that reduce latency where it matters. "
                "Avoid premature abstraction that adds indirection without benefit. "
                "Design data flows so that the common case is fast; handle the slow path explicitly. "
                "In your design_rationale, identify the performance-critical paths and explain the "
                "specific choices that keep them efficient. "
                "Call submit_work with your full design JSON."
            ),
        },
        {
            "name": "architecture",
            "system_prompt": (
                "Your primary concern is clean architecture and strict separation of concerns. "
                "Each module must have one clear, narrow responsibility. Define explicit interface "
                "contracts between components — what each provides, what it consumes, what invariants "
                "it upholds. Minimise coupling: a change in one area should not ripple unexpectedly. "
                "Design the system so its structure is self-evident from the file layout alone. "
                "In your design_rationale, explain exactly where you drew each boundary and why each "
                "component owns the responsibilities it does. "
                "Call submit_work with your full design JSON."
            ),
        },
    ],
    "judge_system_prompt": (
        "You are a senior engineer selecting the best design proposal for production. "
        "Review all proposals and select the one that best balances correctness, security, "
        "clarity, performance, and architectural soundness for this specific task. "
        "Output JSON: {\"winner_index\": N, \"rationale\": \"concise reason\"}"
    ),
    "output_key": "winning_design",
}

_REVIEW_TOOLS = [
    "read_file", "read_file_metadata", "find_in_files", "find_files",
    "find_symbol", "find_callers", "list_directory", "submit_work",
]

_REVIEW_CONFIG = {
    "required_input_keys": ["survey_summary", "winning_design"],
    "reviewers": [
        {
            "name": "coupling",
            "system_prompt": (
                "You are a design reviewer focused on coupling and dependencies. "
                "Review the proposed design (available above in survey_summary and winning_design). "
                "Check for: circular dependencies, over-coupling, god objects, missing abstractions, "
                "and issues in the dependency graph. "
                "Vote ACCEPTED if coupling is sound, REJECTED if there are critical coupling issues. "
                "Call submit_work with signal='ACCEPTED'|'REJECTED', "
                "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
            ),
            "tools": _REVIEW_TOOLS,
            "max_turns": 12,
        },
        {
            "name": "interface",
            "system_prompt": (
                "You are a design reviewer focused on interface completeness. "
                "Review the proposed design (available above in survey_summary and winning_design). "
                "Check for: contract coverage, API documentation, data flow explicitness. "
                "Every 'consumes' entry must resolve to a 'provides'. "
                "Vote ACCEPTED if interfaces are complete, REJECTED if there are gaps. "
                "Call submit_work with signal='ACCEPTED'|'REJECTED', "
                "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
            ),
            "tools": _REVIEW_TOOLS,
            "max_turns": 12,
        },
        {
            "name": "testability",
            "system_prompt": (
                "You are a design reviewer focused on testability and safety. "
                "Review the proposed design (available above in survey_summary and winning_design). "
                "Check: test strategy adequacy, destructive operation risks, safety rule compliance. "
                "Vote ACCEPTED if the design is testable and safe, REJECTED if there are critical gaps. "
                "Call submit_work with signal='ACCEPTED'|'REJECTED', "
                "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
            ),
            "tools": _REVIEW_TOOLS,
            "max_turns": 12,
        },
        {
            "name": "security_design",
            "system_prompt": (
                "You are a design reviewer focused on security concerns BEFORE any code is written. "
                "Review the proposed design (available above in survey_summary and winning_design). "
                "Look for: authentication/authorization gaps, data flows exposing sensitive information, "
                "API endpoints lacking security controls, missing encryption, injection vulnerabilities. "
                "Vote ACCEPTED if security is sound, REJECTED if there are fundamental security flaws. "
                "Call submit_work with signal='ACCEPTED'|'REJECTED', "
                "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
            ),
            "tools": _REVIEW_TOOLS,
            "max_turns": 12,
        },
        {
            "name": "performance",
            "system_prompt": (
                "You are a design reviewer focused on performance and scalability. "
                "Review the proposed design (available above in survey_summary and winning_design). "
                "Look for: N+1 query patterns, missing caching strategy, synchronous blocking in async paths, "
                "unbounded data growth, algorithms with poor time/space complexity. "
                "Vote ACCEPTED if performance is sound, REJECTED if there are fundamental scalability flaws. "
                "Call submit_work with signal='ACCEPTED'|'REJECTED', "
                "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
            ),
            "tools": _REVIEW_TOOLS,
            "max_turns": 12,
        },
    ],
    "tally_strategy": "majority",
    "output_key": "design_review_result",
}

_PITFALLS_CONFIG: dict = {}   # pitfall_node reads winning_design from task.content directly
_CONSOLIDATE_CONFIG: dict = {}  # consolidation_node reads from task.content directly
_GATE_CONFIG: dict = {}         # planning_gate_node reads from planning_results table directly


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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


def _insert_stage(conn, tid, stage_key, label, agent_type, position, config):
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, :key, :label, :atype, :pos, CAST(:cfg AS jsonb))",
        {
            "tid": tid, "key": stage_key, "label": label,
            "atype": agent_type, "pos": position, "cfg": _json.dumps(config),
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


# ---------------------------------------------------------------------------
# up
# ---------------------------------------------------------------------------

def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0124] WARNING: 'Software Development' template not found — skipping.")
        return

    planning_row = _get_stage(conn, tid, "planning")
    if not planning_row:
        print("[0124] WARNING: 'planning' stage not found — already migrated? Skipping.")
        return

    planning_id = planning_row["id"]

    # ------------------------------------------------------------------ #
    # 1. Collect IDs for stages that stay unchanged                       #
    # ------------------------------------------------------------------ #
    idea_row          = _get_stage(conn, tid, "idea")
    jsg_row           = _get_stage(conn, tid, "json_schema_gate")
    correction_row    = _get_stage(conn, tid, "planning_correction")

    idea_id       = idea_row["id"]       if idea_row       else None
    jsg_id        = jsg_row["id"]        if jsg_row        else None
    correction_id = correction_row["id"] if correction_row else None

    # ------------------------------------------------------------------ #
    # 2. Shift positions of json_schema_gate and all later stages by +5  #
    # ------------------------------------------------------------------ #
    conn.execute(
        """
        UPDATE pipeline_stages
        SET position = position + 5
        WHERE template_id = :tid
          AND position >= :cutoff
        """,
        {"tid": tid, "cutoff": planning_row["position"] + 1},
    )
    print("[0124] Shifted downstream stage positions +5.")

    # ------------------------------------------------------------------ #
    # 3. Rename 'planning' → 'planning_survey' (reuse same row, pos=2)   #
    # ------------------------------------------------------------------ #
    conn.execute(
        """
        UPDATE pipeline_stages
        SET stage_key  = 'planning_survey',
            label      = 'Plan: Survey',
            agent_type = 'planning_survey_node',
            config     = CAST(:cfg AS jsonb)
        WHERE id = :sid
        """,
        {"cfg": _json.dumps(_SURVEY_CONFIG), "sid": planning_id},
    )
    survey_id = planning_id
    base_pos = planning_row["position"]   # = 2
    print(f"[0124] Renamed planning -> planning_survey at position {base_pos}.")

    # ------------------------------------------------------------------ #
    # 4. Insert 5 new stages                                              #
    # ------------------------------------------------------------------ #
    propose_id     = _insert_stage(conn, tid, "planning_propose",    "Plan: Propose",     "fan_out_judge",        base_pos + 1, _PROPOSE_CONFIG)
    review_id      = _insert_stage(conn, tid, "planning_review",     "Plan: Review",      "voting_panel",         base_pos + 2, _REVIEW_CONFIG)
    pitfalls_id    = _insert_stage(conn, tid, "planning_pitfalls",   "Plan: Pitfalls",    "pitfall_node",         base_pos + 3, _PITFALLS_CONFIG)
    consolidate_id = _insert_stage(conn, tid, "planning_consolidate","Plan: Consolidate", "consolidation_node",   base_pos + 4, _CONSOLIDATE_CONFIG)
    gate_id        = _insert_stage(conn, tid, "planning_gate",       "Plan: Gate",        "planning_gate_node",   base_pos + 5, _GATE_CONFIG)
    print("[0124] Inserted 5 new planning stages.")

    # ------------------------------------------------------------------ #
    # 5. Delete old transitions involving 'planning' (now planning_survey)#
    # ------------------------------------------------------------------ #
    # Remove all FROM/TO planning_survey (will re-wire below)
    conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": survey_id})
    conn.execute("DELETE FROM pipeline_transitions WHERE to_stage_id   = :sid", {"sid": survey_id})

    # ------------------------------------------------------------------ #
    # 6. Wire new transitions                                             #
    # ------------------------------------------------------------------ #
    # idea → planning_survey (pass)
    if idea_id:
        _wire(conn, tid, idea_id, survey_id, "pass")

    # planning_survey → planning_propose (pass) | → idea (fail)
    _wire(conn, tid, survey_id, propose_id, "pass")
    if idea_id:
        _wire(conn, tid, survey_id, idea_id, "fail")

    # planning_propose → planning_review (pass) | → idea (fail/reject)
    _wire(conn, tid, propose_id, review_id, "pass")
    if idea_id:
        _wire(conn, tid, propose_id, idea_id, "fail")
        _wire(conn, tid, propose_id, idea_id, "reject")

    # planning_review → planning_pitfalls (pass) | → planning_propose (fail/reject)
    # The fail→propose transition replaces the Python retry loop.
    _wire(conn, tid, review_id, pitfalls_id, "pass")
    _wire(conn, tid, review_id, propose_id, "fail")
    _wire(conn, tid, review_id, propose_id, "reject")

    # planning_pitfalls → planning_consolidate (pass)  [always passes]
    _wire(conn, tid, pitfalls_id, consolidate_id, "pass")

    # planning_consolidate → planning_gate (pass) | → idea (fail)
    _wire(conn, tid, consolidate_id, gate_id, "pass")
    if idea_id:
        _wire(conn, tid, consolidate_id, idea_id, "fail")

    # planning_gate → json_schema_gate (pass) | → planning_correction (fail)
    if jsg_id:
        _wire(conn, tid, gate_id, jsg_id, "pass")
    if correction_id:
        _wire(conn, tid, gate_id, correction_id, "fail")

    # ------------------------------------------------------------------ #
    # 7. Wire planning_correction fail -> planning_gate                   #
    # The old planning_correction->planning(fail) row was deleted above   #
    # (to_stage_id pointed at survey_id=planning_id). Re-insert it.       #
    # ------------------------------------------------------------------ #
    if correction_id and gate_id:
        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE from_stage_id = :corr_id AND condition = 'fail'",
            {"corr_id": correction_id},
        )
        _wire(conn, tid, correction_id, gate_id, "fail")
        print("[0124] Wired planning_correction fail -> planning_gate.")

    print("[0124] Transitions wired for all 6 planning stages.")


# ---------------------------------------------------------------------------
# down
# ---------------------------------------------------------------------------

def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    survey_row   = _get_stage(conn, tid, "planning_survey")
    if not survey_row:
        return

    survey_id = survey_row["id"]
    base_pos  = survey_row["position"]

    # Collect new stage IDs
    propose_row     = _get_stage(conn, tid, "planning_propose")
    review_row      = _get_stage(conn, tid, "planning_review")
    pitfalls_row    = _get_stage(conn, tid, "planning_pitfalls")
    consolidate_row = _get_stage(conn, tid, "planning_consolidate")
    gate_row        = _get_stage(conn, tid, "planning_gate")
    correction_row  = _get_stage(conn, tid, "planning_correction")
    jsg_row         = _get_stage(conn, tid, "json_schema_gate")
    idea_row        = _get_stage(conn, tid, "idea")

    # Delete the 5 inserted stages + all their transitions
    for row in [propose_row, review_row, pitfalls_row, consolidate_row, gate_row]:
        if row:
            conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid OR to_stage_id = :sid", {"sid": row["id"]})
            conn.execute("DELETE FROM pipeline_stages WHERE id = :sid", {"sid": row["id"]})

    # Restore planning_correction fail → planning (survey_id will become planning again)
    if correction_row and survey_id:
        conn.execute(
            """
            UPDATE pipeline_transitions
            SET to_stage_id = :planning_id
            WHERE from_stage_id = :corr_id AND condition = 'fail'
            """,
            {"planning_id": survey_id, "corr_id": correction_row["id"]},
        )

    # Revert planning_survey → planning
    conn.execute(
        """
        UPDATE pipeline_stages
        SET stage_key  = 'planning',
            label      = 'Planning',
            agent_type = 'planning_node',
            config     = NULL
        WHERE id = :sid
        """,
        {"sid": survey_id},
    )

    # Restore downstream positions (-5)
    conn.execute(
        """
        UPDATE pipeline_stages
        SET position = position - 5
        WHERE template_id = :tid
          AND position > :cutoff
        """,
        {"tid": tid, "cutoff": base_pos},
    )

    # Rewire: idea → planning (pass)
    if idea_row:
        conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": idea_row["id"]})
        _wire(conn, tid, idea_row["id"], survey_id, "pass")

    # Rewire: planning → json_schema_gate (pass), → idea (fail/reject)
    conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": survey_id})
    if jsg_row:
        _wire(conn, tid, survey_id, jsg_row["id"], "pass")
    if idea_row:
        _wire(conn, tid, survey_id, idea_row["id"], "fail")
        _wire(conn, tid, survey_id, idea_row["id"], "reject")

    print("[0124 down] Reverted 6-stage planning chain → single planning_node stage.")
