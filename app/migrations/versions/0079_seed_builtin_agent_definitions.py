"""
Seed the 14 built-in agent definitions into custom_agent_definitions.

Each row has is_builtin=TRUE, a behavior_type that maps to a dispatch strategy,
and a behavior_config JSON blob with the configurable parameters that drive
the built-in Python handler.  These definitions are locked (the API guards
PUT/DELETE on is_builtin rows) but can be cloned to produce editable copies.

behavior_type -> dispatch routing in pipeline_router.dispatch_task:
  intake_pipeline    -> _stage_handlers["idea"]
  planning_pipeline  -> _stage_handlers["planning"]
  maestro_loop       -> _stage_handlers["indev"]
  conceptual_review  -> _stage_handlers["conceptual_review"]
  optimization       -> _stage_handlers["optimization"]
  security           -> _stage_handlers["security"]
  final_review       -> _stage_handlers["final_review"]
  factory            -> _stage_handlers["factory_node"]
  voting_panel       -> _agent_type_executors["voting_panel"]
  circuit_breaker    -> _agent_type_executors["circuit_breaker"]
  fan_out_judge      -> _agent_type_executors["fan_out_judge"]
  human_gate         -> no dispatch (waits for human)
  arch_gen           -> no dispatch (arch-gen job queue)
  single_pass_llm    -> GenericStageAgent fallback
"""

import json as _json

_BUILTIN_DEFS = [
    {
        "name": "intake_agent",
        "display_name": "Intake Agent",
        "description": (
            "4-stage voting pipeline: scope analysis, static analysis, feasibility, "
            "and conflict detection. Advances a card from IDEA to the first pipeline stage."
        ),
        "intent": "Validate that a new task is well-scoped, feasible, and non-conflicting before committing pipeline resources.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "voting",
        "verifier": "none",
        "behavior_type": "intake_pipeline",
        "behavior_config": _json.dumps({
            "stages": [
                {"key": "scope",       "name": "Scope Analysis",     "weight": 1.0},
                {"key": "static",      "name": "Static Analysis",    "weight": 1.0},
                {"key": "feasibility", "name": "Feasibility",        "weight": 1.0},
                {"key": "conflict",    "name": "Conflict Detection", "weight": 1.0},
            ],
            "context_budget_ratio": 0.60,
            "research_tools": ["web_search", "web_fetch"],
            "tiebreaker": "pass",
        }),
    },
    {
        "name": "planning_agent",
        "display_name": "Planning Agent",
        "description": (
            "Design and planning pipeline with LLM gate and automated correction agent. "
            "Generates interface contracts, file manifest, and implementation plan."
        ),
        "intent": "Produce a complete, LLM-reviewed implementation plan before any code is written.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "llm_judge",
        "verifier": "none",
        "behavior_type": "planning_pipeline",
        "behavior_config": _json.dumps({
            "correction_enabled": True,
            "max_correction_attempts": 3,
            "context_budget_ratio": 0.60,
        }),
    },
    {
        "name": "implementation_agent",
        "display_name": "Implementation Agent",
        "description": (
            "Parallel implementation orchestrator with test-suite gate. "
            "Runs Design->Implement->Test->Verify cycles via MaestroLoop in an isolated git worktree."
        ),
        "intent": "Write, test, and commit code that satisfies the planning specification.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([
            "read_file", "write_file", "list_directory", "search_files",
            "run_pytest", "run_mypy", "run_ruff", "run_black_check",
            "git_add", "git_restore", "git_unstage",
            "submit_work", "report_tool_bug",
        ]),
        "gate_type": "test_suite",
        "verifier": "none",
        "behavior_type": "maestro_loop",
        "behavior_config": _json.dumps({
            "max_turns": 40,
            "test_command": "run_pytest",
            "enable_worktree": True,
        }),
    },
    {
        "name": "review_agent",
        "display_name": "Conceptual Review Agent",
        "description": (
            "Multi-agent code quality review with voting gate. "
            "Evaluates correctness, design coherence, and test coverage."
        ),
        "intent": "Catch logical errors, design smells, and test gaps before the optimization pass.",
        "system_prompt": "",
        "allowed_tools": _json.dumps(["read_file", "search_files", "list_directory"]),
        "gate_type": "voting",
        "verifier": "none",
        "behavior_type": "conceptual_review",
        "behavior_config": _json.dumps({
            "num_voters": 3,
            "threshold": 0.6,
            "topic": "code_quality",
        }),
    },
    {
        "name": "optimization_agent",
        "display_name": "Optimization Agent",
        "description": (
            "Performance and code quality optimization pipeline. "
            "Single-pass analysis and targeted improvements to hot paths and bottlenecks."
        ),
        "intent": "Improve runtime performance and reduce code complexity without changing observable behavior.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([
            "read_file", "write_file", "list_directory", "search_files", "run_pytest",
        ]),
        "gate_type": "single_pass",
        "verifier": "none",
        "behavior_type": "optimization",
        "behavior_config": _json.dumps({"max_turns": 20}),
    },
    {
        "name": "security_agent",
        "display_name": "Security Review Agent",
        "description": (
            "Security vulnerability and compliance pipeline with voting gate. "
            "Scans for OWASP Top 10, dependency CVEs, and authentication issues."
        ),
        "intent": "Identify and remediate security vulnerabilities before the final quality gate.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([
            "read_file", "search_files", "list_directory", "run_bandit", "run_pip_audit",
        ]),
        "gate_type": "voting",
        "verifier": "none",
        "behavior_type": "security",
        "behavior_config": _json.dumps({
            "num_voters": 3,
            "threshold": 0.6,
            "topic": "security",
        }),
    },
    {
        "name": "final_review_agent",
        "display_name": "Final Review Agent",
        "description": (
            "Multi-stage final quality gate with virtual merge check and voting panel. "
            "Last automated gate before human review."
        ),
        "intent": "Confirm the implementation is complete, correct, and safe to hand off for human sign-off.",
        "system_prompt": "",
        "allowed_tools": _json.dumps(["read_file", "search_files", "list_directory"]),
        "gate_type": "voting",
        "verifier": "none",
        "behavior_type": "final_review",
        "behavior_config": _json.dumps({
            "num_voters": 3,
            "threshold": 0.6,
            "topic": "final_quality",
            "virtual_merge_check": True,
        }),
    },
    {
        "name": "human_gate",
        "display_name": "Human Review Gate",
        "description": (
            "Manual human approval gate. No auto-dispatch -- "
            "waits for a human to approve or reject via the UI."
        ),
        "intent": "Pause the pipeline at a checkpoint that requires explicit human judgment.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "human",
        "verifier": "none",
        "behavior_type": "human_gate",
        "behavior_config": _json.dumps({"requires_comment": False}),
    },
    {
        "name": "arch_agent",
        "display_name": "Architecture Agent",
        "description": (
            "Architecture card generation via LLM. "
            "Dispatched through the arch-gen job queue, not the main task scheduler."
        ),
        "intent": "Generate and maintain architecture constraint cards for a project.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "single_pass",
        "verifier": "none",
        "behavior_type": "arch_gen",
        "behavior_config": _json.dumps({}),
    },
    {
        "name": "factory_node",
        "display_name": "Card Factory",
        "description": (
            "Sub-card subdivision factory. Creates batches of child cards from templates "
            "or LLM segmentation of a source dataset."
        ),
        "intent": "Decompose a parent card into a structured set of child work items.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "single_pass",
        "verifier": "none",
        "behavior_type": "factory",
        "behavior_config": _json.dumps({"mode": "llm_segmented", "max_cards": 20}),
    },
    {
        "name": "generic_stage",
        "display_name": "Generic LLM Stage",
        "description": (
            "Universal LLM agent driven entirely by stage.config "
            "(system_prompt, tools, gate_type). No hardcoded logic."
        ),
        "intent": "A flexible single-agent stage for any task not covered by a specialized agent.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "llm_judge",
        "verifier": "none",
        "behavior_type": "single_pass_llm",
        "behavior_config": _json.dumps({"max_turns": 20}),
    },
    {
        "name": "circuit_breaker",
        "display_name": "Circuit Breaker",
        "description": (
            "Counts demotion attempts via TransitionResult rows; "
            "parks or fails the card when the maximum attempt count is reached."
        ),
        "intent": "Prevent infinite retry loops by limiting how many times a card can revisit a stage.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "none",
        "verifier": "none",
        "behavior_type": "circuit_breaker",
        "behavior_config": _json.dumps({"max_attempts": 3, "action_on_exceed": "park"}),
    },
    {
        "name": "voting_panel",
        "display_name": "Voting Panel",
        "description": (
            "Spawns N concurrent LLM voters; tallies results; advances on majority. "
            "Configurable voter count and pass threshold."
        ),
        "intent": "Reach a consensus decision among multiple independent LLM judges.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "voting",
        "verifier": "none",
        "behavior_type": "voting_panel",
        "behavior_config": _json.dumps({"num_voters": 3, "threshold": 0.6}),
    },
    {
        "name": "fan_out_judge",
        "display_name": "Fan-Out + Judge",
        "description": (
            "Runs N parallel proposal agents then an LLM judge selects the best one. "
            "Useful for creative or exploratory stages."
        ),
        "intent": "Generate multiple candidate solutions and select the strongest one via LLM adjudication.",
        "system_prompt": "",
        "allowed_tools": _json.dumps([]),
        "gate_type": "llm_judge",
        "verifier": "none",
        "behavior_type": "fan_out_judge",
        "behavior_config": _json.dumps({"num_proposals": 3}),
    },
]


