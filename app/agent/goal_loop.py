"""
app/agent/goal_loop.py
-----------------------
run_goal_iteration() — one full iteration of the goal evaluation loop.

Flow: expert panel evaluation → if achieved: mark done.
      If not achieved and iterations remain: run GlobalMaestroAgent to create
      new tasks addressing the panel critiques.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime

from app.database import get_goal, update_goal, append_goal_evidence
from app.agent.goal_evaluator import GoalEvaluatorAgent
from app.agent.maestro_orchestrator import GlobalMaestroAgent

logger = logging.getLogger(__name__)


async def run_goal_iteration(goal_id: int, llm_id: int, budget_id: int) -> str:
    """Run one full goal iteration.

    Returns one of: 'achieved' | 'planning' | 'exhausted' | 'error'
    """
    goal = get_goal(goal_id)
    if not goal:
        logger.error("run_goal_iteration: goal %d not found", goal_id)
        return "error"

    max_iter = getattr(goal, "max_iterations", 10)
    iter_count = getattr(goal, "iteration_count", 0)

    if max_iter != -1 and iter_count >= max_iter:
        update_goal(goal_id, status="escalated")
        append_goal_evidence(
            goal_id,
            f"Goal escalated after reaching max_iterations={max_iter}. "
            "Human review required.",
        )
        logger.info("Goal %d escalated (max_iterations=%d)", goal_id, max_iter)
        return "exhausted"

    # Run expert panel
    try:
        evaluator = GoalEvaluatorAgent(goal_id, llm_id, budget_id)
        result = await evaluator.evaluate()
    except Exception as exc:
        logger.error("GoalEvaluatorAgent failed for goal %d: %s", goal_id, exc)
        return "error"

    if result["achieved"]:
        update_goal(
            goal_id,
            status="achieved",
            achieved_at=datetime.utcnow(),
            progress=1.0,
            last_verdict={"achieved": True, "votes": result["votes"]},
        )
        append_goal_evidence(goal_id, "Expert panel unanimously agreed: GOAL ACHIEVED.")
        logger.info("Goal %d achieved at iteration %d", goal_id, iter_count)
        return "achieved"

    # Build critique context
    critiques = result.get("dissenting_critiques") or []
    critique_ctx = "\n".join(f"- {c}" for c in critiques) or "(no specific critiques)"

    update_goal(
        goal_id,
        iteration_count=iter_count + 1,
        last_verdict={"achieved": False, "votes": result["votes"]},
    )
    append_goal_evidence(
        goal_id,
        f"## Iteration {iter_count + 1} — Expert Panel Critiques\n{critique_ctx}",
    )

    # Re-fetch project name for orchestrator
    goal = get_goal(goal_id)
    if not goal:
        return "error"

    from app.database import get_project_by_id
    project = get_project_by_id(goal.project_id)
    if not project:
        logger.error("Goal %d: project %d not found", goal_id, goal.project_id)
        return "error"

    directive = (
        f"The expert panel evaluated goal #{goal_id} and found it NOT YET ACHIEVED.\n\n"
        f"Critiques from this round:\n{critique_ctx}\n\n"
        f"Create the minimum set of new tasks needed to address these critiques.\n"
        f"Link each task to goal_id={goal_id}. Set appropriate prerequisites.\n"
        f"Do not duplicate tasks that are already in-progress."
    )

    orchestrator = GlobalMaestroAgent(
        project_name=project.name,
        llm_id=llm_id,
        budget_id=budget_id,
        goal_id=goal_id,
    )
    try:
        orch_result = await orchestrator.run(directive)
        logger.info("Orchestrator result for goal %d: %s", goal_id, orch_result.get("signal"))
    except Exception as exc:
        logger.error("Orchestrator failed for goal %d: %s", goal_id, exc)

    return "planning"
