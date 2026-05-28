description = "Experiment Analysis built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Experiment Analysis",
    "description": (
        "Ingest results from a completed A/B experiment and render a statistically grounded "
        "ADOPT / REJECT / RUN_LONGER verdict. Pairs with the Experiment Design pipeline."
    ),
    "stages": [
        {
            "key": "ingest_results",
            "label": "Ingest Results",
            "agent": "generic_stage",
            "pos": 0,
            "config": {
                "system_prompt": (
                    "You are a data ingestion analyst. Parse the benchmark scorecards from the "
                    "control and variant runs of a completed A/B experiment.\n\n"
                    "Extract from each arm:\n"
                    "- Task count (n)\n"
                    "- Pass rate on the primary metric\n"
                    "- Mean score and standard deviation (if scored)\n"
                    "- Secondary metric values (token cost, demotion rate, latency if available)\n"
                    "- Any anomalies: tasks that crashed, were manually overridden, or otherwise "
                    "contaminated the data\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"ingested_results\": {\n"
                    "    \"control\": {\"n\": N, \"pass_rate\": 0.0, \"mean_score\": 0.0, "
                    "\"std_score\": 0.0, \"secondary\": {...}, \"anomalies\": [...]},\n"
                    "    \"variant\": {\"n\": N, \"pass_rate\": 0.0, \"mean_score\": 0.0, "
                    "\"std_score\": 0.0, \"secondary\": {...}, \"anomalies\": [...]},\n"
                    "    \"experiment_spec_summary\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["ingested_results"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "statistical_test",
            "label": "Statistical Test",
            "agent": "generic_stage",
            "pos": 1,
            "config": {
                "system_prompt": (
                    "You are a statistician. Run the appropriate hypothesis test on the ingested "
                    "experiment results.\n\n"
                    "For pass/fail metrics: two-proportion z-test.\n"
                    "For continuous scores: Welch's t-test (unequal variance).\n\n"
                    "Compute:\n"
                    "- Effect size (Cohen's h for proportions, Cohen's d for means)\n"
                    "- p-value (two-tailed)\n"
                    "- 95% confidence interval for the difference\n"
                    "- Whether the minimum detectable effect size from the experiment spec was reached\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"statistical_results\": {\n"
                    "    \"test\": \"two_proportion_z|welch_t\",\n"
                    "    \"effect_size\": 0.0,\n"
                    "    \"p_value\": 0.0,\n"
                    "    \"ci_95\": [lower, upper],\n"
                    "    \"significant\": true|false,\n"
                    "    \"mde_reached\": true|false,\n"
                    "    \"interpretation\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["ingested_results"],
                "output_keys": ["statistical_results"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "regression_scan",
            "label": "Regression Scan",
            "agent": "multiplier_node",
            "pos": 2,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["ingested_results", "statistical_results"],
                "agents": [
                    {
                        "name": "primary_metric_guard",
                        "system_prompt": (
                            "You are a regression guard for the primary metric. "
                            "Did the variant degrade the primary metric compared to control, "
                            "even if the difference was not statistically significant? "
                            "Any degradation, even within noise, is a yellow flag. "
                            "Vote ACCEPTED if no primary metric degradation, REJECTED if degraded. "
                            "Call submit_work with {\"finding\": \"...\", \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 8,
                    },
                    {
                        "name": "secondary_metric_guard",
                        "system_prompt": (
                            "You are a regression guard for secondary metrics (token cost, demotion rate, "
                            "latency, error rate). Did the variant cause any secondary metric to worsen "
                            "significantly (>10% degradation)? "
                            "Vote ACCEPTED if secondary metrics are stable, REJECTED if any degraded > 10%. "
                            "Call submit_work with {\"findings\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 8,
                    },
                    {
                        "name": "anomaly_reviewer",
                        "system_prompt": (
                            "You are an anomaly reviewer. Review the ingested anomalies from both arms. "
                            "Are the anomalies balanced between control and variant, or do they "
                            "systematically affect one arm? Could the anomalies explain the results? "
                            "Vote ACCEPTED if anomalies are balanced/benign, REJECTED if they compromise "
                            "the integrity of the experiment. "
                            "Call submit_work with {\"anomaly_analysis\": \"...\", \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 8,
                    },
                ],
                "output_key": "regression_verdict",
            },
        },
        {
            "key": "render_verdict",
            "label": "Render Verdict",
            "agent": "multiplier_node",
            "pos": 3,
            "config": {
                "n": 3,
                "collapser_mode": "judge_select",
                "required_input_keys": ["statistical_results", "regression_verdict"],
                "agents": [
                    {
                        "name": "optimist",
                        "system_prompt": (
                            "You are an optimist analyst. Given the statistical results and regression "
                            "scan, argue the strongest case for ADOPT: significant improvement, no "
                            "regressions, effect size meaningful. If the data does not support ADOPT, "
                            "argue for RUN_LONGER if results are directionally positive but underpowered. "
                            "Call submit_work with {\"verdict\": \"ADOPT|RUN_LONGER|REJECT\", "
                            "\"rationale\": \"...\", \"confidence\": 0.0–1.0}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "skeptic",
                        "system_prompt": (
                            "You are a skeptical analyst. Given the statistical results and regression "
                            "scan, argue the strongest case for REJECT: p-value not significant, "
                            "effect size below MDE, regressions outweigh improvements, or data quality "
                            "concerns. If rejection is too strong, argue for RUN_LONGER. "
                            "Call submit_work with {\"verdict\": \"ADOPT|RUN_LONGER|REJECT\", "
                            "\"rationale\": \"...\", \"confidence\": 0.0–1.0}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "pragmatist",
                        "system_prompt": (
                            "You are a pragmatic decision-maker. Weigh the statistical evidence against "
                            "operational cost: deployment complexity, rollback risk, and the value of "
                            "the improvement. A marginal improvement with easy rollback may be worth "
                            "adopting; a large improvement with high deployment risk may need more data. "
                            "Call submit_work with {\"verdict\": \"ADOPT|RUN_LONGER|REJECT\", "
                            "\"rationale\": \"...\", \"confidence\": 0.0–1.0}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "judge_system_prompt": (
                    "You are a chief scientist selecting the most defensible verdict for this experiment. "
                    "Consider all three analyst perspectives. The verdict must be: "
                    "ADOPT (clear improvement, no regressions, significant result), "
                    "REJECT (no improvement or net harm), or "
                    "RUN_LONGER (directionally positive but underpowered). "
                    "Prioritise data quality over optimism. "
                    "Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
                ),
                "judge_max_turns": 10,
                "output_key": "verdict_recommendation",
            },
        },
        {
            "key": "reflection",
            "label": "Reflection",
            "agent": "reflection_agent",
            "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a skeptical reviewer of experiment analysis. "
                    "Critique: Was the statistical test appropriate for the data type? "
                    "Is the effect size practically meaningful even if significant? "
                    "Were the regression checks sufficient? Is the verdict well-supported by "
                    "the data, or are there confounders not accounted for? "
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
            "pos": 5,
        },
        {
            "key": "verdict_published",
            "label": "Verdict Published",
            "agent": "terminal",
            "pos": 6,
        },
    ],
    "transitions": [
        ("ingest_results",      "statistical_test",      "pass"),
        ("statistical_test",    "regression_scan",       "pass"),
        ("regression_scan",     "render_verdict",        "pass"),
        ("regression_scan",     "ingest_results",        "fail"),
        ("render_verdict",      "reflection",            "pass"),
        ("reflection",          "human_review",          "pass"),
        ("human_review",        "verdict_published",     "pass"),
    ],
    "arch_categories": [
        "Experiment Specs", "Raw Results", "Statistical Tests",
        "Regression Reports", "Verdicts",
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
