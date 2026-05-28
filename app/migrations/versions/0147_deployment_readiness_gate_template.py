description = "Deployment Readiness Gate built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Deployment Readiness Gate",
    "description": (
        "Before promoting Instance B (candidate) to replace Instance A (stable), run a "
        "structured multi-reviewer gate. Wraps blue/green promotion in an auditable decision."
    ),
    "stages": [
        {
            "key": "candidate_intake",
            "label": "Candidate Intake",
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
            "key": "smoke_test",
            "label": "Smoke Test",
            "agent": "generic_stage",
            "pos": 2,
            "config": {
                "system_prompt": (
                    "You are a smoke tester for a Maestro server instance. "
                    "The candidate instance (B) is running on a non-production port.\n\n"
                    "Test the following endpoints and record status code + response shape:\n"
                    "1. GET /api/projects — should return 200 with a list\n"
                    "2. GET /api/scheduler/status — should return 200\n"
                    "3. GET /api/pipelines — should return 200 with at least the built-in templates\n"
                    "4. GET /api/llms — should return 200\n"
                    "5. GET /api/budgets — should return 200\n\n"
                    "For each endpoint, verify the response schema matches the stable instance.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"smoke_test_results\": {\n"
                    "    \"endpoint_results\": [\n"
                    "      {\"endpoint\": \"...\", \"status\": N, \"schema_ok\": true|false, "
                    "\"notes\": \"...\"}\n"
                    "    ],\n"
                    "    \"all_passed\": true|false\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["smoke_test_results"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "migration_check",
            "label": "Migration Check",
            "agent": "generic_stage",
            "pos": 3,
            "config": {
                "system_prompt": (
                    "You are a database migration validator.\n\n"
                    "Verify:\n"
                    "1. All migrations on the candidate branch have been applied to the "
                    "test database (no PENDING or TAMPERED status)\n"
                    "2. All migrations are idempotent — re-running them would not corrupt data\n"
                    "3. Any new migrations have a valid down() function for rollback\n"
                    "4. No migration modifies a column type that is already populated\n"
                    "5. No migration drops a column or table without a prior deprecation period\n\n"
                    "Use the migration runner status output and git log to verify.\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"migration_check\": {\n"
                    "    \"applied_count\": N,\n"
                    "    \"pending_count\": N,\n"
                    "    \"issues\": [...],\n"
                    "    \"rollback_safe\": true|false,\n"
                    "    \"all_passed\": true|false\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["migration_check"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "benchmark_compare",
            "label": "Benchmark Compare",
            "agent": "generic_stage",
            "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a benchmark comparison analyst.\n\n"
                    "Compare the candidate instance's benchmark scorecard against the stable "
                    "instance's baseline scorecard.\n\n"
                    "For each benchmarked stage:\n"
                    "- Did pass rate improve, stay the same, or degrade?\n"
                    "- Did token cost change significantly (>15%)?\n"
                    "- Are there new failure modes not present in the baseline?\n\n"
                    "If no benchmark scorecard is available, note this and proceed with a "
                    "warning (benchmarks are recommended, not required for deployment).\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"benchmark_comparison\": {\n"
                    "    \"available\": true|false,\n"
                    "    \"stage_deltas\": [\n"
                    "      {\"stage\": \"...\", \"baseline_pass\": 0.0, \"candidate_pass\": 0.0, "
                    "\"delta\": 0.0, \"regressions\": [...]}\n"
                    "    ],\n"
                    "    \"overall_verdict\": \"improved|unchanged|degraded|unavailable\"\n"
                    "  }\n"
                    "}"
                ),
                "output_keys": ["benchmark_comparison"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "regression_scan",
            "label": "Regression Scan",
            "agent": "multiplier_node",
            "pos": 5,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["smoke_test_results", "migration_check", "benchmark_comparison"],
                "agents": [
                    {
                        "name": "data_safety_reviewer",
                        "system_prompt": (
                            "You are a data safety reviewer. Examine the git diff and migration list "
                            "for the candidate branch. Could any change cause data loss, silent "
                            "corruption, or incorrect reads from the database? Check in particular: "
                            "changes to ORM models, migration column alterations, changes to budget "
                            "entry accumulation logic, and changes to task soft-delete. "
                            "Vote ACCEPTED if no data safety concerns, REJECTED otherwise. "
                            "Call submit_work with {\"findings\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "api_compatibility_reviewer",
                        "system_prompt": (
                            "You are an API compatibility reviewer. Check the git diff for any "
                            "breaking changes to the API: renamed routes, removed fields from "
                            "responses, changed status codes, altered request schemas. The frontend "
                            "assumes stable API contracts. "
                            "Vote ACCEPTED if API is backward-compatible, REJECTED if breaking "
                            "changes are present. "
                            "Call submit_work with {\"findings\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "scheduler_safety_reviewer",
                        "system_prompt": (
                            "You are a scheduler safety reviewer. Check the git diff for changes to "
                            "the scheduler tick, LLM capacity management, task dispatch logic, or "
                            "agent session lifecycle. Could any change cause: double-dispatch, "
                            "stuck tasks, zombie sessions, or capacity misreporting? "
                            "Vote ACCEPTED if scheduler logic is safe, REJECTED if risky changes present. "
                            "Call submit_work with {\"findings\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "regression_scan_verdict",
            },
        },
        {
            "key": "final_gate",
            "label": "Final Gate",
            "agent": "multiplier_node",
            "pos": 6,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "reject",
                "required_input_keys": [
                    "smoke_test_results", "migration_check",
                    "benchmark_comparison", "regression_scan_verdict",
                ],
                "agents": [
                    {
                        "name": "promote_vote_1",
                        "system_prompt": (
                            "You are a deployment gatekeeper. Review all prior checks: smoke tests, "
                            "migration validation, benchmark comparison, and regression scan. "
                            "Vote ACCEPTED (promote) only if ALL of: smoke tests passed, no pending "
                            "migrations, no regression scan rejections, benchmark did not degrade. "
                            "Vote REJECTED (hold) if any check failed or if you have significant "
                            "unresolved concerns. Be conservative — a false HOLD is cheaper than "
                            "a bad deploy. "
                            "Call submit_work with {\"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"blocking_issues\": [...], \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "promote_vote_2",
                        "system_prompt": (
                            "You are an independent deployment gatekeeper. Review all prior checks "
                            "independently. Focus on: are there any partial failures or borderline "
                            "results that the other reviewers might have glossed over? "
                            "Vote ACCEPTED (promote) only if all checks clearly pass. "
                            "Vote REJECTED (hold) on any doubt. "
                            "Call submit_work with {\"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"blocking_issues\": [...], \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "promote_vote_3",
                        "system_prompt": (
                            "You are a risk-weighted deployment gatekeeper. Weight the risks: "
                            "a failed smoke test = hard block; a missing benchmark = soft warning; "
                            "a regression scan rejection = hard block; a benchmark degradation > 5% "
                            "= hard block; < 5% = soft warning. "
                            "Vote ACCEPTED only if there are no hard blocks. "
                            "Call submit_work with {\"verdict\": \"ACCEPTED|REJECTED\", "
                            "\"hard_blocks\": [...], \"soft_warnings\": [...], \"rationale\": \"...\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "final_gate_verdict",
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 7,
        },
        {
            "key": "promote_or_rollback",
            "label": "Promote or Rollback",
            "agent": "generic_stage",
            "pos": 8,
            "config": {
                "system_prompt": (
                    "You are executing the final deployment action.\n\n"
                    "Given human approval, produce the deployment record and the specific "
                    "Maestro system_settings commands to promote Instance B to be the "
                    "scheduler owner (or document why rollback was chosen instead).\n\n"
                    "The promotion involves:\n"
                    "1. Setting scheduler_owner = 'B' in system_settings\n"
                    "2. Confirming Instance A's scheduler sees the ownership change and stops\n"
                    "3. Logging the promotion event with timestamps and git SHAs\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"deployment_record\": {\n"
                    "    \"action\": \"promote|rollback\",\n"
                    "    \"from_sha\": \"...\",\n"
                    "    \"to_sha\": \"...\",\n"
                    "    \"verdict\": \"PROMOTED|ROLLED_BACK\",\n"
                    "    \"promoted_at\": \"ISO8601\",\n"
                    "    \"rollback_sha\": \"...\",\n"
                    "    \"operator_notes\": \"...\"\n"
                    "  }\n"
                    "}"
                ),
                "required_input_keys": ["final_gate_verdict"],
                "output_keys": ["deployment_record"],
                "max_turns": 12,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "deployment_complete",
            "label": "Deployment Complete",
            "agent": "terminal",
            "pos": 9,
        },
    ],
    "transitions": [
        ("candidate_intake",   "intake_gate",         "pass"),
        ("intake_gate",        "smoke_test",          "pass"),
        ("intake_gate",        "candidate_intake",    "fail"),
        ("smoke_test",         "migration_check",     "pass"),
        ("migration_check",    "benchmark_compare",   "pass"),
        ("benchmark_compare",  "regression_scan",     "pass"),
        ("regression_scan",    "final_gate",          "pass"),
        ("regression_scan",    "smoke_test",          "fail"),
        ("final_gate",         "human_review",        "pass"),
        ("final_gate",         "smoke_test",          "fail"),
        ("human_review",       "promote_or_rollback", "pass"),
        ("promote_or_rollback","deployment_complete", "pass"),
    ],
    "arch_categories": [
        "Candidate Builds", "Smoke Test Results", "Migration Logs",
        "Benchmark Comparisons", "Deployment Records",
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
