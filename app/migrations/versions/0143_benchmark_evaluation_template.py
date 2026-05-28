description = "Benchmark / Evaluation built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Benchmark / Evaluation",
    "description": (
        "Measure how well a pipeline stage performs on a standardised synthetic task suite. "
        "Produces a structured scorecard. Prerequisite for all self-improvement loops."
    ),
    "stages": [
        {
            "key": "define_scope",
            "label": "Define Scope",
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
            "key": "generate_inputs",
            "label": "Generate Inputs",
            "agent": "multiplier_node",
            "pos": 2,
            "config": {
                "n": 5,
                "collapser_mode": "judge_select",
                "agents": [
                    {
                        "name": "easy_domain_a",
                        "system_prompt": (
                            "You are a test-case engineer. Generate a simple, well-formed synthetic task "
                            "input for the stage being benchmarked. The input should be representative of "
                            "an easy, common-case request a user might submit. Focus on clarity and "
                            "correctness of the input specification. "
                            "Call submit_work with a JSON object: {\"input\": \"...\", "
                            "\"difficulty\": \"easy\", \"domain\": \"<domain>\", \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "medium_domain_b",
                        "system_prompt": (
                            "You are a test-case engineer. Generate a moderately complex synthetic task "
                            "input for the stage being benchmarked. The input should require real reasoning "
                            "and cover a different domain than typical easy cases. "
                            "Call submit_work with a JSON object: {\"input\": \"...\", "
                            "\"difficulty\": \"medium\", \"domain\": \"<domain>\", \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "hard_edge_case",
                        "system_prompt": (
                            "You are a test-case engineer specialising in edge cases. Generate a difficult "
                            "synthetic task input that probes the limits of the stage: ambiguous requirements, "
                            "conflicting constraints, or an unusually large/complex scope. "
                            "Call submit_work with a JSON object: {\"input\": \"...\", "
                            "\"difficulty\": \"hard\", \"domain\": \"<domain>\", \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "adversarial",
                        "system_prompt": (
                            "You are a red-team engineer. Generate an adversarial synthetic task input "
                            "designed to expose failure modes: underspecified requirements, contradictory "
                            "constraints, or inputs that could trigger hallucination or refusal. "
                            "Call submit_work with a JSON object: {\"input\": \"...\", "
                            "\"difficulty\": \"adversarial\", \"domain\": \"<domain>\", \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "cross_domain",
                        "system_prompt": (
                            "You are a test-case engineer. Generate a cross-domain synthetic task input "
                            "that combines elements from two unrelated domains (e.g. data-science + "
                            "creative writing, or security + mathematics). Unusual combinations stress-test "
                            "generalisation. "
                            "Call submit_work with a JSON object: {\"input\": \"...\", "
                            "\"difficulty\": \"medium\", \"domain\": \"cross-domain\", \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "judge_system_prompt": (
                    "You are a benchmark designer reviewing five proposed test inputs for a pipeline stage. "
                    "Select the most valuable test suite: prefer diversity across difficulty levels and domains. "
                    "Reject inputs that are redundant, trivial, or fail to probe real failure modes. "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"why this set is most diagnostic\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "benchmark_inputs",
            },
        },
        {
            "key": "generate_ideal_outputs",
            "label": "Generate Ideal Outputs",
            "agent": "parallel_agents",
            "pos": 3,
            "config": {
                "agents": [
                    {
                        "name": "ideal_output_generator",
                        "system_prompt": (
                            "You are producing the ideal output for benchmark test cases. "
                            "Given the task inputs and the system prompt of the stage being benchmarked, "
                            "produce the best possible output that stage could generate for each input. "
                            "These become gold-standard references for scoring. "
                            "Call submit_work with {\"ideal_outputs\": [{\"input_id\": N, \"output\": \"...\", "
                            "\"quality_notes\": \"...\"}]}"
                        ),
                        "max_turns": 25,
                    },
                ],
                "output_key": "ideal_outputs",
                "max_turns": 25,
            },
        },
        {
            "key": "score_outputs",
            "label": "Score Outputs",
            "agent": "multiplier_node",
            "pos": 4,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["benchmark_inputs", "ideal_outputs"],
                "agents": [
                    {
                        "name": "correctness_judge",
                        "system_prompt": (
                            "You are a correctness evaluator. For each (input, ideal_output) pair in the "
                            "benchmark, score the actual stage output on correctness: does it address the "
                            "right problem, reach the right conclusions, and avoid factual errors? "
                            "Score 0–10 per pair. Vote ACCEPTED if mean score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"scores\": [{\"input_id\": N, \"score\": X, \"notes\": \"...\"}], "
                            "\"verdict\": \"ACCEPTED\" or \"REJECTED\"}"
                        ),
                        "max_turns": 15,
                    },
                    {
                        "name": "completeness_judge",
                        "system_prompt": (
                            "You are a completeness evaluator. For each (input, ideal_output) pair, score "
                            "the actual stage output on completeness: are all required elements present, "
                            "all sub-questions answered, all output_keys populated? "
                            "Score 0–10 per pair. Vote ACCEPTED if mean score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"scores\": [{\"input_id\": N, \"score\": X, \"notes\": \"...\"}], "
                            "\"verdict\": \"ACCEPTED\" or \"REJECTED\"}"
                        ),
                        "max_turns": 15,
                    },
                    {
                        "name": "format_judge",
                        "system_prompt": (
                            "You are a format compliance evaluator. For each (input, ideal_output) pair, "
                            "score the actual stage output on format compliance: correct JSON schema, "
                            "required keys present, no extra disallowed fields, valid types. "
                            "Score 0–10 per pair. Vote ACCEPTED if mean score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"scores\": [{\"input_id\": N, \"score\": X, \"notes\": \"...\"}], "
                            "\"verdict\": \"ACCEPTED\" or \"REJECTED\"}"
                        ),
                        "max_turns": 15,
                    },
                ],
                "output_key": "scoring_results",
            },
        },
        {
            "key": "aggregate_scorecard",
            "label": "Aggregate Scorecard",
            "agent": "generic_stage",
            "pos": 5,
            "config": {
                "system_prompt": (
                    "You are a benchmark analyst. Aggregate the scoring results into a structured scorecard.\n\n"
                    "Compute per-dimension (correctness, completeness, format) and overall pass rates.\n"
                    "Cluster failures by type: what patterns caused the most failures?\n"
                    "Estimate average token consumption per task from budget context if available.\n\n"
                    "Call submit_work with JSON matching exactly:\n"
                    "{\n"
                    "  \"benchmark_scorecard\": {\n"
                    "    \"template_name\": \"...\",\n"
                    "    \"stage_key\": \"...\",\n"
                    "    \"n_tasks\": N,\n"
                    "    \"pass_rate\": 0.0,\n"
                    "    \"dimension_scores\": {\"correctness\": 0.0, \"completeness\": 0.0, \"format\": 0.0},\n"
                    "    \"avg_score\": 0.0,\n"
                    "    \"failure_modes\": [{\"type\": \"...\", \"count\": N, \"examples\": [...]}],\n"
                    "    \"recommendations\": [\"...\"]\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["benchmark_scorecard"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "reflection",
            "label": "Reflection",
            "agent": "reflection_agent",
            "pos": 6,
            "config": {
                "system_prompt": (
                    "You are a skeptical reviewer of benchmark methodology. "
                    "Critique: Are the test inputs truly diverse? Are the scoring criteria appropriate "
                    "and unbiased? Could pass rates be artificially inflated by easy test cases? "
                    "Are the failure-mode clusters meaningful or just noise? "
                    "Call submit_work with {\"confidence\": 0.0–1.0, \"issues\": [...], \"uncertain_about\": [...]}"
                ),
                "gate_type": "single_pass",
                "max_turns": 15,
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 7,
        },
        {
            "key": "scorecard_published",
            "label": "Scorecard Published",
            "agent": "terminal",
            "pos": 8,
        },
    ],
    "transitions": [
        ("define_scope",           "intake_gate",            "pass"),
        ("intake_gate",            "generate_inputs",        "pass"),
        ("intake_gate",            "define_scope",           "fail"),
        ("generate_inputs",        "generate_ideal_outputs", "pass"),
        ("generate_ideal_outputs", "score_outputs",          "pass"),
        ("score_outputs",          "aggregate_scorecard",    "pass"),
        ("score_outputs",          "generate_inputs",        "fail"),
        ("aggregate_scorecard",    "reflection",             "pass"),
        ("reflection",             "human_review",           "pass"),
        ("human_review",           "scorecard_published",    "pass"),
    ],
    "arch_categories": [
        "Benchmark Inputs", "Scoring Rubric", "Failure Modes",
        "Baseline Scores", "Improvement Candidates",
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