def up(conn):
    for row in _BUILTIN_DEFS:
        if conn.is_postgres:
            conn.execute(
                """
                INSERT INTO custom_agent_definitions
                    (name, display_name, description, intent, system_prompt,
                     allowed_tools, gate_type, verifier,
                     behavior_type, behavior_config, is_builtin, created_at)
                VALUES
                    (:name, :display_name, :description, :intent, :system_prompt,
                     CAST(:allowed_tools AS jsonb), :gate_type, :verifier,
                     :behavior_type, CAST(:behavior_config AS jsonb), TRUE, NOW())
                ON CONFLICT (name) DO UPDATE SET
                    display_name    = EXCLUDED.display_name,
                    description     = EXCLUDED.description,
                    intent          = EXCLUDED.intent,
                    behavior_type   = EXCLUDED.behavior_type,
                    behavior_config = EXCLUDED.behavior_config,
                    is_builtin      = TRUE
                """,
                row,
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO custom_agent_definitions
                    (name, display_name, description, intent, system_prompt,
                     allowed_tools, gate_type, verifier,
                     behavior_type, behavior_config, is_builtin, created_at)
                VALUES
                    (:name, :display_name, :description, :intent, :system_prompt,
                     :allowed_tools, :gate_type, :verifier,
                     :behavior_type, :behavior_config, 1, datetime('now'))
                """,
                row,
            )


def down(conn):
    names = [r["name"] for r in _BUILTIN_DEFS]
    for name in names:
        if conn.is_postgres:
            conn.execute(
                "DELETE FROM custom_agent_definitions WHERE name = :name AND is_builtin = TRUE",
                {"name": name},
            )
        else:
            conn.execute(
                "DELETE FROM custom_agent_definitions WHERE name = :name AND is_builtin = 1",
                {"name": name},
            )
