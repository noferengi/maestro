description = "Capability Gap Analysis built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Capability Gap Analysis",
    "description": (
        "Survey what Maestro cannot do that it should. "
        "Output: a ranked backlog of new pipeline proposals. "
        "Maestro creates cards in this pipeline to plan its own growth."
    ),
    "stages": [
        {
            "key": "survey_templates",
            "label": "Survey Templates",
            "agent": "generic_stage",
            "pos": 0,
            "config": {
                "system_prompt": (
                    "You are a capability analyst. Survey all current pipeline templates in this "
                    "Maestro instance and produce a structured inventory.\n\n"
                    "For each template, note:\n"
                    "- Name and purpose\n"
                    "- Stage count and agent types used\n"
                    "- What workflows it enables\n"
                    "- What workflows it does NOT cover (obvious gaps given the template's purpose)\n\n"
                    "Also note: what agent types and node patterns exist but may be underused?\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"template_inventory\": [\n"
                    "    {\"name\": \"...\", \"purpose\": \"...\", \"stage_count\": N, "
                    "\"agent_types\": [...], \"known_gaps\": [...]}\n"
                    "  ],\n"
                    "  \"underused_patterns\": [\"...\"]\n"
                    "}"
                ),
                "output_keys": ["template_inventory"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "brainstorm_gaps",
            "label": "Brainstorm Gaps",
            "agent": "multiplier_node",
            "pos": 1,
            "config": {
                "n": 4,
                "collapser_mode": "judge_select",
                "required_input_keys": ["template_inventory"],
                "agents": [
                    {
                        "name": "software_specialist",
                        "system_prompt": (
                            "You are a software engineering domain specialist. Given the template "
                            "inventory, identify 3 missing pipeline capabilities that software "
                            "engineers working with this tool would most want. Think about: "
                            "code review workflows, dependency management, documentation generation, "
                            "migration safety, API contract testing, performance profiling. "
                            "Call submit_work with {\"domain\": \"software\", "
                            "\"gaps\": [{\"name\": \"...\", \"description\": \"...\", "
                            "\"estimated_value\": \"high|medium|low\", "
                            "\"sketch\": \"...\"}, ...]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "data_specialist",
                        "system_prompt": (
                            "You are a data science domain specialist. Given the template inventory, "
                            "identify 3 missing pipeline capabilities that data scientists and "
                            "analysts would want. Think about: dataset versioning, model evaluation, "
                            "statistical validation, ETL pipeline design, feature engineering, "
                            "A/B testing analysis (separate from Maestro's own self-improvement). "
                            "Call submit_work with {\"domain\": \"data\", "
                            "\"gaps\": [{\"name\": \"...\", \"description\": \"...\", "
                            "\"estimated_value\": \"high|medium|low\", "
                            "\"sketch\": \"...\"}, ...]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "reasoning_specialist",
                        "system_prompt": (
                            "You are a reasoning and knowledge-work domain specialist. Given the "
                            "template inventory, identify 3 missing pipeline capabilities for "
                            "knowledge workers: literature review, argument mapping, fact-checking "
                            "workflows, structured debate, multi-source synthesis, or similar. "
                            "Call submit_work with {\"domain\": \"reasoning\", "
                            "\"gaps\": [{\"name\": \"...\", \"description\": \"...\", "
                            "\"estimated_value\": \"high|medium|low\", "
                            "\"sketch\": \"...\"}, ...]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "infrastructure_specialist",
                        "system_prompt": (
                            "You are an infrastructure and operations specialist. Given the template "
                            "inventory, identify 3 missing pipeline capabilities for DevOps and "
                            "system management: infrastructure-as-code review, incident post-mortems, "
                            "capacity planning, runbook generation, change management, or similar. "
                            "Call submit_work with {\"domain\": \"infrastructure\", "
                            "\"gaps\": [{\"name\": \"...\", \"description\": \"...\", "
                            "\"estimated_value\": \"high|medium|low\", "
                            "\"sketch\": \"...\"}, ...]}"
                        ),
                        "max_turns": 12,
                    },
                ],
                "judge_system_prompt": (
                    "You are a product strategist selecting the most valuable set of capability "
                    "gaps to address. Select the analyst whose gaps are most strategically valuable "
                    "given the current template inventory: prefer gaps that are high-value, "
                    "implementable with existing agent types, and not redundant with existing templates. "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "identified_gaps",
            },
        },
        {
            "key": "deduplicate_gaps",
            "label": "Deduplicate Gaps",
            "agent": "generic_stage",
            "pos": 2,
            "config": {
                "system_prompt": (
                    "You are a backlog manager. Merge the identified gaps with any existing "
                    "backlog items.\n\n"
                    "1. Remove near-duplicate gaps (same workflow, different name)\n"
                    "2. Combine complementary gaps that would be better as one pipeline\n"
                    "3. Note overlaps with existing templates that partially address a gap\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"deduplicated_gaps\": [\n"
                    "    {\"name\": \"...\", \"description\": \"...\", \"domain\": \"...\", "
                    "\"merged_from\": [...], \"partial_coverage\": \"...\"}\n"
                    "  ]\n"
                    "}"
                ),
                "required_input_keys": ["identified_gaps"],
                "output_keys": ["deduplicated_gaps"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "prioritize",
            "label": "Prioritize",
            "agent": "multiplier_node",
            "pos": 3,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["deduplicated_gaps"],
                "agents": [
                    {
                        "name": "user_value_scorer",
                        "system_prompt": (
                            "You are a user value analyst. Score each capability gap 1–10 on "
                            "user value: how many users would benefit, how frequently, and how "
                            "significantly? High score = many users, frequent use, major impact. "
                            "Vote ACCEPTED if the top-ranked gaps are genuinely high-value. "
                            "Call submit_work with {\"scores\": [{\"gap\": \"...\", \"score\": N, "
                            "\"rationale\": \"...\"}], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "effort_scorer",
                        "system_prompt": (
                            "You are an implementation effort estimator. Score each capability gap "
                            "1–10 on implementation effort (1 = trivial, 10 = enormous): "
                            "requires new agent types, new infrastructure, external dependencies, "
                            "or major schema changes? "
                            "Vote ACCEPTED if gaps are estimated correctly — reject if effort "
                            "estimates seem wildly optimistic. "
                            "Call submit_work with {\"scores\": [{\"gap\": \"...\", \"score\": N, "
                            "\"rationale\": \"...\"}], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "strategic_fit_scorer",
                        "system_prompt": (
                            "You are a strategic alignment evaluator. Score each capability gap "
                            "1–10 on strategic fit: does it advance Maestro's core mission of "
                            "self-improving agentic orchestration? Does it strengthen the "
                            "self-improvement loop (benchmark → experiment → prompt refinement)? "
                            "Vote ACCEPTED if strategic fit is correctly assessed. "
                            "Call submit_work with {\"scores\": [{\"gap\": \"...\", \"score\": N, "
                            "\"rationale\": \"...\"}], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "priority_scores",
            },
        },
        {
            "key": "design_pipelines",
            "label": "Design Pipelines",
            "agent": "generic_stage",
            "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a pipeline architect. For the top 5 highest-priority gaps, "
                    "design a concrete pipeline template sketch.\n\n"
                    "For each:\n"
                    "- Name and purpose (1 sentence)\n"
                    "- Proposed stages (key, label, agent_type, brief description)\n"
                    "- Transitions (which stages feed into which)\n"
                    "- Why the proposed agent types are appropriate\n"
                    "- Estimated migration number (next available)\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"capability_backlog\": {\n"
                    "    \"gaps\": [\n"
                    "      {\"name\": \"...\", \"priority\": N, "
                    "\"effort_score\": N, \"strategic_score\": N, "
                    "\"pipeline_sketch\": {\"stages\": [...], \"transitions\": [...]}}\n"
                    "    ]\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["deduplicated_gaps", "priority_scores"],
                "output_keys": ["capability_backlog"],
                "max_turns": 20,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "reflection",
            "label": "Reflection",
            "agent": "reflection_agent",
            "pos": 5,
            "config": {
                "system_prompt": (
                    "You are a skeptical reviewer of the capability gap analysis. "
                    "Critique: Are the identified gaps genuinely missing, or do existing "
                    "templates already cover them? Are the priority scores honest, or do they "
                    "reflect the analyst's domain bias? Are the pipeline sketches implementable "
                    "with current agent types, or do they require new infrastructure? "
                    "Call submit_work with {\"confidence\": 0.0–1.0, \"issues\": [...], "
                    "\"uncertain_about\": [...]}"
                ),
                "gate_type": "single_pass",
                "max_turns": 12,
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 6,
        },
        {
            "key": "backlog_published",
            "label": "Backlog Published",
            "agent": "terminal",
            "pos": 7,
        },
    ],
    "transitions": [
        ("survey_templates",  "brainstorm_gaps",  "pass"),
        ("brainstorm_gaps",   "deduplicate_gaps", "pass"),
        ("deduplicate_gaps",  "prioritize",       "pass"),
        ("prioritize",        "design_pipelines", "pass"),
        ("prioritize",        "brainstorm_gaps",  "fail"),
        ("design_pipelines",  "reflection",       "pass"),
        ("reflection",        "human_review",     "pass"),
        ("human_review",      "backlog_published","pass"),
    ],
    "arch_categories": [
        "Template Inventory", "Identified Gaps", "Priority Rankings",
        "Pipeline Sketches", "Approved Backlog",
    ],
}


def _seed(conn, tpl):
    name = tpl["name"]
    conn.execute(
        """
        INSERT INTO pipeline_templates (name, description, is_default, is_builtin)
        VALUES (:name, :desc, FALSE, TRUE)
        ON CONFLICT (name) DO NOTHING
        """,
        {"name": name, "desc": tpl["description"]},
    )
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    if not row:
        return
    tid = row["id"]

    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_stages WHERE template_id = :tid", {"tid": tid}
    ).fetchone()["n"]
    if existing > 0:
        return

    stage_key_to_id = {}
    for s in tpl["stages"]:
        config_str = _json.dumps(s["config"]) if s.get("config") else None
        conn.execute(
            """
            INSERT INTO pipeline_stages
                (template_id, stage_key, label, agent_type, position, config)
            VALUES (:tid, :key, :label, :agent, :pos, CAST(:config AS jsonb))
            """,
            {
                "tid": tid, "key": s["key"], "label": s["label"],
                "agent": s["agent"], "pos": s["pos"], "config": config_str,
            },
        )
        row = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": s["key"]},
        ).fetchone()
        stage_key_to_id[s["key"]] = row["id"]

    for from_key, to_key, cond in tpl["transitions"]:
        from_id = stage_key_to_id.get(from_key)
        to_id = stage_key_to_id.get(to_key)
        if from_id and to_id:
            conn.execute(
                """
                INSERT INTO pipeline_transitions
                    (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :fid, :toid, :cond)
                ON CONFLICT DO NOTHING
                """,
                {"tid": tid, "fid": from_id, "toid": to_id, "cond": cond},
            )

    for pos, label in enumerate(tpl.get("arch_categories", [])):
        key = label.lower().replace("/", "_").replace(" ", "_")
        conn.execute(
            """
            INSERT INTO pipeline_arch_categories (template_id, key, label, position)
            VALUES (:tid, :key, :label, :pos)
            ON CONFLICT (template_id, key) DO NOTHING
            """,
            {"tid": tid, "key": key, "label": label, "pos": pos},
        )


def up(conn):
    _seed(conn, _TEMPLATE)


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name AND is_builtin = TRUE",
        {"name": _TEMPLATE["name"]},
    ).fetchone()
    if not row:
        return
    tid = row["id"]
    conn.execute("DELETE FROM pipeline_arch_categories WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_transitions WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_stages WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_templates WHERE id = :tid", {"tid": tid})
