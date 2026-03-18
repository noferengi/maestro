"""
app/agent/optimization.py
--------------------------
Optimization Pipeline — profile → propose → vote → implement → verify.

5-phase flow:
  1. Profiling Agent → BaselineReport
  2. 5x Proposers in parallel → Optimization Proposals
  3. 3x Judges in parallel → Ranked proposals
  4. Implementation Agent → Code changes
  5. Profiling Agent again → Compare vs baseline
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    OPTIMIZATION_PROPOSAL_COUNT,
    OPTIMIZATION_JUDGE_COUNT,
    OPTIMIZATION_IMPL_MAX_TURNS,
    OPTIMIZATION_PROPOSER_TEMPERATURE,
    OPTIMIZATION_JUDGE_TEMPERATURE,
    OPTIMIZATION_IMPL_TEMPERATURE,
    OPTIMIZATION_MIN_IMPROVEMENT_PCT,
    OPTIMIZATION_MAX_REGRESSION_PCT,
)
from app.agent.llm_client import call_llm

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class OptimizationPipelineResult:
    task_id: str
    outcome: str  # "optimized" | "skipped" | "rejected"
    baseline_report: dict = field(default_factory=dict)
    proposals: list[dict] = field(default_factory=list)
    judge_scores: list[dict] = field(default_factory=list)
    winning_proposal_index: int = 0
    winning_score: float = 0.0
    post_report: dict = field(default_factory=dict)
    improvement_summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    revote_occurred: bool = False
    round1_vote_tally: dict = field(default_factory=dict)
    round2_vote_tally: dict = field(default_factory=dict)
    demotion_target: str = ""


class OptimizationPipeline:
    """5-phase optimization pipeline."""

    def __init__(
        self,
        task_id: str,
        task_description: str,
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ):
        self.task_id = task_id
        self.task_description = task_description
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self._total_prompt = 0
        self._total_completion = 0

    async def run(self) -> OptimizationPipelineResult:
        """Execute all 5 phases."""
        logger.info("[optimization] Starting for task '%s'", self.task_id)

        # Phase 1: Baseline profiling
        baseline = await self._phase_profiling("baseline")

        # Phase 2: Parallel proposals
        proposals = await self._phase_proposals(baseline)

        # Phase 3: Judge proposals
        scores, winner_idx, winner_score, r1_tally, r2_tally, revote = await self._phase_judging(proposals)

        # No majority after revote — reject and demote to indev
        if winner_idx == -1:
            reason = (
                f"No consensus on optimization approach after revote. "
                f"Round 1: {r1_tally}, Round 2: {r2_tally}."
            )
            logger.warning("[optimization] Rejecting task '%s': %s", self.task_id, reason)
            result = OptimizationPipelineResult(
                task_id=self.task_id,
                outcome="rejected",
                baseline_report=baseline,
                proposals=proposals,
                judge_scores=scores,
                improvement_summary=reason,
                prompt_tokens=self._total_prompt,
                completion_tokens=self._total_completion,
                revote_occurred=revote,
                round1_vote_tally=r1_tally,
                round2_vote_tally=r2_tally,
                demotion_target="indev",
            )
            self._store_result(result)
            return result

        if not proposals or winner_score < 0.3:
            logger.info("[optimization] No viable proposals, skipping optimization.")
            return OptimizationPipelineResult(
                task_id=self.task_id,
                outcome="skipped",
                baseline_report=baseline,
                proposals=proposals,
                judge_scores=scores,
                improvement_summary="No viable optimization proposals.",
                prompt_tokens=self._total_prompt,
                completion_tokens=self._total_completion,
            )

        # Phase 4: Implementation
        winning_proposal = proposals[winner_idx] if winner_idx < len(proposals) else proposals[0]
        impl_success = await self._phase_implementation(winning_proposal)

        if not impl_success:
            return OptimizationPipelineResult(
                task_id=self.task_id,
                outcome="rejected",
                baseline_report=baseline,
                proposals=proposals,
                judge_scores=scores,
                winning_proposal_index=winner_idx,
                winning_score=winner_score,
                improvement_summary="Implementation failed.",
                prompt_tokens=self._total_prompt,
                completion_tokens=self._total_completion,
            )

        # Phase 5: Post-optimization profiling
        post = await self._phase_profiling("post")

        # Compare
        outcome, summary = self._compare_reports(baseline, post)

        result = OptimizationPipelineResult(
            task_id=self.task_id,
            outcome=outcome,
            baseline_report=baseline,
            proposals=proposals,
            judge_scores=scores,
            winning_proposal_index=winner_idx,
            winning_score=winner_score,
            post_report=post,
            improvement_summary=summary,
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

        # Store in DB
        self._store_result(result)

        return result

    # ------------------------------------------------------------------
    # Phase 1 & 5: Profiling
    # ------------------------------------------------------------------

    async def _phase_profiling(self, phase_name: str) -> dict:
        """Run profiling analysis via LLM."""
        prompt = (
            f"You are a performance profiler running {phase_name} analysis.\n"
            f"Task: {self.task_description}\n\n"
            "Analyze and report on:\n"
            "- Test duration estimates\n"
            "- Memory usage patterns\n"
            "- Dependency count\n"
            "- Hotspot identification\n"
            "- Code complexity metrics\n\n"
            "Output JSON: {\"test_duration_ms\": 0, \"memory_peak_mb\": 0, "
            "\"dep_count\": 0, \"hotspots\": [], \"complexity_score\": 0}"
        )

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": "You are a performance profiler. Output only JSON."},
                    {"role": "user", "content": prompt},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=0.1,
                response_format={"type": "json_object"},
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )
            self._track_tokens(response)
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            return json.loads(content)
        except Exception as e:
            logger.warning("[optimization] Profiling (%s) failed: %s", phase_name, e)
            return {"error": str(e)}

    # ------------------------------------------------------------------
    # Phase 2: Proposals
    # ------------------------------------------------------------------

    async def _phase_proposals(self, baseline: dict) -> list[dict]:
        """Generate N optimization proposals in parallel."""
        lenses = [
            ("algorithmic", "Data structure choices, complexity reduction, asymptotic bounds"),
            ("dependency", "Eliminate unnecessary deps, stdlib over third-party"),
            ("memory", "__slots__, object overhead, zero-copy, memoryview"),
            ("distribution", "Binary size, static linking, compilation"),
            ("bit_level", "IntEnum vs str Enum, bitfield packing, binary formats"),
        ]

        tasks = []
        for lens_name, focus in lenses[:OPTIMIZATION_PROPOSAL_COUNT]:
            prompt = (
                f"You are an optimization proposer with lens: {lens_name}.\n"
                f"Focus: {focus}\n\n"
                f"Task: {self.task_description}\n"
                f"Baseline: {json.dumps(baseline, indent=1)[:2000]}\n\n"
                "Propose optimizations. Output JSON: "
                "{\"lens\": \"...\", \"proposals\": [{\"description\": \"...\", "
                "\"estimated_improvement_pct\": 0, \"risk\": \"low|medium|high\", "
                "\"implementation_steps\": [\"...\"]}]}"
            )
            tasks.append(
                call_llm(
                    [
                        {"role": "system", "content": "You are an optimization expert. Output only JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=OPTIMIZATION_PROPOSER_TEMPERATURE,
                    response_format={"type": "json_object"},
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                )
            )

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        proposals = []
        for resp in responses:
            if isinstance(resp, Exception):
                continue
            self._track_tokens(resp)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            try:
                proposals.append(json.loads(content))
            except json.JSONDecodeError:
                pass

        return proposals

    # ------------------------------------------------------------------
    # Phase 3: Judging
    # ------------------------------------------------------------------

    async def _phase_judging(
        self, proposals: list[dict]
    ) -> tuple[list[dict], int, float, dict, dict, bool]:
        """Judge proposals and select winner via majority vote.

        Returns (all_scores, winner_idx, winner_score, round1_tally, round2_tally, revote_occurred).
        winner_idx == -1 signals no majority after both rounds.
        """
        if not proposals:
            return [], 0, 0.0, {}, {}, False

        majority_threshold = OPTIMIZATION_JUDGE_COUNT / 2

        def _build_prompt(extra_context: str = "") -> str:
            p = (
                "Rate each optimization proposal on a scale of 0-100.\n"
                "Consider: estimated improvement, implementation risk, code complexity impact.\n\n"
            )
            for i, prop in enumerate(proposals):
                p += f"\n--- Proposal {i} ---\n{json.dumps(prop, indent=1)[:1500]}\n"
            if extra_context:
                p += f"\n{extra_context}\n"
            p += (
                "\nOutput JSON: {\"scores\": [{\"index\": 0, \"score\": 0, \"rationale\": \"...\"}], "
                "\"winner_index\": 0}"
            )
            return p

        async def _run_judges(judge_prompt: str) -> tuple[list[dict], dict[int, int]]:
            tasks = []
            for _ in range(OPTIMIZATION_JUDGE_COUNT):
                tasks.append(
                    call_llm(
                        [
                            {"role": "system", "content": "You are an optimization judge. Output only JSON."},
                            {"role": "user", "content": judge_prompt},
                        ],
                        base_url=self.llm_base_url,
                        model=self.llm_model,
                        temperature=OPTIMIZATION_JUDGE_TEMPERATURE,
                        response_format={"type": "json_object"},
                        task_id=self.task_id,
                        llm_id=self.llm_id,
                        budget_id=self.budget_id,
                    )
                )
            responses = await asyncio.gather(*tasks, return_exceptions=True)
            scores: list[dict] = []
            votes: dict[int, int] = {}
            for resp in responses:
                if isinstance(resp, Exception):
                    continue
                self._track_tokens(resp)
                content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
                try:
                    data = json.loads(content)
                    scores.append(data)
                    winner = data.get("winner_index", 0)
                    votes[winner] = votes.get(winner, 0) + 1
                except json.JSONDecodeError:
                    pass
            return scores, votes

        # --- Round 1 ---
        r1_scores, r1_votes = await _run_judges(_build_prompt())
        r1_tally = dict(r1_votes)
        best_r1 = max(r1_votes, key=r1_votes.get) if r1_votes else None
        if best_r1 is not None and r1_votes[best_r1] > majority_threshold:
            winner_score = r1_votes[best_r1] / OPTIMIZATION_JUDGE_COUNT
            return r1_scores, best_r1, winner_score, r1_tally, {}, False

        # --- No majority — revote with context ---
        logger.info("[optimization] No majority in round 1 (tally=%s). Running revote.", r1_tally)
        vote_summary = ", ".join(
            f"Proposal {idx}: {count} vote(s)" for idx, count in sorted(r1_tally.items())
        )
        revote_context = (
            f"Previous round had no majority. Votes: {vote_summary}. "
            "Please reconsider and vote for the strongest proposal."
        )
        r2_scores, r2_votes = await _run_judges(_build_prompt(revote_context))
        r2_tally = dict(r2_votes)
        best_r2 = max(r2_votes, key=r2_votes.get) if r2_votes else None
        if best_r2 is not None and r2_votes[best_r2] > majority_threshold:
            winner_score = r2_votes[best_r2] / OPTIMIZATION_JUDGE_COUNT
            return r1_scores + r2_scores, best_r2, winner_score, r1_tally, r2_tally, True

        # --- Still no majority ---
        logger.warning("[optimization] No majority after revote (r1=%s, r2=%s).", r1_tally, r2_tally)
        return r1_scores + r2_scores, -1, 0.0, r1_tally, r2_tally, True

    # ------------------------------------------------------------------
    # Phase 4: Implementation
    # ------------------------------------------------------------------

    async def _phase_implementation(self, proposal: dict) -> bool:
        """Implement the winning optimization (simplified — single LLM call)."""
        prompt = (
            "Implement the following optimization.\n\n"
            f"Proposal: {json.dumps(proposal, indent=1)[:3000]}\n\n"
            "Describe the exact code changes needed. "
            "Output JSON: {\"success\": true/false, \"changes\": [{\"file\": \"...\", \"description\": \"...\"}]}"
        )

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": "You are an implementation agent. Output only JSON."},
                    {"role": "user", "content": prompt},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=OPTIMIZATION_IMPL_TEMPERATURE,
                response_format={"type": "json_object"},
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )
            self._track_tokens(response)
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            data = json.loads(content)
            return data.get("success", True)
        except Exception as e:
            logger.warning("[optimization] Implementation failed: %s", e)
            return False

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def _compare_reports(
        self, baseline: dict, post: dict
    ) -> tuple[str, str]:
        """Compare baseline vs post reports and determine outcome."""
        b_score = baseline.get("complexity_score", 100)
        p_score = post.get("complexity_score", 100)

        if b_score == 0:
            improvement = 0.0
        else:
            improvement = ((b_score - p_score) / b_score) * 100

        if improvement < -OPTIMIZATION_MAX_REGRESSION_PCT:
            return "rejected", f"Regression of {abs(improvement):.1f}% exceeds {OPTIMIZATION_MAX_REGRESSION_PCT}% limit."

        if improvement < OPTIMIZATION_MIN_IMPROVEMENT_PCT:
            return "skipped", f"Improvement of {improvement:.1f}% below {OPTIMIZATION_MIN_IMPROVEMENT_PCT}% threshold."

        return "optimized", f"Improvement of {improvement:.1f}% achieved."

    def _track_tokens(self, response: dict) -> None:
        usage = response.get("usage", {})
        self._total_prompt += usage.get("prompt_tokens", 0)
        self._total_completion += usage.get("completion_tokens", 0)

    def _store_result(self, result: OptimizationPipelineResult) -> None:
        try:
            from app.database import create_optimization_result
            create_optimization_result(
                task_id=result.task_id,
                outcome=result.outcome,
                baseline_report=json.dumps(result.baseline_report),
                proposals=json.dumps(result.proposals),
                judge_scores=json.dumps(result.judge_scores),
                winning_proposal_index=result.winning_proposal_index,
                winning_score=int(result.winning_score * 100),
                post_report=json.dumps(result.post_report),
                improvement_summary=result.improvement_summary,
                total_prompt_tokens=result.prompt_tokens,
                total_completion_tokens=result.completion_tokens,
            )
        except Exception as e:
            logger.error("[optimization] Failed to store result: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_optimization_pipeline(
    task_id: str,
    task_description: str,
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> dict:
    """Run the optimization pipeline and return a result dict."""
    pipeline = OptimizationPipeline(
        task_id=task_id,
        task_description=task_description,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
    )
    result = await pipeline.run()
    return {
        "task_id": result.task_id,
        "outcome": result.outcome,
        "improvement_summary": result.improvement_summary,
        "winning_proposal_index": result.winning_proposal_index,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
    }
