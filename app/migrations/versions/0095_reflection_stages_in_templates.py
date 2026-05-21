description = "Add reflection_agent stages to Software Development and Math Proof builtin templates"

import json as _json

_REFLECTION_SYSTEM_PROMPT = (
    "You are a skeptical reviewer. Your role is to find problems with the work product "
    "described in the task context — not to be encouraging, but to identify real defects, "
    "wrong assumptions, and missed edge cases that the producing agent may have overlooked.\n\n"
    "Be specific. Vague concerns do not help. If you are uncertain, say so in "
    "`uncertain_about`. Do not invent issues. A high-confidence clean report is valuable.\n\n"
    "You may call get_task_history_recent to inspect the worker agent's LLM turn history "
    "when the base context is insufficient. Use it when needed.\n\n"
    "When you have completed your analysis, call submit_work with a JSON payload matching "
    "this schema exactly:\n\n"
    "{\n"
    '  "confidence": <float 0.0-1.0>,\n'
    '  "issues": [\n'
    '    {"severity": "blocking"|"warning"|"note", "finding": "<specific description>"}\n'
    "  ],\n"
    '  "uncertain_about": ["<thing you could not verify>"]\n'
    "}\n\n"
    "Severity levels:\n"
    "  blocking  — real defect that should not advance\n"
    "  warning   — potential issue; orchestrator decides\n"
    "  note      — cosmetic / speculative; human review only"
)

_REFLECTION_CONFIG = _json.dumps({
    "system_prompt": _REFLECTION_SYSTEM_PROMPT,
    "gate_type": "single_pass",
    "max_turns": 150,
})


def _insert_reflection_stage(conn, template_name, before_stage_key, new_stage_key, new_label):
    """
    Insert a reflection stage immediately before `before_stage_key` in `template_name`.

    Steps:
      1. Find the template id.
      2. Find the 'before' stage and its predecessor via the pass transition.
      3. Insert a new pipeline_stages row at a fractional position.
      4. Delete the old direct transition predecessor → before_stage (pass).
      5. Insert predecessor → new_stage (pass) and new_stage → before_stage (pass).
    """
    # 1. Template
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name AND is_builtin = TRUE",
        {"name": template_name},
    ).fetchone()
    if row is None:
        return  # template doesn't exist in this DB — skip gracefully

    template_id = row[0]

    # 2. Find the 'before' stage
    before_row = conn.execute(
        "SELECT id, position FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key",
        {"tid": template_id, "key": before_stage_key},
    ).fetchone()
    if before_row is None:
        return

    before_stage_id, before_position = before_row

    # Find the predecessor (stage whose pass transition leads to before_stage_key)
    pred_row = conn.execute(
        "SELECT ps.id, ps.position FROM pipeline_transitions t "
        "JOIN pipeline_stages ps ON t.from_stage_id = ps.id "
        "WHERE t.to_stage_id = :to_id AND t.condition = 'pass' AND ps.template_id = :tid",
        {"to_id": before_stage_id, "tid": template_id},
    ).fetchone()
    if pred_row is None:
        return

    pred_stage_id, pred_position = pred_row

    # 3. Choose a fractional position between predecessor and before_stage
    new_position = (pred_position + before_position) / 2.0

    # 4. Insert new stage
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, :key, :label, 'reflection_agent', :pos, :cfg)",
        {
            "tid": template_id,
            "key": new_stage_key,
            "label": new_label,
            "pos": new_position,
            "cfg": _REFLECTION_CONFIG,
        },
    )

    new_stage_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
        {"tid": template_id, "key": new_stage_key},
    ).fetchone()[0]

    # 5. Remove the direct pass transition predecessor → before_stage
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE from_stage_id = :from_id AND to_stage_id = :to_id AND condition = 'pass'",
        {"from_id": pred_stage_id, "to_id": before_stage_id},
    )

    # 6. Wire: predecessor → new_stage (pass) and new_stage → before_stage (pass)
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": template_id, "from_id": pred_stage_id, "to_id": new_stage_id},
    )
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": template_id, "from_id": new_stage_id, "to_id": before_stage_id},
    )


def up(conn):
    # Software Development: reflection between optimization and security
    _insert_reflection_stage(
        conn,
        template_name="Software Development",
        before_stage_key="security",
        new_stage_key="reflection",
        new_label="Reflection",
    )

    # Mathematics / Proof Exploration: reflection between PROOF_ATTEMPT and FORMAL_VERIFICATION
    _insert_reflection_stage(
        conn,
        template_name="Mathematics / Proof Exploration",
        before_stage_key="FORMAL_VERIFICATION",
        new_stage_key="REFLECTION",
        new_label="Reflection",
    )


def down(conn):
    for template_name, stage_key in [
        ("Software Development", "reflection"),
        ("Mathematics / Proof Exploration", "REFLECTION"),
    ]:
        row = conn.execute(
            "SELECT id FROM pipeline_templates WHERE name = :name AND is_builtin = TRUE",
            {"name": template_name},
        ).fetchone()
        if row is None:
            continue
        template_id = row[0]

        refl_row = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": template_id, "key": stage_key},
        ).fetchone()
        if refl_row is None:
            continue
        refl_id = refl_row[0]

        # Find predecessor and successor of the reflection stage
        pred_row = conn.execute(
            "SELECT from_stage_id FROM pipeline_transitions "
            "WHERE to_stage_id = :refl_id AND condition = 'pass'",
            {"refl_id": refl_id},
        ).fetchone()
        succ_row = conn.execute(
            "SELECT to_stage_id FROM pipeline_transitions "
            "WHERE from_stage_id = :refl_id AND condition = 'pass'",
            {"refl_id": refl_id},
        ).fetchone()

        if pred_row and succ_row:
            conn.execute(
                "INSERT INTO pipeline_transitions "
                "(template_id, from_stage_id, to_stage_id, condition, priority) "
                "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
                {"tid": template_id, "from_id": pred_row[0], "to_id": succ_row[0]},
            )

        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE from_stage_id = :id OR to_stage_id = :id",
            {"id": refl_id},
        )
        conn.execute(
            "DELETE FROM pipeline_stages WHERE id = :id",
            {"id": refl_id},
        )
