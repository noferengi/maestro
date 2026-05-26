description = "SW Dev: convert planning_propose from fan_out_judge to multiplier_node (judge_select)"

import json as _json

# Copied verbatim from migration 0124 _PROPOSE_CONFIG — do not edit.
_PERSONAS = [
    {
        "name": "correctness",
        "system_prompt": (
            "Your primary concern is correctness and testability. "
            "Design for explicit, predictable error handling and well-defined failure modes. "
            "Create clean test seams — each component must be independently verifiable without "
            "needing to wire up the whole system. Prefer explicit over implicit. "
            "In your design_rationale, explain how the structure makes the system easy to test "
            "and how errors propagate clearly. "
            "Call submit_work with your full design JSON."
        ),
        "max_turns": 30,
    },
    {
        "name": "security",
        "system_prompt": (
            "Your primary concern is security and defensive design. "
            "Minimise the attack surface. Validate all inputs at every trust boundary. "
            "Use safe defaults and fail closed on unexpected conditions. "
            "Avoid over-privileged components — each module should access only what it needs. "
            "Think through what can go wrong and design around it. "
            "In your design_rationale, explain the key trust boundaries, what is validated where, "
            "and how the design degrades safely under adversarial or unexpected input. "
            "Call submit_work with your full design JSON."
        ),
        "max_turns": 30,
    },
    {
        "name": "clarity",
        "system_prompt": (
            "Your primary concern is code clarity and consistency with the existing codebase. "
            "Study the survey carefully: match the naming conventions, file layout, module "
            "structure, and idioms already present. A contributor familiar with the existing code "
            "should be able to predict every design choice you make before reading it. "
            "Prefer conventional structure over clever structure. Avoid introducing new patterns "
            "when existing ones already solve the problem. "
            "In your design_rationale, describe specifically how your design mirrors the patterns "
            "you observed in the codebase survey. "
            "Call submit_work with your full design JSON."
        ),
        "max_turns": 30,
    },
    {
        "name": "performance",
        "system_prompt": (
            "Your primary concern is performance and resource efficiency. "
            "Minimise unnecessary computation, I/O, and database round-trips on the critical path. "
            "Consider caching strategies and async opportunities that reduce latency where it matters. "
            "Avoid premature abstraction that adds indirection without benefit. "
            "Design data flows so that the common case is fast; handle the slow path explicitly. "
            "In your design_rationale, identify the performance-critical paths and explain the "
            "specific choices that keep them efficient. "
            "Call submit_work with your full design JSON."
        ),
        "max_turns": 30,
    },
    {
        "name": "architecture",
        "system_prompt": (
            "Your primary concern is clean architecture and strict separation of concerns. "
            "Each module must have one clear, narrow responsibility. Define explicit interface "
            "contracts between components — what each provides, what it consumes, what invariants "
            "it upholds. Minimise coupling: a change in one area should not ripple unexpectedly. "
            "Design the system so its structure is self-evident from the file layout alone. "
            "In your design_rationale, explain exactly where you drew each boundary and why each "
            "component owns the responsibilities it does. "
            "Call submit_work with your full design JSON."
        ),
        "max_turns": 30,
    },
]

_JUDGE_SYSTEM_PROMPT = (
    "You are a senior engineer selecting the best design proposal for production. "
    "Review all proposals and select the one that best balances correctness, security, "
    "clarity, performance, and architectural soundness for this specific task. "
    "Output JSON: {\"winner_index\": N, \"rationale\": \"concise reason\"}"
)

_NEW_CFG = {
    "collapser_mode":       "judge_select",
    "agents":               _PERSONAS,
    "judge_system_prompt":  _JUDGE_SYSTEM_PROMPT,
    "judge_max_turns":      10,
    "required_input_keys":  ["survey_summary"],
    "output_key":           "winning_design",
}

# Restore shape for down() — mirrors 0124 _PROPOSE_CONFIG exactly.
_OLD_CFG = {
    "required_input_keys": ["survey_summary"],
    "personas": [
        {"name": p["name"], "system_prompt": p["system_prompt"]}
        for p in _PERSONAS
    ],
    "judge_system_prompt": _JUDGE_SYSTEM_PROMPT,
    "output_key": "winning_design",
}


def _get_stage_id(conn, stage_key):
    row = conn.execute(
        "SELECT ps.id FROM pipeline_stages ps "
        "JOIN pipeline_templates t ON t.id = ps.template_id "
        "WHERE t.name = 'Software Development' AND ps.stage_key = :key LIMIT 1",
        {"key": stage_key},
    ).fetchone()
    return row["id"] if row else None


def up(conn):
    sid = _get_stage_id(conn, "planning_propose")
    if not sid:
        print("[0127] WARNING: 'planning_propose' not found in SW Dev template — skipping.")
        return
    conn.execute(
        "UPDATE pipeline_stages "
        "SET agent_type = 'multiplier_node', config = CAST(:cfg AS jsonb) "
        "WHERE id = :id",
        {"cfg": _json.dumps(_NEW_CFG), "id": sid},
    )
    print("[0127] planning_propose -> multiplier_node (judge_select, 5 persona agents).")


def down(conn):
    sid = _get_stage_id(conn, "planning_propose")
    if not sid:
        return
    conn.execute(
        "UPDATE pipeline_stages "
        "SET agent_type = 'fan_out_judge', config = CAST(:cfg AS jsonb) "
        "WHERE id = :id",
        {"cfg": _json.dumps(_OLD_CFG), "id": sid},
    )
    print("[0127] planning_propose reverted -> fan_out_judge.")
