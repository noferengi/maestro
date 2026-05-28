"""Replace the three planning_agent stages in the Data Analysis template with generic_stage.

The planning_pipeline monolith (survey/propose/review/pitfalls/consolidate/gate) was designed
for code planning. Data analysis question refinement, schema design, and analysis planning are
simple single-turn structured-output stages — generic_stage with a crafted system_prompt suffices.

Stages updated (in-place, no position shifts):
  planning           (pos 4) — planning_agent → generic_stage
  question_refinement (pos 5) — planning_agent → generic_stage
  schema_design      (pos 7) — planning_agent → generic_stage
"""

import json

description = "data analysis replace planning agent with generic stage"

_DA_PLANNING_PROMPT = """\
You are a data analysis project planner. Based on the refined analysis question,
produce a structured analysis plan with:
- analysis_objective: one clear sentence
- data_sources: list of required datasets/tables/APIs
- methodology: list of analysis steps (load → clean → explore → model → visualise)
- output_artifacts: list of expected deliverables (CSV, chart, report section)
- success_criteria: how to know the analysis is complete and correct

Call submit_work with the plan as a JSON object.
"""

_DA_QUESTION_PROMPT = """\
You are a data science research assistant. The raw analysis question needs sharpening.

Produce:
- refined_question: a precise, measurable, answerable version of the question
- key_metrics: the specific quantities to compute
- assumptions: any assumptions made to make the question answerable
- out_of_scope: what this analysis explicitly does NOT cover

Call submit_work with the refined question definition as JSON.
"""

_DA_SCHEMA_PROMPT = """\
You are a data schema designer. Based on the analysis plan and collected data,
design the working schema for this analysis:
- input_schema: describe each input dataset (columns, types, expected size)
- derived_tables: intermediate tables/dataframes to compute
- output_schema: final output columns and format

Call submit_work with the schema design as JSON.
"""

_STAGES = [
    ("planning",            "Planning",             _DA_PLANNING_PROMPT),
    ("question_refinement", "Question Refinement",  _DA_QUESTION_PROMPT),
    ("schema_design",       "Schema Design",        _DA_SCHEMA_PROMPT),
]


def up(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Data Analysis'"
    ).fetchone()
    if not row:
        print("[0138] Data Analysis template not found — skipping.")
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

    print(f"[0138] Updated {updated} planning_agent stages to generic_stage in Data Analysis.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Data Analysis'"
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
