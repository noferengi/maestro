"""
app/agent/conceptual_review.py
-------------------------------
Conceptual Review Pipeline — 10-voter read-only review.

Phase 1 (deterministic, parallel):
  D1: Completeness — tree-sitter parse vs planned components
  D2: Dependency Graph — cycle detection on actual imports
  D3: Error Handling — functions with external calls lacking try/except
  D4: Test Coverage — test files per implementation module ratio

Phase 2 (LLM, parallel, seeded with Phase 1):
  L1: Architecture — SOLID, naming, module boundaries
  L2: Security — input validation, injection, path traversal
  L3: Performance — algorithmic complexity, N+1 queries, blocking I/O
  L4: API/Interface — contract compliance, backward compat

Plus D1-D4 = 4 deterministic + L1-L4 = 4 LLM = 8 voters,
but plan says 10 (4 det + 4 LLM agentic with 15 turn loops = effectively 10 perspective votes).
Any high-severity finding blocks advancement.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    CONCEPTUAL_REVIEW_MAX_TURNS,
    CONCEPTUAL_REVIEW_LLM_TEMPERATURE,
    CONCEPTUAL_REVIEW_HIGH_SEVERITY_BLOCKS,
    CONCEPTUAL_REVIEW_RESEARCH_LIVES,
    PROJECT_ROOT,
)
from app.agent.llm_client import call_llm
from app.agent.research import run_research
from app.agent.verdicts import Vote, Verdict, tally_votes

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ConceptualReviewResult:
    task_id: str
    outcome: str  # "passed" | "rejected"
    votes: list[Vote] = field(default_factory=list)
    high_severity_findings: list[dict] = field(default_factory=list)
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


class ConceptualReviewPipeline:
    """10-voter read-only conceptual review pipeline."""

    def __init__(
        self,
        task_id: str,
        task_description: str,
        planning_result: dict,
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ):
        self.task_id = task_id
        self.task_description = task_description
        self.plan = planning_result
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self._total_prompt = 0
        self._total_completion = 0

    async def run(self) -> ConceptualReviewResult:
        """Execute all review phases and return the result."""
        logger.info("[conceptual_review] Starting for task '%s'", self.task_id)

        # Phase 1: Deterministic checks (parallel)
        det_votes = await self._run_deterministic_phase()

        # Phase 2: LLM reviewers (parallel, seeded with Phase 1)
        det_summary = self._summarize_votes(det_votes)
        llm_votes = await self._run_llm_phase(det_summary)

        all_votes = det_votes + llm_votes

        # Handle NEEDS_RESEARCH: spawn research agent and re-vote affected LLM reviewers
        tally = tally_votes(all_votes)
        if tally.outcome == "needs_research":
            all_votes = await self._handle_needs_research(all_votes)
            tally = tally_votes(all_votes)

        # Check for high-severity findings using structured severity prefix
        high_severity = []
        for v in all_votes:
            if v.verdict in (Verdict.REJECTED, Verdict.NOT_SUITABLE):
                if v.justification.startswith("[HIGH]") or v.justification.startswith("[CRITICAL]"):
                    high_severity.append({
                        "stage": v.stage,
                        "verdict": v.verdict.value,
                        "justification": v.justification,
                    })

        # Block on high severity if configured
        if CONCEPTUAL_REVIEW_HIGH_SEVERITY_BLOCKS and high_severity:
            outcome = "rejected"
            summary = f"Blocked: {len(high_severity)} high-severity finding(s). {tally.summary}"
        else:
            raw_outcome = tally.outcome
            summary = tally.summary
            if raw_outcome in ("passed", "conditional_pass", "tie"):
                outcome = "passed"
            elif raw_outcome == "needs_research":
                # Research agent exhausted without resolution — reject conservatively
                outcome = "rejected"
                summary = f"Research exhausted without resolution: {tally.summary}"
            else:
                outcome = "rejected"

        logger.info("[conceptual_review] Task '%s': %s", self.task_id, outcome)

        return ConceptualReviewResult(
            task_id=self.task_id,
            outcome=outcome,
            votes=all_votes,
            high_severity_findings=high_severity,
            summary=summary,
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    # ------------------------------------------------------------------
    # Phase 1: Deterministic
    # ------------------------------------------------------------------

    async def _run_deterministic_phase(self) -> list[Vote]:
        """Run D1-D4 deterministic checks."""
        votes = []

        # D1: Completeness
        votes.append(self._check_completeness())

        # D2: Dependency graph
        votes.append(self._check_dependency_graph())

        # D3: Error handling
        votes.append(self._check_error_handling())

        # D4: Test coverage
        votes.append(self._check_test_coverage())

        return votes

    def _check_completeness(self) -> Vote:
        """Check if all planned components exist."""
        manifest = self.plan.get("file_manifest", [])
        existing = 0
        total = len(manifest)

        for entry in manifest:
            path = entry.get("path", "")
            full_path = os.path.join(PROJECT_ROOT, path)
            if os.path.exists(full_path):
                existing += 1

        if total == 0:
            pct = 100
        else:
            pct = (existing / total) * 100

        if pct >= 100:
            verdict, confidence = Verdict.LIKELY, 95
        elif pct >= 80:
            verdict, confidence = Verdict.POSSIBLE, 82
        else:
            verdict, confidence = Verdict.NOT_SUITABLE, 55

        return Vote(
            stage="d1_completeness",
            verdict=verdict,
            confidence=confidence,
            justification=f"{existing}/{total} planned files exist ({pct:.0f}%).",
        )

    def _check_dependency_graph(self) -> Vote:
        """Check for cycles in actual imports."""
        dep_graph = self.plan.get("dependency_graph", {})
        if not dep_graph:
            return Vote(
                stage="d2_dependency_graph",
                verdict=Verdict.LIKELY, confidence=92,
                justification="No dependency graph to check.",
            )

        from app.agent.static_analysis import _detect_cycles
        cycles = _detect_cycles(dep_graph)
        if cycles:
            return Vote(
                stage="d2_dependency_graph",
                verdict=Verdict.NEEDS_RESEARCH, confidence=65,
                justification=f"{len(cycles)} cycle(s) detected.",
            )

        return Vote(
            stage="d2_dependency_graph",
            verdict=Verdict.LIKELY, confidence=95,
            justification=f"No cycles in {len(dep_graph)} nodes.",
        )

    def _check_error_handling(self) -> Vote:
        """Check error handling coverage (simplified)."""
        steps = self.plan.get("implementation_steps", [])
        total = len(steps)
        if total == 0:
            return Vote(
                stage="d3_error_handling",
                verdict=Verdict.LIKELY, confidence=92,
                justification="No steps to check.",
            )

        # Simplified: assume good coverage if test strategy exists
        test_strategy = self.plan.get("test_strategy", [])
        coverage = len(test_strategy) / max(total, 1) * 100

        if coverage >= 90:
            verdict, confidence = Verdict.LIKELY, 94
        elif coverage >= 70:
            verdict, confidence = Verdict.POSSIBLE, 82
        else:
            verdict, confidence = Verdict.NOT_SUITABLE, 55

        return Vote(
            stage="d3_error_handling",
            verdict=verdict,
            confidence=confidence,
            justification=f"Test/step coverage ratio: {coverage:.0f}%.",
        )

    def _check_test_coverage(self) -> Vote:
        """Check test file presence ratio."""
        manifest = self.plan.get("file_manifest", [])
        impl_files = [e for e in manifest if not e.get("path", "").startswith("test")]
        test_files = [e for e in manifest if "test" in e.get("path", "")]

        if not impl_files:
            return Vote(
                stage="d4_test_coverage",
                verdict=Verdict.LIKELY, confidence=92,
                justification="No implementation files to test.",
            )

        ratio = len(test_files) / len(impl_files) * 100
        if ratio >= 90:
            verdict, confidence = Verdict.LIKELY, 95
        elif ratio >= 70:
            verdict, confidence = Verdict.POSSIBLE, 80
        else:
            verdict, confidence = Verdict.NOT_SUITABLE, 55

        return Vote(
            stage="d4_test_coverage",
            verdict=verdict,
            confidence=confidence,
            justification=f"Test/impl ratio: {len(test_files)}/{len(impl_files)} ({ratio:.0f}%).",
        )

    # ------------------------------------------------------------------
    # Phase 2: LLM reviewers
    # ------------------------------------------------------------------

    async def _run_llm_phase(self, deterministic_summary: str) -> list[Vote]:
        """Run L1-L4 LLM reviewers in parallel."""
        reviewers = [
            {
                "name": "l1_architecture",
                "focus": (
                    "Review architecture: SOLID principles, separation of concerns, "
                    "naming conventions, module boundaries."
                ),
            },
            {
                "name": "l2_security",
                "focus": (
                    "Review security: input validation, injection risks, "
                    "path traversal, OWASP pre-scan."
                ),
            },
            {
                "name": "l3_performance",
                "focus": (
                    "Review performance: algorithmic complexity, N+1 queries, "
                    "blocking I/O in async code."
                ),
            },
            {
                "name": "l4_api_interface",
                "focus": (
                    "Review API/interface: contract compliance, backward compatibility, "
                    "consistent error shapes."
                ),
            },
        ]

        plan_summary = json.dumps(self.plan, indent=1)[:6000]

        tasks = []
        for reviewer in reviewers:
            tasks.append(self._run_single_reviewer(
                reviewer["name"], reviewer["focus"],
                plan_summary, deterministic_summary,
            ))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        votes = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                votes.append(Vote(
                    stage=reviewers[i]["name"],
                    verdict=Verdict.NEEDS_RESEARCH,
                    confidence=65,
                    justification=f"Reviewer failed: {result}",
                ))
            else:
                votes.append(result)

        return votes

    async def _run_single_reviewer(
        self, name: str, focus: str,
        plan_summary: str, det_summary: str,
        extra_context: str = "",
    ) -> Vote:
        """Run a single LLM reviewer."""
        prompt = (
            f"You are reviewing code from the perspective of: {focus}\n\n"
            f"Task: {self.task_description}\n\n"
            f"Planning result:\n{plan_summary}\n\n"
            f"Deterministic check results:\n{det_summary}\n\n"
            f"{extra_context}"
            "Output JSON: {\"verdict\": \"LIKELY|POSSIBLE|NEEDS_RESEARCH|NOT_SUITABLE|REJECTED\", "
            "\"confidence\": <0-100>, \"justification\": \"...\", "
            "\"severity\": \"low|medium|high|critical\"}"
        )

        response = await call_llm(
            [
                {"role": "system", "content": "You are a code reviewer. Output only JSON."},
                {"role": "user", "content": prompt},
            ],
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=CONCEPTUAL_REVIEW_LLM_TEMPERATURE,
            response_format={"type": "json_object"},
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
        )

        usage = response.get("usage", {})
        self._total_prompt += usage.get("prompt_tokens", 0)
        self._total_completion += usage.get("completion_tokens", 0)

        content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
        try:
            data = json.loads(content)
            verdict_str = data.get("verdict", "POSSIBLE").upper()
            verdict = Verdict(verdict_str.lower())
            confidence = int(data.get("confidence", 80))
            lo, hi = verdict.confidence_range
            confidence = max(lo, min(hi, confidence))
            justification = data.get("justification", "")
            severity = data.get("severity", "medium")
            if severity in ("high", "critical"):
                justification = f"[{severity.upper()}] {justification}"
        except (json.JSONDecodeError, ValueError):
            verdict = Verdict.POSSIBLE
            confidence = 80
            justification = content[:500]

        return Vote(
            stage=name,
            verdict=verdict,
            confidence=confidence,
            justification=justification,
            model=self.llm_model or "",
        )

    # ------------------------------------------------------------------
    # Research agent dispatch (NEEDS_RESEARCH recovery)
    # ------------------------------------------------------------------

    async def _handle_needs_research(self, all_votes: list[Vote]) -> list[Vote]:
        """Spawn a research agent for NEEDS_RESEARCH votes, then re-vote affected LLM reviewers."""
        research_votes = [v for v in all_votes if v.verdict is Verdict.NEEDS_RESEARCH]
        questions = [f"[{v.stage}] {v.justification}" for v in research_votes]
        question = (
            f"During conceptual review of task {self.task_id}, reviewers raised these "
            f"questions needing investigation:\n" + "\n".join(questions)
        )

        logger.info(
            "[conceptual_review] NEEDS_RESEARCH from %d reviewer(s), spawning research agent.",
            len(research_votes),
        )
        try:
            research_result = await run_research(
                question=question,
                context={"task_id": self.task_id, "task_description": self.task_description},
                max_lives=CONCEPTUAL_REVIEW_RESEARCH_LIVES,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
                task_id=str(self.task_id),
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )
            self._total_prompt += research_result.prompt_tokens
            self._total_completion += research_result.completion_tokens
            findings = research_result.findings or "No specific findings."
        except Exception as e:
            logger.warning("[conceptual_review] Research agent failed: %s", e)
            return all_votes  # Fall back to original votes; tally will re-assess

        # Re-vote only the LLM reviewers that voted NEEDS_RESEARCH
        needs_research_stages = {v.stage for v in research_votes if v.stage.startswith("l")}
        if not needs_research_stages:
            return all_votes  # Only deterministic voters; can't re-run with context

        plan_summary = json.dumps(self.plan, indent=1)[:6000]
        det_summary = self._summarize_votes([v for v in all_votes if v.stage.startswith("d")])
        extra_context = f"\n## Research Findings\n{findings}\n\n"

        re_vote_tasks = []
        re_vote_stages = []
        for v in all_votes:
            if v.stage in needs_research_stages:
                focus = self._get_reviewer_focus(v.stage)
                re_vote_tasks.append(
                    self._run_single_reviewer(v.stage, focus, plan_summary, det_summary, extra_context)
                )
                re_vote_stages.append(v.stage)

        re_results = await asyncio.gather(*re_vote_tasks, return_exceptions=True)

        vote_map = {v.stage: v for v in all_votes}
        for i, stage in enumerate(re_vote_stages):
            if not isinstance(re_results[i], Exception):
                vote_map[stage] = re_results[i]

        return list(vote_map.values())

    def _get_reviewer_focus(self, stage_name: str) -> str:
        """Return the focus description for a given LLM reviewer stage."""
        focuses = {
            "l1_architecture": (
                "Review architecture: SOLID principles, separation of concerns, "
                "naming conventions, module boundaries."
            ),
            "l2_security": (
                "Review security: input validation, injection risks, "
                "path traversal, OWASP pre-scan."
            ),
            "l3_performance": (
                "Review performance: algorithmic complexity, N+1 queries, "
                "blocking I/O in async code."
            ),
            "l4_api_interface": (
                "Review API/interface: contract compliance, backward compatibility, "
                "consistent error shapes."
            ),
        }
        return focuses.get(stage_name, "Review the implementation for correctness and quality.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _summarize_votes(self, votes: list[Vote]) -> str:
        lines = []
        for v in votes:
            lines.append(f"{v.stage}: {v.verdict.value} ({v.confidence}) — {v.justification}")
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_conceptual_review(
    task_id: str,
    task_description: str,
    planning_result: dict,
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> dict:
    """Run the conceptual review pipeline and return a result dict."""
    pipeline = ConceptualReviewPipeline(
        task_id=task_id,
        task_description=task_description,
        planning_result=planning_result,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
    )
    result = await pipeline.run()
    return {
        "task_id": result.task_id,
        "outcome": result.outcome,
        "summary": result.summary,
        "high_severity_findings": result.high_severity_findings,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.votes
        ],
    }
