"""Replace the two planning_agent stages in the Overnight Generation template with generic_stage.

The story bible and per-chapter outline are single-turn structured-output tasks that run
unattended in a batch pipeline. The planning monolith is overkill; generic_stage with a
self-contained system prompt is sufficient and much faster.

Stages updated (in-place, no position shifts):
  story_bible      (pos 2) — planning_agent → generic_stage
  chapter_outline  (pos 4) — planning_agent → generic_stage
"""

import json

description = "overnight generation replace planning agent with generic stage"

_OG_BIBLE_PROMPT = """\
You are building a story bible for an unattended batch writing pipeline.
The story bible must be self-contained — all agents will read it without seeing
the original seed prompt.

Produce:
- title, genre, tone
- world: setting, rules, atmosphere
- characters: name, role, voice, goal for each
- central_conflict: the driving tension
- chapter_count: how many chapters to generate
- style_guide: sentence length, POV, vocabulary register

Call submit_work with the story bible as JSON.
"""

_OG_OUTLINE_PROMPT = """\
You are generating a chapter outline based on the story bible.

For each chapter produce:
- chapter_number
- title
- events: 3 key events
- emotional_beat: the dominant emotion of the chapter
- ends_on: final line direction (leave reader curious, resolved, shocked)

Call submit_work with chapters as a JSON array.
"""

_STAGES = [
    ("story_bible",     "Story Bible",     _OG_BIBLE_PROMPT),
    ("chapter_outline", "Chapter Outline", _OG_OUTLINE_PROMPT),
]


def up(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Overnight Generation'"
    ).fetchone()
    if not row:
        print("[0140] Overnight Generation template not found — skipping.")
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

    print(f"[0140] Updated {updated} planning_agent stages to generic_stage in Overnight Generation.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Overnight Generation'"
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
