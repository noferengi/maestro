description = "Synthetic Training Data Factory built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Synthetic Training Data Factory",
    "description": (
        "Generate (input, ideal_output) pairs for a given stage spec. "
        "Output is a JSONL document in the project store for future fine-tuning use."
    ),
    "stages": [
        {
            "key": "spec_intake",
            "label": "Spec Intake",
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
                        "name": "common_case_generator",
                        "system_prompt": (
                            "You are a training data engineer. Generate a batch of 5 common-case "
                            "training inputs for the specified stage. These should be representative "
                            "of the most frequent, well-formed task requests the stage will encounter. "
                            "Vary domain and phrasing but keep difficulty low. "
                            "Call submit_work with {\"inputs\": [{\"id\": N, \"input\": \"...\", "
                            "\"category\": \"common_case\"}]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "edge_case_generator",
                        "system_prompt": (
                            "You are a training data engineer specialising in edge cases. "
                            "Generate a batch of 5 edge-case training inputs for the specified stage: "
                            "unusually large or small scope, ambiguous requirements, multi-step "
                            "requests, or requests that need clarification. "
                            "Call submit_work with {\"inputs\": [{\"id\": N, \"input\": \"...\", "
                            "\"category\": \"edge_case\"}]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "domain_diversity_generator",
                        "system_prompt": (
                            "You are a training data engineer focused on domain diversity. "
                            "Generate a batch of 5 inputs for the specified stage that span "
                            "diverse domains: at least 3 different application domains "
                            "(e.g. web dev, data science, creative writing, security, devops). "
                            "Call submit_work with {\"inputs\": [{\"id\": N, \"input\": \"...\", "
                            "\"category\": \"diverse_domain\", \"domain\": \"...\"}]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "adversarial_generator",
                        "system_prompt": (
                            "You are a red-team training data engineer. "
                            "Generate a batch of 5 adversarial inputs designed to test robustness: "
                            "contradictory requirements, invalid inputs, prompt injection attempts, "
                            "or requests outside the stage's scope. "
                            "Include the expected correct handling (reject, clarify, or redirect). "
                            "Call submit_work with {\"inputs\": [{\"id\": N, \"input\": \"...\", "
                            "\"category\": \"adversarial\", \"expected_handling\": \"...\"}]}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "difficulty_gradient_generator",
                        "system_prompt": (
                            "You are a training data engineer. Generate 5 inputs at graduated "
                            "difficulty levels for the specified stage: one trivial, one easy, "
                            "one medium, one hard, one expert-level. Label each clearly. "
                            "Call submit_work with {\"inputs\": [{\"id\": N, \"input\": \"...\", "
                            "\"category\": \"graduated\", \"difficulty\": \"trivial|easy|medium|hard|expert\"}]}"
                        ),
                        "max_turns": 12,
                    },
                ],
                "judge_system_prompt": (
                    "You are a training data curator. Select the most valuable set of inputs "
                    "for training a model to perform well on this stage. Prefer: diversity "
                    "of difficulty and domain, coverage of real failure modes, balance between "
                    "common and edge cases, and inputs that are clearly answerable "
                    "(not so ambiguous that ideal outputs are undefined). "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "training_inputs",
            },
        },
        {
            "key": "generate_outputs",
            "label": "Generate Ideal Outputs",
            "agent": "parallel_agents",
            "pos": 3,
            "config": {
                "agents": [
                    {
                        "name": "ideal_output_writer",
                        "system_prompt": (
                            "You are writing ideal training outputs for a pipeline stage. "
                            "For each input in the training set, produce the best possible output "
                            "the stage should generate — correct, complete, well-formatted, "
                            "and matching the stage's output schema exactly.\n\n"
                            "For adversarial inputs where the correct handling is to reject or "
                            "clarify, the ideal output should demonstrate that correct handling.\n\n"
                            "Call submit_work with {\"ideal_outputs\": ["
                            "{\"input_id\": N, \"output\": \"...\", "
                            "\"quality_notes\": \"...\", \"format_valid\": true|false}]}"
                        ),
                        "max_turns": 30,
                    },
                ],
                "output_key": "ideal_outputs",
                "max_turns": 30,
            },
        },
        {
            "key": "quality_filter",
            "label": "Quality Filter",
            "agent": "multiplier_node",
            "pos": 4,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["training_inputs", "ideal_outputs"],
                "agents": [
                    {
                        "name": "correctness_filter",
                        "system_prompt": (
                            "You are a training data quality filter focused on correctness. "
                            "Review each (input, ideal_output) pair. Reject pairs where the "
                            "ideal output is factually wrong, incomplete, or does not correctly "
                            "handle the input type. A model trained on wrong data learns wrong things. "
                            "Vote ACCEPTED if ≥80% of pairs are correct, REJECTED otherwise. "
                            "Call submit_work with {\"rejected_pairs\": [N, ...], "
                            "\"verdict\": \"ACCEPTED|REJECTED\", \"notes\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "format_filter",
                        "system_prompt": (
                            "You are a training data quality filter focused on format compliance. "
                            "Review each (input, ideal_output) pair. Reject pairs where the "
                            "ideal output does not conform to the stage's output schema: wrong keys, "
                            "wrong types, missing required fields. "
                            "Vote ACCEPTED if ≥80% of pairs are format-correct, REJECTED otherwise. "
                            "Call submit_work with {\"rejected_pairs\": [N, ...], "
                            "\"verdict\": \"ACCEPTED|REJECTED\", \"notes\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                    {
                        "name": "diversity_filter",
                        "system_prompt": (
                            "You are a training data quality filter focused on dataset diversity. "
                            "Review the full set of (input, ideal_output) pairs. Are there too "
                            "many near-duplicate inputs? Is domain coverage adequate? Are adversarial "
                            "cases represented? Vote ACCEPTED if the dataset is diverse enough to "
                            "be useful for training. Vote REJECTED if it is too homogeneous. "
                            "Call submit_work with {\"diversity_issues\": [...], "
                            "\"verdict\": \"ACCEPTED|REJECTED\", \"notes\": \"...\"}"
                        ),
                        "max_turns": 12,
                    },
                ],
                "output_key": "quality_verdict",
            },
        },
        {
            "key": "export_jsonl",
            "label": "Export JSONL",
            "agent": "generic_stage",
            "pos": 5,
            "config": {
                "system_prompt": (
                    "You are a dataset exporter. Assemble the approved (input, ideal_output) pairs "
                    "into a JSONL dataset and store it in the project document store.\n\n"
                    "Each JSONL line must be a valid JSON object with exactly:\n"
                    "  {\"messages\": [{\"role\": \"user\", \"content\": \"<input>\"}, "
                    "{\"role\": \"assistant\", \"content\": \"<ideal_output>\"}]}\n\n"
                    "Filter out any rejected pairs (from quality_verdict). "
                    "Compute dataset statistics.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"training_dataset\": {\n"
                    "    \"stage_key\": \"...\",\n"
                    "    \"n_pairs\": N,\n"
                    "    \"quality_mean\": 0.0,\n"
                    "    \"document_key\": \"training/<stage_key>_<timestamp>.jsonl\",\n"
                    "    \"jsonl_content\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["training_inputs", "ideal_outputs", "quality_verdict"],
                "output_keys": ["training_dataset"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 6,
        },
        {
            "key": "dataset_published",
            "label": "Dataset Published",
            "agent": "terminal",
            "pos": 7,
        },
    ],
    "transitions": [
        ("spec_intake",      "intake_gate",       "pass"),
        ("intake_gate",      "generate_inputs",   "pass"),
        ("intake_gate",      "spec_intake",       "fail"),
        ("generate_inputs",  "generate_outputs",  "pass"),
        ("generate_outputs", "quality_filter",    "pass"),
        ("quality_filter",   "export_jsonl",      "pass"),
        ("quality_filter",   "generate_inputs",   "fail"),
        ("export_jsonl",     "human_review",      "pass"),
        ("human_review",     "dataset_published", "pass"),
    ],
    "arch_categories": [
        "Stage Specs", "Training Inputs", "Ideal Outputs",
        "Quality Reports", "JSONL Datasets",
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
