description = "SW Dev: split optimization into optimization_propose (optimization_node) + optimization_implement (dangerous_edit_llm_agent)"

import json as _json

_PROPOSER_TOOLS = [
    "read_file", "read_file_metadata", "find_in_files", "find_files",
    "find_symbol", "find_callers", "list_directory", "submit_work",
]

_INDEV_AGENT_TOOLS = [
    "read_file", "read_file_metadata", "read_last_output",
    "write_file", "append_file", "patch_file", "move_file",
    "list_directory", "find_in_files", "find_files", "find_symbol",
    "find_callers", "find_imports_of", "write_archive",
    "read_git_status", "read_git_diff", "read_git_log", "read_git_blame",
    "read_git_show", "read_diff_stat",
    "write_git_branch", "write_git_commit", "write_git_checkout", "write_git_restore",
    "get_task", "list_tasks", "write_task_status", "write_task_history",
    "write_arch_doc", "write_mermaid", "write_interface_contract",
    "spawn_research_agent", "write_benchmark",
    "run_test_pytest", "run_check_mypy", "run_check_ruff", "run_check_black",
    "run_test_unittest", "run_test_npm", "run_test_cargo", "run_test_go",
    "read_test_summary",
    "run_build_make", "run_build_cargo", "run_build_go", "run_build_npm",
    "run_build_tsc", "run_build_gradle", "run_build_mvn",
    "run_deps_pip", "run_deps_npm", "run_deps_cargo",
    "consult_maestro", "report_tool_bug", "submit_work",
    "query_episodes", "ask_agent", "list_active_sessions",
]

_PROPOSE_CONFIG = {
    "proposal_personas": [
        {
            "name": "algorithmic",
            "system_prompt": (
                "You are an algorithmic optimization expert. Analyze data structure choices "
                "and complexity. Read the code, identify the highest-complexity operations, "
                "then propose a concrete algorithmic improvement. "
                "Call submit_work with your proposal JSON."
            ),
        },
        {
            "name": "dependency",
            "system_prompt": (
                "You are a dependency optimization expert. Analyze import graph and coupling. "
                "Identify slow or redundant dependencies and propose removal or lazy-loading. "
                "Call submit_work with your proposal JSON."
            ),
        },
        {
            "name": "memory",
            "system_prompt": (
                "You are a memory optimization expert. Analyze memory allocation patterns. "
                "Identify object churn, large retained structures, or missed opportunities "
                "for streaming. Call submit_work with your proposal JSON."
            ),
        },
        {
            "name": "distribution",
            "system_prompt": (
                "You are a parallelism optimization expert. Analyze parallelism opportunities. "
                "Identify CPU-bound loops that could use asyncio, threading, or multiprocessing. "
                "Call submit_work with your proposal JSON."
            ),
        },
        {
            "name": "bit_level",
            "system_prompt": (
                "You are a micro-optimization expert. Analyze low-level efficiency: "
                "string formatting, repeated attribute lookups, unnecessary copies. "
                "Propose micro-optimizations. Call submit_work with your proposal JSON."
            ),
        },
    ],
    "proposer_tools": _PROPOSER_TOOLS,
    "proposer_max_turns": 20,
    "judge_count": 3,
    "judge_max_turns": 10,
    "judge_system_prompt": (
        "You are a performance expert judging optimization proposals. "
        "Score each proposal on feasibility, estimated impact, and implementation risk. "
        "Pick the best one. Output JSON: {\"winner_index\": N, \"rationale\": \"...\"}"
    ),
    "min_improvement_pct": 10.0,
    "output_key": "winning_optimization_proposal",
}

