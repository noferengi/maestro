"""
app/agent/goal_evaluator.py
----------------------------
GoalEvaluatorAgent — runs 5 independent LLM judge calls (expert panel) and
records their YES/NO votes in goal_expert_votes.

All 5 judges must vote YES for the goal to be considered achieved.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from app.database import (
    get_goal,
    record_expert_vote,
    tally_expert_panel,
    goal_to_dict,
    get_llm,
)
from app.agent.llm_client import call_llm

logger = logging.getLogger(__name__)

JUDGE_PERSONAS = [
    (
        "Completeness Auditor",
        "You evaluate whether every stated success criterion is demonstrably present in the evidence.",
    ),
    (
        "Quality Skeptic",
        "You evaluate whether the quality meets professional standards with no rough edges.",
    ),
    (
        "Devil's Advocate",
        "You actively look for any gaps, omissions, or unmet assumptions.",
    ),
    (
        "Criterion Literalist",
        "You evaluate the exact wording of each criterion, refusing to accept partial fulfillment.",
    ),
    (
        "External Perspective",
        "You evaluate as if you had no knowledge of the system's implementation — only the output.",
    ),
]


class GoalEvaluatorAgent:
    """Evaluates a goal by running 5 independent judge calls in parallel."""

    def __init__(self, goal_id: int, llm_id: int, budget_id: int):
        self.goal_id = goal_id
        self.llm_id = llm_id
        self.budget_id = budget_id

    async def evaluate(self) -> dict:
        """Run all 5 judges and return the tally result."""
        goal = get_goal(self.goal_id)
        if not goal:
            return {"achieved": False, "votes": [], "dissenting_critiques": [f"Goal {self.goal_id} not found"]}

        iteration = getattr(goal, "iteration_count", 0)

        judge_tasks = [
            self._call_judge(goal, iteration, i, persona_name, persona_desc)
            for i, (persona_name, persona_desc) in enumerate(JUDGE_PERSONAS)
        ]
        results = await asyncio.gather(*judge_tasks, return_exceptions=True)

        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error("Judge %d failed: %s", i, r)
                record_expert_vote(
                    self.goal_id, iteration, i,
                    JUDGE_PERSONAS[i][0], "NO",
                    f"Judge call failed: {r}",
                )

        return tally_expert_panel(self.goal_id, iteration)

    async def _call_judge(
        self,
        goal: Any,
        iteration: int,
        judge_index: int,
        persona_name: str,
        persona_desc: str,
    ) -> None:
        system_prompt = (
            f"You are {persona_name}. {persona_desc}\n\n"
            "You are evaluating whether a Goal has been fully achieved.\n\n"
            "RULES:\n"
            "- Answer YES or NO only on the first line.\n"
            "- YES means EVERY criterion is fully met with zero caveats, zero hedging,\n"
            "  zero 'could be improved', zero 'partially'. If you have any doubt: NO.\n"
            "- If NO, state the single most critical unmet criterion in one sentence.\n"
            "- Do not reference other judges or prior evaluations."
        )

        d = goal_to_dict(goal)
        criteria_text = "\n".join(
            f"- {c}" if isinstance(c, str) else f"- {c.get('text', str(c))}"
            for c in (d.get("criteria") or [])
        ) or "(no explicit criteria — use your best judgment)"

        evidence = d.get("evidence") or "(no evidence recorded)"
        # Keep evidence to last 4000 chars to fit context
        if len(evidence) > 4000:
            evidence = "...[truncated]\n" + evidence[-4000:]

        user_content = (
            f"## Goal Statement\n{d['statement']}\n\n"
            f"## Success Criteria\n{criteria_text}\n\n"
            f"## Evidence of Work Completed\n{evidence}\n\n"
            f"## Your Verdict\n"
        )

        llm = get_llm(self.llm_id)
        base_url = f"http://{llm.address}:{llm.port}/v1" if llm else None
        try:
            response = await call_llm(
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_content},
                ],
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                base_url=base_url,
                agent_name=f"GoalEvaluator-{judge_index}",
            )
        except Exception as exc:
            raise RuntimeError(f"LLM call failed for judge {judge_index}: {exc}") from exc

        content = (
            response.get("choices", [{}])[0]
            .get("message", {})
            .get("content", "")
            or ""
        ).strip()

        verdict, justification = self._parse_verdict(content)

        usage = response.get("usage", {})
        record_expert_vote(
            goal_id=self.goal_id,
            iteration=iteration,
            judge_index=judge_index,
            judge_persona=persona_name,
            verdict=verdict,
            justification=justification,
            model=None,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    @staticmethod
    def _parse_verdict(content: str) -> tuple[str, str]:
        lines = content.strip().splitlines()
        first = lines[0].strip().upper() if lines else ""
        if first.startswith("YES"):
            verdict = "YES"
        elif first.startswith("NO"):
            verdict = "NO"
        else:
            verdict = "NO"  # default to NO when ambiguous

        justification = " ".join(lines[1:]).strip() if len(lines) > 1 else content
        if not justification:
            justification = "(no justification given)"
        return verdict, justification[:2000]
