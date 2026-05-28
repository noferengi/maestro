description = "Experiment Design built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Experiment Design",
    "description": (
        "Turn a hypothesis about improving a stage into a concrete, reviewable A/B test spec. "
        "Output feeds into blue/green deployment and the Experiment Analysis pipeline."
    ),
    "stages": [
        {
            "key": "hypothesis_intake",
            "label": "Hypothesis Intake",
            "agent": "intake_scope",
            "pos": 0,
        },
        {
            "key": "intake_gate",
            "label": "Intake Gate",
            "agent": "intake_gate",
            "pos": 1,
        },
        {
            "key": "identify_metric",
            "label": "Identify Metric",
            "agent": "generic_stage",
            "pos": 2,
            "config": {
                "system_prompt": (
                    "You are an experimental methods analyst. Given the hypothesis, define the "
                    "primary metric that will prove or disprove it.\n\n"
                    "The metric must be:\n"
                    "- Measurable from existing pipeline data (budget entries, transition results, "
                    "gate verdicts, scoring results)\n"
                    "- Sensitive to the change being tested\n"
                    "- Resistant to confounding factors\n\n"
                    "Also define secondary metrics (e.g. token cost, latency, demotion rate) "
                    "and specify what a meaningful improvement looks like (effect size).\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"metric\": {\n"
                    "    \"name\": \"...\",\n"
                    "    \"definition\": \"...\",\n"
                    "    \"data_source\": \"...\",\n"
                    "    \"minimum_effect_size\": \"...\",\n"
                    "    \"secondary_metrics\": [\"...\"]\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["metric"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "design_variants",
            "label": "Design Variants",
            "agent": "multiplier_node",
            "pos": 3,
            "config": {
                "n": 3,
                "collapser_mode": "judge_select",
                "required_input_keys": ["metric"],
                "agents": [
                    {
                        "name": "prompt_variant",
                        "system_prompt": (
                            "You are an A/B test designer. Design a variant that tests the hypothesis "
                            "through a system-prompt change only — no code changes, no schema changes. "
                            "Specify exactly: what changes in the prompt, what stays the same, "
                            "and how to measure the effect on the primary metric. "
                            "Call submit_work with {\"variant_type\": \"prompt_change\", "
                            "\"control_config\": {...}, \"variant_config\": {...}, "
                            "\"change_description\": \"...\", \"expected_effect\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "config_variant",
                        "system_prompt": (
                            "You are an A/B test designer. Design a variant that tests the hypothesis "
                            "through a pipeline stage configuration change (gate_type, max_turns, "
                            "tool_allowlist, n, collapser_mode, etc.) — no new code required. "
                            "Specify exactly: what config keys change, what the new values are, "
                            "and how to measure the effect on the primary metric. "
                            "Call submit_work with {\"variant_type\": \"config_change\", "
                            "\"control_config\": {...}, \"variant_config\": {...}, "
                            "\"change_description\": \"...\", \"expected_effect\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "agent_type_variant",
                        "system_prompt": (
                            "You are an A/B test designer. Design a variant that tests the hypothesis "
                            "through changing the agent_type of the stage (e.g. generic_stage → "
                            "multiplier_node, or different collapser_mode). This is the highest-risk "
                            "variant — specify migration steps carefully. "
                            "Call submit_work with {\"variant_type\": \"agent_type_change\", "
                            "\"control_config\": {...}, \"variant_config\": {...}, "
                            "\"migration_required\": true|false, "
                            "\"change_description\": \"...\", \"expected_effect\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                ],
                "judge_system_prompt": (
                    "You are a senior engineer evaluating three A/B test designs. Select the variant "
                    "most likely to isolate the hypothesis signal: prefer minimal changes (fewer "
                    "variables = cleaner signal), reversibility (easy rollback), and low deployment "
                    "risk. A prompt-only change is preferred over a config change, which is preferred "
                    "over an agent-type change, unless the hypothesis specifically requires the latter. "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "experiment_variant",
            },
        },
        {
            "key": "sample_size_calc",
            "label": "Sample Size Calculation",
            "agent": "generic_stage",
            "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a statistical power analyst. Given the metric definition and expected "
                    "effect size, compute the minimum number of tasks needed to detect the effect "
                    "at 80% power and 95% confidence.\n\n"
                    "Use the appropriate test: two-proportion z-test for pass/fail metrics, "
                    "t-test for continuous scores. Account for:\n"
                    "- Expected baseline pass rate (estimate from benchmark scorecard if available)\n"
                    "- Minimum detectable effect size\n"
                    "- Realistic task throughput per day\n\n"
                    "Also estimate calendar time to complete the experiment.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"sample_size\": {\n"
                    "    \"n_per_arm\": N,\n"
                    "    \"total_n\": N,\n"
                    "    \"power\": 0.8,\n"
                    "    \"alpha\": 0.05,\n"
                    "    \"estimated_days\": N,\n"
                    "    \"assumptions\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["metric", "experiment_variant"],
                "output_keys": ["sample_size"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "risk_assessment",
            "label": "Risk Assessment",
            "agent": "multiplier_node",
            "pos": 5,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["experiment_variant"],
                "agents": [
                    {
                        "name": "regression_risk",
                        "system_prompt": (
                            "You are a regression risk analyst. Review the proposed experiment variant. "
                            "What baseline metrics could degrade? What adjacent stages could be affected "
                            "by this change? Is rollback straightforward if the variant is worse? "
                            "Vote ACCEPTED if risks are manageable, REJECTED if the experiment is too "
                            "dangerous to run without code review first. "
                            "Call submit_work with {\"risks\": [...], \"rollback_plan\": \"...\", "
                            "\"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "data_contamination_risk",
                        "system_prompt": (
                            "You are a data integrity analyst. Review the proposed experiment. "
                            "Could running the variant contaminate production data, shared document "
                            "stores, or task histories in ways that would corrupt the control arm? "
                            "Is there a clean isolation boundary between control and variant tasks? "
                            "Vote ACCEPTED if data is cleanly isolated, REJECTED if contamination "
                            "is likely. "
                            "Call submit_work with {\"risks\": [...], \"isolation_plan\": \"...\", "
                            "\"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "resource_risk",
                        "system_prompt": (
                            "You are a resource capacity analyst. Review the proposed experiment. "
                            "How much additional LLM capacity does running N variant tasks require? "
                            "Could this crowd out normal production work? Is the token cost justified "
                            "by the expected learning? "
                            "Vote ACCEPTED if resource cost is acceptable, REJECTED if it would "
                            "significantly impact production. "
                            "Call submit_work with {\"risks\": [...], \"cost_estimate\": \"...\", "
                            "\"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "risk_verdict",
            },
        },
        {
            "key": "write_spec",
            "label": "Write Spec",
            "agent": "generic_stage",
            "pos": 6,
            "config": {
                "system_prompt": (
                    "You are writing the final experiment specification document. "
                    "Assemble all prior outputs into a complete, unambiguous spec that a "
                    "Maestro operator can follow to run the experiment.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"experiment_spec\": {\n"
                    "    \"hypothesis\": \"...\",\n"
                    "    \"metric\": {...},\n"
                    "    \"control_config\": {...},\n"
                    "    \"variant_config\": {...},\n"
                    "    \"n_tasks\": N,\n"
                    "    \"success_criteria\": \"...\",\n"
                    "    \"risks\": [...],\n"
                    "    \"rollback_plan\": \"...\",\n"
                    "    \"estimated_days\": N\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["metric", "experiment_variant", "sample_size", "risk_verdict"],
                "output_keys": ["experiment_spec"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 7,
        },
        {
            "key": "spec_approved",
            "label": "Spec Approved",
            "agent": "terminal",
            "pos": 8,
        },
    ],
    "transitions": [
        ("hypothesis_intake", "intake_gate",      "pass"),
        ("intake_gate",       "identify_metric",  "pass"),
        ("intake_gate",       "hypothesis_intake","fail"),
        ("identify_metric",   "design_variants",  "pass"),
        ("design_variants",   "sample_size_calc", "pass"),
        ("sample_size_calc",  "risk_assessment",  "pass"),
        ("risk_assessment",   "write_spec",        "pass"),
        ("risk_assessment",   "design_variants",   "fail"),
        ("write_spec",        "human_review",      "pass"),
        ("human_review",      "spec_approved",     "pass"),
    ],
    "arch_categories": [
        "Hypotheses", "Metrics", "Variants", "Risk Log",
        "Experiment Specs", "Results",
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
