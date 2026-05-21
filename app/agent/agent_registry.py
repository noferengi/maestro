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


AGENT_REGISTRY: dict[str, AgentSpec] = {
    "intake_agent": AgentSpec(
        cls=None,
        display_name="Intake",
        description="4-stage intake voting pipeline (scope, static, feasibility, conflict)",
        gate_type="voting",
    ),
    "planning_agent": AgentSpec(
        cls=None,
        display_name="Planning",
        description="Design + planning pipeline with LLM gate and correction agent",
        gate_type="llm_judge",
    ),
    "implementation_agent": AgentSpec(
        cls=None,
        display_name="Dev Orchestrator",
        description="Parallel implementation orchestrator with test-suite gate",
        gate_type="test_suite",
    ),
    "review_agent": AgentSpec(
        cls=None,
        display_name="Conceptual Review",
        description="Multi-agent code quality review",
        gate_type="voting",
    ),
    "optimization_agent": AgentSpec(
        cls=None,
        display_name="Optimization",
        description="Performance and code quality optimization pipeline",
        gate_type="single_pass",
    ),
    "security_agent": AgentSpec(
        cls=None,
        display_name="Security Review",
        description="Security vulnerability and compliance pipeline",
        gate_type="voting",
    ),
    "final_review_agent": AgentSpec(
        cls=None,
        display_name="Final Review",
        description="Multi-stage final quality gate with virtual merge check",
        gate_type="voting",
    ),
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
        description="Sub-card subdivision factory (Phase 9)",
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
    "voting_panel": AgentSpec(
        cls=None,
        display_name="Voting Panel",
        description="Spawns N concurrent LLM voters; tallies results; advances on majority",
        gate_type="voting",
    ),
    "fan_out_judge": AgentSpec(
        cls=None,
        display_name="Fan-Out + Judge",
        description="Runs N parallel proposal agents then an LLM judge selects the best one",
        gate_type="llm_judge",
    ),
}
