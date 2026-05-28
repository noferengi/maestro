"""
agent_registry — metadata catalogue of built-in Maestro agent types.

AGENT_REGISTRY maps agent_type keys (as stored in pipeline_stages.agent_type)
to AgentSpec descriptors.  The registry is documentation + dispatch metadata;
actual handler functions are registered separately in pipeline_router via
register_handler() to avoid circular imports.

Phase 2: cls fields are None — dispatch uses the registered handler functions.
Phase 5+: cls is populated for direct-instantiation agents (CustomLLMAgent).
          Custom agent definitions loaded from the DB at startup also register
          here via load_custom_agents_into_registry().
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class AgentSpec:
    cls: type | None          # Agent class (None = handled via registered fn)
    display_name: str
    description: str
    default_tools: list[str] = field(default_factory=list)
    gate_type: str = "llm_judge"  # llm_judge | single_pass | test_suite | human | voting | none
    executor_type: str = "infrastructure"  # infrastructure | custom_python | user_defined


AGENT_REGISTRY: dict[str, AgentSpec] = {
    # -------------------------------------------------------------------------
    # Intake sub-stages (custom_python — executors in stage_executors.py)
    # -------------------------------------------------------------------------
    "intake_scope": AgentSpec(
        cls=None,
        display_name="Intake: Scope",
        description="Intake stage 1 — LLM scope analysis (size, complexity, decomposition).",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "intake_static": AgentSpec(
        cls=None,
        display_name="Intake: Static",
        description="Intake stage 2 — deterministic tree-sitter code structure analysis.",
        gate_type="none",
        executor_type="custom_python",
    ),
    "intake_conflict": AgentSpec(
        cls=None,
        display_name="Intake: Conflict",
        description="Intake stage 3 — LLM conflict detection against existing project tasks.",
        gate_type="voting",
        executor_type="custom_python",
    ),
    "intake_feasibility": AgentSpec(
        cls=None,
        display_name="Intake: Feasibility",
        description="Intake stage 4 — LLM feasibility analysis informed by static output.",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "intake_gate": AgentSpec(
        cls=None,
        display_name="Intake: Gate",
        description="Intake stage 5 — tally all votes; passes, rejects, or triggers subdivide/research.",
        gate_type="voting",
        executor_type="custom_python",
    ),
    # -------------------------------------------------------------------------
    "planning_survey_node": AgentSpec(
        cls=None,
        display_name="Planning: Survey",
        description="Agentic codebase survey + task classification (is_proof, is_simple, best_of_n). Wraps planning_utils.run_planning_survey.",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "pitfall_node": AgentSpec(
        cls=None,
        display_name="Planning: Pitfalls",
        description="Deterministic dependency/cycle checks + LLM edge-case detection. Wraps planning_utils.run_pitfall_detection.",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "consolidation_node": AgentSpec(
        cls=None,
        display_name="Planning: Consolidate",
        description="Merges winning design + pitfalls into final PlanningResult. Wraps planning_utils.run_consolidation_and_store.",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "planning_gate_node": AgentSpec(
        cls=None,
        display_name="Planning: Gate",
        description="10-check gate: namespace conflicts, interface completeness, cycles, feasibility recheck, context budget. Wraps planning_gate.run_planning_gate.",
        gate_type="llm_judge",
        executor_type="custom_python",
    ),
    "planning_correction_stage": AgentSpec(
        cls=None,
        display_name="Planning: Correction",
        description="Surgical JSON repair of a failing plan. Wraps planning_correction.PlanningCorrectionAgent.",
        gate_type="llm_judge",
        executor_type="custom_python",
    ),
    # -------------------------------------------------------------------------
    # Legacy SW Dev stage agents (custom_python — bespoke per-stage Python)
    # -------------------------------------------------------------------------
    "implementation_agent": AgentSpec(
        cls=None,
        display_name="Dev Orchestrator",
        description="Parallel implementation orchestrator with test-suite gate",
        gate_type="test_suite",
        executor_type="custom_python",
    ),
    "review_agent": AgentSpec(
        cls=None,
        display_name="Conceptual Review",
        description="Multi-agent code quality review",
        gate_type="voting",
        executor_type="custom_python",
    ),
    "optimization_agent": AgentSpec(
        cls=None,
        display_name="Optimization",
        description="Performance and code quality optimization pipeline",
        gate_type="single_pass",
        executor_type="custom_python",
    ),
    "security_agent": AgentSpec(
        cls=None,
        display_name="Security Review",
        description="Security vulnerability and compliance pipeline",
        gate_type="voting",
        executor_type="custom_python",
    ),
    "final_review_agent": AgentSpec(
        cls=None,
        display_name="Final Review",
        description="Multi-stage final quality gate with virtual merge check",
        gate_type="voting",
        executor_type="custom_python",
    ),
    # -------------------------------------------------------------------------
    # Infrastructure nodes (executor_type="infrastructure" — default)
    # -------------------------------------------------------------------------
    "human_gate": AgentSpec(
        cls=None,
        display_name="Human Review",
        description="Manual human approval — no auto-dispatch",
        gate_type="human",
    ),
    "terminal": AgentSpec(
        cls=None,
        display_name="Completed",
        description="Terminal state — no dispatch",
        gate_type="none",
    ),
    "arch_agent": AgentSpec(
        cls=None,
        display_name="Architecture",
        description="Architecture card generation (dispatched via arch-gen job queue)",
        gate_type="single_pass",
    ),
    "custom_llm_agent": AgentSpec(
        cls=None,  # populated lazily on first use (avoids circular import at module init)
        display_name="Custom LLM Agent",
        description="User-defined prompt agent driven by a custom_agent_definitions row",
        gate_type="llm_judge",
    ),
    "factory_node": AgentSpec(
        cls=None,
        display_name="Card Factory",
        description="Sub-card subdivision factory",
        gate_type="single_pass",
    ),
    "generic_stage": AgentSpec(
        cls=None,
        display_name="Generic LLM Stage",
        description="Universal LLM agent driven entirely by stage.config (system_prompt, tools, gate_type)",
        gate_type="llm_judge",
    ),
    "reflection_agent": AgentSpec(
        cls=None,  # dispatched via registered executor in scheduler.py
        display_name="Reflection",
        description=(
            "Skeptical post-stage review — produces a structured JSON confidence report "
            "(confidence, issues, uncertain_about) consumed by Maestro to decide next action."
        ),
        default_tools=["get_task_history_recent", "submit_work"],
        gate_type="single_pass",
    ),
    "circuit_breaker": AgentSpec(
        cls=None,
        display_name="Circuit Breaker",
        description="Counts attempts via TransitionResult rows or task.content counters; parks or fails when max is reached",
        gate_type="none",
    ),
    "static_analysis_widget": AgentSpec(
        cls=None,
        display_name="Static Analysis",
        description="Runs tree-sitter static analysis on the project and injects structured JSON into task.content. No LLM call.",
        gate_type="none",
    ),
    "json_schema_gate": AgentSpec(
        cls=None,
        display_name="JSON Schema Gate",
        description="Validates task.content or planning_result fields against a configurable schema; routes to correction on failure.",
        gate_type="none",
    ),
    "dangerous_edit_llm_agent": AgentSpec(
        cls=None,
        display_name="Dangerous Edit Agent",
        description=(
            "Wraps MaestroLoop with worktree-isolated writes and per-stage config overrides "
            "(system_prompt, agent_tools, max_turns, required_input_keys). "
            "⚠ This node has write access to the project working tree."
        ),
        gate_type="llm_judge",
    ),
    "parallel_agents": AgentSpec(
        cls=None,
        display_name="Parallel Agents",
        description=(
            "Spawns N independent child tasks in parallel. An aggregator merges outputs "
            "and advances the parent once all children complete."
        ),
        gate_type="none",
    ),
    "parallel_subagent": AgentSpec(
        cls=None,
        display_name="Parallel Subagent",
        description="Internal virtual sub-task created by parallel_agents. Config from task.content.",
        gate_type="none",
    ),
    "parallel_subagent_aggregator": AgentSpec(
        cls=None,
        display_name="Parallel Subagent Aggregator",
        description="Internal virtual task that merges parallel subagent outputs and advances the parent stage.",
        gate_type="none",
    ),
    "parallel_subagent_dangerous": AgentSpec(
        cls=None,
        display_name="Parallel Subagent (Write)",
        description=(
            "Internal virtual sub-task created by parallel_agents when subagent_type='dangerous_edit'. "
            "Runs a scoped MaestroLoop with worktree isolation and write access. "
            "Does not advance stage — the aggregator drives the parent forward."
        ),
        gate_type="none",
    ),
    "multiplier_node": AgentSpec(
        cls=None,
        display_name="Multiplier (Fan-Out)",
        description=(
            "Spawns N independent child tasks (voters or proposers), then creates a collapser task "
            "that aggregates their outputs via vote tally or LLM judge. Crash-survivable — each "
            "child is a real DB-backed scheduled task."
        ),
        gate_type="voting",
    ),
    "_fan_out_child": AgentSpec(
        cls=None,
        display_name="Fan-Out Child",
        description="Internal virtual task created by multiplier_node. Runs one agent and writes submission to task.content.",
        gate_type="none",
    ),
    "_fan_out_collapser": AgentSpec(
        cls=None,
        display_name="Fan-Out Collapser",
        description="Internal virtual task created by multiplier_node. Aggregates child submissions and advances the parent stage.",
        gate_type="none",
    ),
}
