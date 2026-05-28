description = "Prompt Refinement built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Prompt Refinement",
    "description": (
        "Given a stage's failure history, generate and score improved system-prompt variants. "
        "Output: a new stage.config.system_prompt plus a migration scaffold to apply it. "
        "Highest-ROI self-improvement pipeline — no code change required."
    ),
    "stages": [
        {
            "key": "failure_intake",
            "label": "Failure Intake",
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
            "key": "analyze_failures",
            "label": "Analyze Failures",
            "agent": "generic_stage",
            "pos": 2,
            "config": {
                "system_prompt": (
                    "You are a root-cause analyst for LLM pipeline stages.\n\n"
                    "You have been given a failure history for a specific pipeline stage: "
                    "task outputs that were rejected, demoted, or scored below threshold.\n\n"
                    "Your job:\n"
                    "1. Cluster the failures by type (format error, incomplete output, "
                    "factual error, hallucination, missing required key, wrong schema, etc.).\n"
                    "2. For each cluster, identify the specific phrase or structure in the "
                    "current system prompt that likely caused the failure (or the absence "
                    "of a constraint that should have been there).\n"
                    "3. Rank clusters by frequency and severity.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"failure_analysis\": {\n"
                    "    \"stage_key\": \"...\",\n"
                    "    \"total_failures\": N,\n"
                    "    \"clusters\": [\n"
                    "      {\"type\": \"...\", \"count\": N, \"root_cause\": \"...\", "
                    "\"prompt_gap\": \"...\", \"examples\": [...]}\n"
                    "    ],\n"
                    "    \"priority_fix\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["failure_analysis"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "propose_variants",
            "label": "Propose Variants",
            "agent": "multiplier_node",
            "pos": 3,
            "config": {
                "n": 4,
                "collapser_mode": "judge_select",
                "required_input_keys": ["failure_analysis"],
                "agents": [
                    {
                        "name": "constraint_specialist",
                        "system_prompt": (
                            "You are a prompt engineer specialising in output constraints.\n"
                            "You have the original system prompt and a failure analysis showing "
                            "what went wrong. Your approach: add explicit, unambiguous constraints "
                            "that prevent the most common failure modes. Use JSON schema examples, "
                            "\"must include\"/\"must not include\" clauses, and explicit field "
                            "definitions. Do not change the core task description.\n"
                            "Call submit_work with {\"variant_name\": \"constraint_specialist\", "
                            "\"revised_prompt\": \"...\", \"changes_made\": \"...\", "
                            "\"failures_addressed\": [...]}"
                        ),
                        "max_turns": 15,
                    },
                    {
                        "name": "clarity_specialist",
                        "system_prompt": (
                            "You are a prompt engineer specialising in clarity and unambiguous intent.\n"
                            "You have the original system prompt and a failure analysis. Your approach: "
                            "rewrite ambiguous instructions into step-by-step numbered lists, "
                            "replace vague adjectives with measurable criteria, and add explicit "
                            "examples of correct and incorrect outputs. Preserve all constraints.\n"
                            "Call submit_work with {\"variant_name\": \"clarity_specialist\", "
                            "\"revised_prompt\": \"...\", \"changes_made\": \"...\", "
                            "\"failures_addressed\": [...]}"
                        ),
                        "max_turns": 15,
                    },
                    {
                        "name": "adversarial_hardening",
                        "system_prompt": (
                            "You are a prompt engineer specialising in adversarial hardening.\n"
                            "You have the original system prompt and a failure analysis showing "
                            "edge cases and adversarial failures. Your approach: add explicit "
                            "handling for ambiguous inputs, contradictory requirements, and "
                            "underspecified requests. Add a fallback instruction: what to do "
                            "when the input is unclear. Add guards against hallucination.\n"
                            "Call submit_work with {\"variant_name\": \"adversarial_hardening\", "
                            "\"revised_prompt\": \"...\", \"changes_made\": \"...\", "
                            "\"failures_addressed\": [...]}"
                        ),
                        "max_turns": 15,
                    },
                    {
                        "name": "conciseness_specialist",
                        "system_prompt": (
                            "You are a prompt engineer specialising in conciseness and token efficiency.\n"
                            "You have the original system prompt and a failure analysis. Your approach: "
                            "eliminate redundant instructions, merge overlapping requirements, and "
                            "rewrite verbose clauses as terse bullets. Shorter prompts reduce "
                            "context window pressure and improve instruction-following accuracy. "
                            "Do not remove any functional constraint — only remove verbosity.\n"
                            "Call submit_work with {\"variant_name\": \"conciseness_specialist\", "
                            "\"revised_prompt\": \"...\", \"changes_made\": \"...\", "
                            "\"failures_addressed\": [...]}"
                        ),
                        "max_turns": 15,
                    },
                ],
                "judge_system_prompt": (
                    "You are a senior prompt engineer selecting the best revised system prompt. "
                    "Evaluate each variant on: (1) how completely it addresses the identified "
                    "failure modes, (2) clarity and unambiguity, (3) token efficiency, "
                    "(4) risk of introducing new failure modes. "
                    "Select the variant most likely to improve stage pass rate. "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "winning_variant",
            },
        },
        {
            "key": "adversarial_test",
            "label": "Adversarial Test",
            "agent": "generic_stage",
            "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a prompt tester. You have the winning revised prompt and the original "
                    "failure cases from the failure analysis.\n\n"
                    "For each failure case, simulate what the LLM would likely produce given the "
                    "new prompt. Identify whether the revision fixes the failure or whether a new "
                    "failure mode was introduced.\n\n"
                    "Also generate 3 new adversarial inputs not in the original failure set and "
                    "simulate the output under the new prompt.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"test_results\": [\n"
                    "    {\"case\": \"...\", \"original_failure\": \"...\", "
                    "\"new_prompt_outcome\": \"pass|fail\", \"notes\": \"...\"}\n"
                    "  ],\n"
                    "  \"new_failure_modes\": [\"...\"]\n"
                    "}"
                ),
                "required_input_keys": ["failure_analysis", "winning_variant"],
                "output_keys": ["test_results"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "score_variants",
            "label": "Score Variants",
            "agent": "multiplier_node",
            "pos": 5,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["winning_variant", "test_results"],
                "agents": [
                    {
                        "name": "clarity_scorer",
                        "system_prompt": (
                            "You are evaluating a revised system prompt for clarity. "
                            "Score 0–10: Is every instruction unambiguous? Are requirements "
                            "stated in measurable terms? Would an LLM with no context understand "
                            "exactly what to do? Vote ACCEPTED if score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"score\": N, \"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"notes\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "completeness_scorer",
                        "system_prompt": (
                            "You are evaluating a revised system prompt for completeness. "
                            "Score 0–10: Does it address all identified failure modes? Are all "
                            "required output keys specified? Are error cases handled? "
                            "Vote ACCEPTED if score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"score\": N, \"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"notes\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "resilience_scorer",
                        "system_prompt": (
                            "You are evaluating a revised system prompt for adversarial resilience. "
                            "Review the test_results to see how the new prompt performed on "
                            "adversarial inputs. Score 0–10: Did it pass the original failures? "
                            "Did it introduce new failure modes? "
                            "Vote ACCEPTED if score >= 7, REJECTED otherwise. "
                            "Call submit_work with {\"score\": N, \"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"notes\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "scoring_verdict",
            },
        },
        {
            "key": "select_winner",
            "label": "Select Winner",
            "agent": "generic_stage",
            "pos": 6,
            "config": {
                "system_prompt": (
                    "You are finalising the prompt refinement. You have the winning variant and "
                    "its scores. Produce the final approved system prompt and a concise diff "
                    "against the original showing exactly what changed and why.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"refined_prompt\": {\n"
                    "    \"stage_key\": \"...\",\n"
                    "    \"original_prompt\": \"...\",\n"
                    "    \"new_prompt\": \"...\",\n"
                    "    \"diff_summary\": \"...\",\n"
                    "    \"failure_modes_addressed\": [...]\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["failure_analysis", "winning_variant"],
                "output_keys": ["refined_prompt"],
                "max_turns": 10,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "scaffold_migration",
            "label": "Scaffold Migration",
            "agent": "generic_stage",
            "pos": 7,
            "config": {
                "system_prompt": (
                    "You are a Maestro migration writer. Given the refined prompt and the stage "
                    "being updated, produce a complete, ready-to-apply migration file.\n\n"
                    "The migration must:\n"
                    "1. Use the scaffolder pattern: description, up(conn), down(conn)\n"
                    "2. In up(): UPDATE pipeline_stages SET config = jsonb_set(config, "
                    "'{system_prompt}', :new_prompt::jsonb) WHERE stage_key = :key "
                    "AND template_id = (SELECT id FROM pipeline_templates WHERE name = :tpl)\n"
                    "3. In down(): restore the original prompt\n"
                    "4. Include a comment explaining what failure modes the new prompt addresses\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"migration_sql\": \"...\",\n"
                    "  \"migration_file_content\": \"...\"\n"
                    "}"
                ),
                "required_input_keys": ["refined_prompt"],
                "output_keys": ["migration_sql"],
                "max_turns": 10,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 8,
        },
        {
            "key": "prompt_published",
            "label": "Prompt Published",
            "agent": "terminal",
            "pos": 9,
        },
    ],
    "transitions": [
        ("failure_intake",    "intake_gate",       "pass"),
        ("intake_gate",       "analyze_failures",  "pass"),
        ("intake_gate",       "failure_intake",    "fail"),
        ("analyze_failures",  "propose_variants",  "pass"),
        ("propose_variants",  "adversarial_test",  "pass"),
        ("adversarial_test",  "score_variants",    "pass"),
        ("score_variants",    "select_winner",     "pass"),
        ("score_variants",    "propose_variants",  "fail"),
        ("select_winner",     "scaffold_migration","pass"),
        ("scaffold_migration","human_review",      "pass"),
        ("human_review",      "prompt_published",  "pass"),
    ],
    "arch_categories": [
        "Failure Logs", "Prompt Versions", "Test Cases",
        "Scoring Results", "Migration Scripts",
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
