description = "Convert SW Dev optimization_propose from optimization_node to multiplier_node (judge_select)"

import json as _json

_PROPOSER_TOOLS = [
    "read_file", "read_file_metadata", "find_in_files", "find_files",
    "find_symbol", "find_callers", "list_directory", "submit_work",
]

_PERSONAS = [
    {
        "name": "algorithmic",
        "system_prompt": (
            "You are an algorithmic optimization expert. Analyze data structure choices "
            "and complexity. Read the code, identify the highest-complexity operations, "
            "then propose a concrete algorithmic improvement. "
            "Call submit_work with your proposal JSON."
        ),
        "tools": _PROPOSER_TOOLS,
        "max_turns": 20,
    },
    {
        "name": "dependency",
        "system_prompt": (
            "You are a dependency optimization expert. Analyze import graph and coupling. "
            "Identify slow or redundant dependencies and propose removal or lazy-loading. "
            "Call submit_work with your proposal JSON."
        ),
        "tools": _PROPOSER_TOOLS,
        "max_turns": 20,
    },
    {
        "name": "memory",
        "system_prompt": (
            "You are a memory optimization expert. Analyze memory allocation patterns. "
            "Identify object churn, large retained structures, or missed opportunities "
            "for streaming. Call submit_work with your proposal JSON."
        ),
        "tools": _PROPOSER_TOOLS,
        "max_turns": 20,
    },
    {
        "name": "distribution",
        "system_prompt": (
            "You are a parallelism optimization expert. Analyze parallelism opportunities. "
            "Identify CPU-bound loops that could use asyncio, threading, or multiprocessing. "
            "Call submit_work with your proposal JSON."
        ),
        "tools": _PROPOSER_TOOLS,
        "max_turns": 20,
    },
    {
        "name": "bit_level",
        "system_prompt": (
            "You are a micro-optimization expert. Analyze low-level efficiency: "
            "string formatting, repeated attribute lookups, unnecessary copies. "
            "Propose micro-optimizations. Call submit_work with your proposal JSON."
        ),
        "tools": _PROPOSER_TOOLS,
        "max_turns": 20,
    },
]

_NEW_CONFIG = {
    "agents": _PERSONAS,
    "collapser_mode": "judge_select",
    "judge_system_prompt": (
        "You are a performance expert judging optimization proposals. "
        "Score each proposal on feasibility, estimated impact, and implementation risk. "
        "Pick the best one. Output JSON: {\"selected_index\": N, \"rationale\": \"...\"}"
    ),
    "judge_max_turns": 10,
    "min_improvement_pct": 10.0,
    "output_key": "winning_optimization_proposal",
}

_OLD_CONFIG = {
    "proposal_personas": [
        {"name": p["name"], "system_prompt": p["system_prompt"]}
        for p in _PERSONAS
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


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0129] WARNING: 'Software Development' template not found — skipping.")
        return

    stage = _get_stage(conn, tid, "optimization_propose")
    if not stage:
        print("[0129] WARNING: 'optimization_propose' stage not found — skipping.")
        return

    conn.execute(
        "UPDATE pipeline_stages "
        "SET agent_type = 'multiplier_node', config = CAST(:cfg AS jsonb) "
        "WHERE id = :sid",
        {"cfg": _json.dumps(_NEW_CONFIG), "sid": stage["id"]},
    )
    print("[0129] optimization_propose: optimization_node -> multiplier_node (judge_select, 5 agents).")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    stage = _get_stage(conn, tid, "optimization_propose")
    if not stage:
        return

    conn.execute(
        "UPDATE pipeline_stages "
        "SET agent_type = 'optimization_node', config = CAST(:cfg AS jsonb) "
        "WHERE id = :sid",
        {"cfg": _json.dumps(_OLD_CONFIG), "sid": stage["id"]},
    )
    print("[0129 down] optimization_propose: multiplier_node -> optimization_node.")