_IMPLEMENT_CONFIG = {
    "required_input_keys": ["winning_optimization_proposal"],
    "agent_tools": _INDEV_AGENT_TOOLS,
    "max_turns": 150,
}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id, position, config, agent_type FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0120] WARNING: 'Software Development' template not found — skipping.")
        return

    opt_row = _get_stage(conn, tid, "optimization")
    if not opt_row:
        print("[0120] WARNING: 'optimization' stage not found — skipping.")
        return

    opt_id = opt_row["id"]
    opt_pos = float(opt_row["position"])

    # Determine position for optimization_implement (between optimization and reflection)
    refl_row = _get_stage(conn, tid, "reflection")
    if refl_row:
        impl_pos = (opt_pos + float(refl_row["position"])) / 2.0
    else:
        impl_pos = opt_pos + 0.5

    # 1. Update optimization stage → optimization_propose with optimization_node
    conn.execute(
        "UPDATE pipeline_stages "
        "SET stage_key = 'optimization_propose', label = 'Optimize: Propose', "
        "    agent_type = 'optimization_node', config = CAST(:cfg AS jsonb) "
        "WHERE id = :sid",
        {"cfg": _json.dumps(_PROPOSE_CONFIG), "sid": opt_id},
    )
    print("[0120] optimization -> optimization_propose (optimization_node).")

    # 2. Insert optimization_implement stage
    conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:tid, 'optimization_implement', 'Optimize: Implement', "
        "        'dangerous_edit_llm_agent', :pos, CAST(:cfg AS jsonb))",
        {"tid": tid, "pos": impl_pos, "cfg": _json.dumps(_IMPLEMENT_CONFIG)},
    )
    impl_row = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'optimization_implement'",
        {"tid": tid},
    ).fetchone()
    impl_id = impl_row["id"]
    print(f"[0120] Inserted optimization_implement stage at position {impl_pos}.")

    # 3. Get IDs for wiring
    refl_row = _get_stage(conn, tid, "reflection")
    security_row = _get_stage(conn, tid, "security")
    indev_row = _get_stage(conn, tid, "indev")
    cr_row = _get_stage(conn, tid, "conceptual_review")

    refl_id = refl_row["id"] if refl_row else None
    security_id = security_row["id"] if security_row else None
    indev_id = indev_row["id"] if indev_row else None
    cr_id = cr_row["id"] if cr_row else None

    # 4. Remove old optimization transitions
    conn.execute(
        "DELETE FROM pipeline_transitions WHERE from_stage_id = :sid",
        {"sid": opt_id},
    )
    # Remove conceptual_review → optimization (pass)
    if cr_id:
        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE from_stage_id = :from_id AND to_stage_id = :to_id",
            {"from_id": cr_id, "to_id": opt_id},
        )

    # 5. Wire optimization_propose transitions
    # pass → optimization_implement
    conn.execute(
        "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
        {"tid": tid, "from_id": opt_id, "to_id": impl_id},
    )
    # skip → reflection (or security if no reflection)
    skip_target_id = refl_id or security_id
    if skip_target_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'skip', 0)",
            {"tid": tid, "from_id": opt_id, "to_id": skip_target_id},
        )
    # fail → indev
    if indev_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'fail', 0)",
            {"tid": tid, "from_id": opt_id, "to_id": indev_id},
        )
    # reject → indev
    if indev_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'reject', 0)",
            {"tid": tid, "from_id": opt_id, "to_id": indev_id},
        )

    # 6. Wire optimization_implement transitions
    # pass → reflection (or security)
    pass_target_id = refl_id or security_id
    if pass_target_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
            {"tid": tid, "from_id": impl_id, "to_id": pass_target_id},
        )
    # fail/reject → indev
    if indev_id:
        for cond in ("fail", "reject"):
            conn.execute(
                "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
                "VALUES (:tid, :from_id, :to_id, :cond, 0)",
                {"tid": tid, "from_id": impl_id, "to_id": indev_id, "cond": cond},
            )

    # 7. Rewire conceptual_review pass → optimization_propose
    if cr_id:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
            {"tid": tid, "from_id": cr_id, "to_id": opt_id},
        )

    print("[0120] Transitions wired for optimization_propose and optimization_implement.")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    prop_row = _get_stage(conn, tid, "optimization_propose")
    impl_row = _get_stage(conn, tid, "optimization_implement")

    if not prop_row:
        return

    prop_id = prop_row["id"]
    impl_id = impl_row["id"] if impl_row else None

    # Remove optimization_implement
    if impl_id:
        conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid OR to_stage_id = :sid",
                     {"sid": impl_id})
        conn.execute("DELETE FROM pipeline_stages WHERE id = :sid", {"sid": impl_id})

    # Revert optimization_propose → optimization
    conn.execute(
        "UPDATE pipeline_stages "
        "SET stage_key = 'optimization', label = 'Optimization', "
        "    agent_type = 'optimization_agent', config = NULL "
        "WHERE id = :sid",
        {"sid": prop_id},
    )

    # Rebuild simple transitions for optimization
    conn.execute("DELETE FROM pipeline_transitions WHERE from_stage_id = :sid", {"sid": prop_id})
    cr_row = _get_stage(conn, tid, "conceptual_review")
    if cr_row:
        conn.execute(
            "DELETE FROM pipeline_transitions WHERE from_stage_id = :cr_id AND to_stage_id = :opt_id",
            {"cr_id": cr_row["id"], "opt_id": prop_id},
        )
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
            {"tid": tid, "from_id": cr_row["id"], "to_id": prop_id},
        )

    refl_row = _get_stage(conn, tid, "reflection")
    security_row = _get_stage(conn, tid, "security")
    indev_row = _get_stage(conn, tid, "indev")
    pass_target = refl_row["id"] if refl_row else (security_row["id"] if security_row else None)
    if pass_target:
        conn.execute(
            "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:tid, :from_id, :to_id, 'pass', 0)",
            {"tid": tid, "from_id": prop_id, "to_id": pass_target},
        )
    if indev_row:
        for cond in ("fail", "reject"):
            conn.execute(
                "INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority) "
                "VALUES (:tid, :from_id, :to_id, :cond, 0)",
                {"tid": tid, "from_id": prop_id, "to_id": indev_row["id"], "cond": cond},
            )

    print("[0120 down] Reverted optimization_propose → optimization (optimization_agent).")
