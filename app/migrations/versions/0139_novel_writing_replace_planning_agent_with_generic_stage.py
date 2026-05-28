"""Replace the two planning_agent stages in the Novel Writing template with generic_stage.

Story structure planning and chapter outlining are single-turn structured-output tasks;
they don't benefit from the survey/propose/review/pitfalls/consolidate/gate monolith
designed for code planning.

Stages updated (in-place, no position shifts):
  planning  (pos 4) — planning_agent → generic_stage  (story structure plan)
  outline   (pos 5) — planning_agent → generic_stage  (chapter-by-chapter outline)
"""

import json

description = "novel writing replace planning agent with generic stage"

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

_STAGES = [
    ("planning", "Planning", _NW_PLANNING_PROMPT),
    ("outline",  "Outline",  _NW_OUTLINE_PROMPT),
]


def up(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Novel Writing'"
    ).fetchone()
    if not row:
        print("[0139] Novel Writing template not found — skipping.")
        return
    tmpl_id = row[0]

    updated = 0
    for stage_key, label, prompt in _STAGES:
        result = conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = 'generic_stage', "
            "    label = :lbl, "
            "    config = CAST(:cfg AS jsonb) "
            "WHERE template_id = :t AND stage_key = :sk AND agent_type = 'planning_agent'",
            {
                "t": tmpl_id, "sk": stage_key, "lbl": label,
                "cfg": json.dumps({"system_prompt": prompt}),
            },
        )
        updated += result.rowcount

    print(f"[0139] Updated {updated} planning_agent stages to generic_stage in Novel Writing.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Novel Writing'"
    ).fetchone()
    if not row:
        return
    tmpl_id = row[0]

    for stage_key, label, _ in _STAGES:
        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = 'planning_agent', "
            "    label = :lbl, "
            "    config = CAST('{}' AS jsonb) "
            "WHERE template_id = :t AND stage_key = :sk",
            {"t": tmpl_id, "sk": stage_key, "lbl": label},
        )
