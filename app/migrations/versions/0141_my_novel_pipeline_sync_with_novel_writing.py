"""Sync My Novel Pipeline (user-cloned from Novel Writing) with the base template.

Applies the same changes as migrations 0136 (intake decomposition) and 0139
(planning_agent → generic_stage) to the cloned template.

Changes:
  1. Replace idea / intake_agent with 4 intake sub-stages (scope/conflict/feasibility/gate)
  2. Replace planning / planning_agent → generic_stage (story structure plan)
  3. Replace outline  / planning_agent → generic_stage (chapter outline)
"""

import json

description = "my novel pipeline sync with novel writing"

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

_NW_PLANNING_PROMPT = """\
You are a story structure architect. Produce a complete story development plan:
- title: working title
- logline: one-sentence premise
- genre_and_tone: genre tags + tonal direction
- protagonist: name, goal, flaw, arc
- antagonist_or_conflict: source of opposition
- three_act_structure: setup / confrontation / resolution beats
- chapter_count: estimated number of chapters
- pov: narrative point of view (first-person, third-limited, omniscient)
- themes: 2–3 thematic concerns

Call submit_work with the story plan as JSON.
"""

_NW_OUTLINE_PROMPT = """\
You are a chapter outline architect. Based on the story plan, produce a complete
chapter-by-chapter outline. For each chapter:
- chapter_number: integer
- title: chapter title or working label
- pov_character: whose viewpoint
- setting: location and time
- events: 3–5 key plot events that occur
- emotional_arc: character emotional state start → end
- ends_on: how the chapter closes (cliffhanger, revelation, quiet beat)

Call submit_work with chapters as a JSON array.
"""


def up(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'My Novel Pipeline'"
    ).fetchone()
    if not row:
        print("[0141] My Novel Pipeline not found — skipping.")
        return
    tmpl_id = row[0]

    legacy = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'idea'",
        {"t": tmpl_id},
    ).fetchone()
    if not legacy:
        print("[0141] idea stage already removed — skipping.")
        return
    idea_id = legacy[0]

    # ── 1. Remove transitions and legacy intake stage ──────────────────────
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
        {"t": tmpl_id, "s": idea_id},
    )
    conn.execute("DELETE FROM pipeline_stages WHERE id = :s", {"s": idea_id})

    # ── 2. Shift remaining stages +3 ──────────────────────────────────────
    conn.execute(
        "UPDATE pipeline_stages SET position = position + 3 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # ── 3. Insert 4 intake sub-stages ─────────────────────────────────────
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

    planning_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'planning'",
        {"t": tmpl_id},
    ).fetchone()[0]

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

    # ── 4. Swap planning_agent stages to generic_stage ─────────────────────
    planning_stages = [
        ("planning", "Planning", _NW_PLANNING_PROMPT),
        ("outline",  "Outline",  _NW_OUTLINE_PROMPT),
    ]
    updated = 0
    for stage_key, label, prompt in planning_stages:
        result = conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = 'generic_stage', label = :lbl, config = CAST(:cfg AS jsonb) "
            "WHERE template_id = :t AND stage_key = :sk AND agent_type = 'planning_agent'",
            {"t": tmpl_id, "sk": stage_key, "lbl": label,
             "cfg": json.dumps({"system_prompt": prompt})},
        )
        updated += result.rowcount

    print(f"[0141] Inserted 4 intake stages + 8 transitions; updated {updated} planning stages.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'My Novel Pipeline'"
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
    conn.execute(
        "UPDATE pipeline_stages SET position = position - 3 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    idea_id = conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'idea', 'Idea', 'intake_agent', 0, CAST('{}' AS jsonb)) RETURNING id",
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

    for stage_key, label in [("planning", "Planning"), ("outline", "Outline")]:
        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = 'planning_agent', label = :lbl, config = CAST('{}' AS jsonb) "
            "WHERE template_id = :t AND stage_key = :sk",
            {"t": tmpl_id, "sk": stage_key, "lbl": label},
        )
