"""Replace the monolithic idea / intake_agent stage in the Novel Writing template
with four decomposed intake sub-stages (no intake_static — creative pipeline has no codebase):
  intake_scope → intake_conflict → intake_feasibility → intake_gate

Remaining stages are shifted +3. Fail on any intake stage loops back to intake_scope.
Gate passes to the existing 'planning' stage.
"""

import json

description = "novel writing intake decomposition"

_NW_INTAKE_SCOPE_PROMPT = """\
You are a creative writing development editor assessing a story concept.

Analyse:
1. CONCEPT CLARITY — Is there a clear protagonist, central conflict, and setting?
2. GENRE & TONE — Is the genre specified? Is the tone (dark, comedic, literary) implied?
3. SCOPE — Is this a short story, novella, or full novel? Is the scope explicit?
4. ORIGINALITY — Is this sufficiently distinct from obvious genre tropes, or purely derivative?

Output: DEVELOPED, UNDERDEVELOPED, or TOO_VAGUE, with a brief assessment of what is present and what is missing.
"""

_NW_INTAKE_CONFLICT_PROMPT = """\
You are a project coordinator for a creative writing pipeline.

Compare the proposed story against all currently active, non-published story tasks in the project.

Detect:
- PREMISE CONFLICT: Effectively the same story premise.
- CHARACTER CONFLICT: A protagonist/antagonist identical or nearly identical to an existing one.
- SETTING CONFLICT: Same world/setting being explored by another active task.
- TITLE CONFLICT: Same or very similar working title.

Output "No conflicts detected" if none found. Otherwise describe the conflict and affected task.
"""

_NW_INTAKE_FEASIBILITY_PROMPT = """\
You are a creative writing pipeline feasibility analyst.

Assess whether this story concept can be executed by an LLM-based pipeline:
1. CONTENT APPROPRIATENESS — Is the content within platform guidelines (no explicit/harmful content)?
2. LLM EXECUTABILITY — Does the story require specialized knowledge (highly technical, real-person fiction, legal risk) that an LLM cannot safely handle?
3. LENGTH FIT — Is the requested length achievable in a single pipeline run (under ~80,000 words)?
4. CONCEPT COMPLETENESS — Is there enough foundation to begin structured planning?

Output: FEASIBLE, INFEASIBLE, or NEEDS_DEVELOPMENT, with rationale.
"""


def up(conn):
    # ── 1. Resolve template ────────────────────────────────────────────────
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Novel Writing'"
    ).fetchone()
    if not row:
        print("[0136] Novel Writing template not found — skipping.")
        return
    tmpl_id = row[0]

    legacy = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'idea'",
        {"t": tmpl_id},
    ).fetchone()
    if not legacy:
        print("[0136] idea stage already removed — skipping.")
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
                     {"system_prompt": _NW_INTAKE_SCOPE_PROMPT})
    s_conflict = ins("intake_conflict",    "Intake: Conflict",    "intake_conflict",    1,
                     {"system_prompt": _NW_INTAKE_CONFLICT_PROMPT})
    s_feasib   = ins("intake_feasibility", "Intake: Feasibility", "intake_feasibility", 2,
                     {"system_prompt": _NW_INTAKE_FEASIBILITY_PROMPT})
    s_igate    = ins("intake_gate",        "Intake: Gate",        "intake_gate",        3, {})

    # ── 6. Find planning stage (gate passes here) ──────────────────────────
    planning_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'planning'",
        {"t": tmpl_id},
    ).fetchone()[0]

    # ── 7. Wire transitions ────────────────────────────────────────────────
    edges = [
        (s_scope,    s_scope,      "fail"),
        (s_scope,    s_conflict,   "pass"),
        (s_conflict, s_scope,      "fail"),
        (s_conflict, s_feasib,     "pass"),
        (s_feasib,   s_scope,      "fail"),
        (s_feasib,   s_igate,      "pass"),
        (s_igate,    s_scope,      "fail"),
        (s_igate,    planning_id,  "pass"),
    ]
    for (fr, to, cond) in edges:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )

    print(f"[0136] Inserted 4 intake stages + {len(edges)} transitions; remaining stages shifted +3.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Novel Writing'"
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

    planning_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'planning'",
        {"t": tmpl_id},
    ).fetchone()[0]

    for cond, to in [("pass", planning_id), ("fail", idea_id)]:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": idea_id, "to": to, "c": cond},
        )
