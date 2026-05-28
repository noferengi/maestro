"""Replace the monolithic idea / intake_agent stage in the Data Analysis template
with four decomposed intake sub-stages:
  intake_scope → intake_conflict → intake_feasibility → intake_gate

Remaining stages are shifted +3. Fail on any intake stage loops back to intake_scope.
Gate passes to the existing 'question_refinement' stage.
"""

import json

description = "data analysis intake decomposition"

_DA_INTAKE_SCOPE_PROMPT = """\
You are a data analysis scoping analyst for the Maestro platform.

Assess:
1. QUESTION CLARITY — Is the analysis question specific and answerable?
2. DATA SOURCES — What data is implied? Is it accessible (structured DB, CSV, API)?
3. OUTPUT FORMAT — What should the deliverable be: report, model, visualisation, dashboard?
4. SCOPE SIZE — Is this a single focused query or a multi-stage investigation?

Output: CLEAR, UNCLEAR, or NEEDS_DECOMPOSITION, with a scope summary.
"""

_DA_INTAKE_CONFLICT_PROMPT = """\
You are a project coordinator for data analysis work.

Review the proposed analysis against all existing non-completed tasks.

Detect:
- DUPLICATE ANALYSIS: Same question or dataset already being processed.
- DEPENDENCY: This analysis requires outputs from another in-progress task.
- OVERLAP: Significant result overlap that would produce redundant artifacts.

Output a conflict report. State "No conflicts detected" if clean.
"""

_DA_INTAKE_FEASIBILITY_PROMPT = """\
You are a data feasibility analyst for the Maestro platform.

Assess:
1. DATA ACCESSIBILITY — Is the required data available within the project or via configured connectors?
2. TOOL FIT — Can this analysis be performed using Python (pandas, numpy, scikit-learn, matplotlib)?
3. SCOPE FIT — Is the depth appropriate for an automated agentic pipeline (not requiring human expert domain judgment)?
4. AMBIGUITY — Are there blocking unknowns that must be resolved before analysis can begin?

Output: FEASIBLE, INFEASIBLE, or NEEDS_CLARIFICATION, with rationale.
"""


def up(conn):
    # ── 1. Resolve template ────────────────────────────────────────────────
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Data Analysis'"
    ).fetchone()
    if not row:
        print("[0135] Data Analysis template not found — skipping.")
        return
    tmpl_id = row[0]

    legacy = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'idea'",
        {"t": tmpl_id},
    ).fetchone()
    if not legacy:
        print("[0135] idea stage already removed — skipping.")
        return
    idea_id = legacy[0]

    # ── 2. Delete transitions touching idea ────────────────────────────────
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
        {"t": tmpl_id, "s": idea_id},
    )

    # ── 3. Delete the legacy stage ─────────────────────────────────────────
    conn.execute(
        "DELETE FROM pipeline_stages WHERE id = :s", {"s": idea_id}
    )

    # ── 4. Shift remaining stages +3 ──────────────────────────────────────
    conn.execute(
        "UPDATE pipeline_stages SET position = position + 3 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # ── 5. Insert new intake stages ────────────────────────────────────────
    def ins(stage_key, label, agent_type, position, config):
        conn.execute(
            "INSERT INTO pipeline_stages "
            "(template_id, stage_key, label, agent_type, position, config) "
            "VALUES (:t, :sk, :lbl, :at, :pos, CAST(:cfg AS jsonb))",
            {
                "t": tmpl_id, "sk": stage_key, "lbl": label,
                "at": agent_type, "pos": position,
                "cfg": json.dumps(config),
            },
        )
        return conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = :sk",
            {"t": tmpl_id, "sk": stage_key},
        ).fetchone()[0]

    s_scope    = ins("intake_scope",       "Intake: Scope",       "intake_scope",       0,
                     {"system_prompt": _DA_INTAKE_SCOPE_PROMPT})
    s_conflict = ins("intake_conflict",    "Intake: Conflict",    "intake_conflict",    1,
                     {"system_prompt": _DA_INTAKE_CONFLICT_PROMPT})
    s_feasib   = ins("intake_feasibility", "Intake: Feasibility", "intake_feasibility", 2,
                     {"system_prompt": _DA_INTAKE_FEASIBILITY_PROMPT})
    s_igate    = ins("intake_gate",        "Intake: Gate",        "intake_gate",        3, {})

    # ── 6. Find question_refinement stage (gate passes here) ──────────────
    qr_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'question_refinement'",
        {"t": tmpl_id},
    ).fetchone()[0]

    # ── 7. Wire transitions ────────────────────────────────────────────────
    edges = [
        (s_scope,    s_scope,    "fail"),
        (s_scope,    s_conflict, "pass"),
        (s_conflict, s_scope,    "fail"),
        (s_conflict, s_feasib,   "pass"),
        (s_feasib,   s_scope,    "fail"),
        (s_feasib,   s_igate,    "pass"),
        (s_igate,    s_scope,    "fail"),
        (s_igate,    qr_id,      "pass"),
    ]
    for (fr, to, cond) in edges:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )

    print(f"[0135] Inserted 4 intake stages + {len(edges)} transitions; remaining stages shifted +3.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Data Analysis'"
    ).fetchone()
    if not row:
        return
    tmpl_id = row[0]

    new_keys = ["intake_scope", "intake_conflict", "intake_feasibility", "intake_gate"]
    rows = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = ANY(:keys)",
        {"t": tmpl_id, "keys": new_keys},
    ).fetchall()
    for r in rows:
        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
            {"t": tmpl_id, "s": r[0]},
        )
    conn.execute(
        "DELETE FROM pipeline_stages WHERE template_id = :t AND stage_key = ANY(:keys)",
        {"t": tmpl_id, "keys": new_keys},
    )

    # Shift remaining stages back -3
    conn.execute(
        "UPDATE pipeline_stages SET position = position - 3 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # Re-insert legacy idea stage at position 0
    idea_id = conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'idea', 'Idea', 'intake_agent', 0, CAST('{}' AS jsonb)) "
        "RETURNING id",
        {"t": tmpl_id},
    ).fetchone()[0]

    qr_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'question_refinement'",
        {"t": tmpl_id},
    ).fetchone()[0]

    for cond, to in [("pass", qr_id), ("fail", idea_id)]:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": idea_id, "to": to, "c": cond},
        )
