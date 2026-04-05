"""
app/agent/optimization.py
--------------------------
Optimization Pipeline - profile -> propose -> vote -> implement -> verify.

5-phase flow:
  1. Profiling Agent -> BaselineReport
  2. 5x Proposers in parallel -> Optimization Proposals
  3. 3x Judges in parallel -> Ranked proposals
  4. Implementation Agent -> Code changes
  5. Profiling Agent again -> Compare vs baseline
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
    OPTIMIZATION_PROPOSER_TEMPERATURE,
    OPTIMIZATION_JUDGE_TEMPERATURE,
    OPTIMIZATION_IMPL_TEMPERATURE,
    OPTIMIZATION_MIN_IMPROVEMENT_PCT,
    OPTIMIZATION_MAX_REGRESSION_PCT,
    OPTIMIZATION_MAX_REVIEWER_TURNS,
    OPTIMIZATION_REVIEWER_TOOLS,
    OPTIMIZATION_COMPUTE_WEIGHT,
    OPTIMIZATION_MEMORY_WEIGHT,
    OPTIMIZATION_READABILITY_PENALTY_MAX,
    OPTIMIZATION_PREMATURE_MULTIPLIER,
    OPTIMIZATION_TECH_DEBT_BONUS_PCT,
    BIG_O_RANKING,
    OPTIMIZATION_BIG_O_BONUS_PCT,
)
from app.agent.json_utils import extract_json_block
from app.agent.tools import dispatch_tool, build_tool_schemas
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "Optimization Pipeline"


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

    _REVIEWER_SCHEMAS: list[dict] = build_tool_schemas(OPTIMIZATION_REVIEWER_TOOLS)

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
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        logger.info(f"[{AGENT_NAME}] Starting for task '%s'", self.task_id)

        # Phase 1: Baseline profiling
        baseline = await self._phase_profiling("baseline")
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        # Phase 2: Parallel proposals
        proposals = await self._phase_proposals(baseline)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        # Phase 3: Judge proposals
        scores, winner_idx, winner_score, r1_tally, r2_tally, revote = await self._phase_judging(proposals)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        # No majority after revote - reject and demote to indev
        if winner_idx == -1:
            reason = (
                f"No consensus on optimization approach after revote. "
                f"Round 1: {r1_tally}, Round 2: {r2_tally}."
            )
            logger.warning(f"[{AGENT_NAME}] Rejecting task '%s': %s", self.task_id, reason)
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
            logger.info(f"[{AGENT_NAME}] No viable proposals, skipping optimization.")
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
        outcome, summary = self._compare_reports(baseline, post, parent_task_id=self.task_id)

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
        """Run profiling analysis via LLM mini-loop."""
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        prompt = (
            f"You are a performance profiler running {phase_name} analysis.\n"
            f"Task: {self.task_description}\n\n"
            "Step 1 - Read the relevant source files to understand the code.\n"
            "Step 2 - Determine the Big O class of the critical path by reading the code "
            "(do not guess; trace the actual algorithm).\n"
            "Step 3 - Run a synthetic benchmark using run_shell with a Python one-liner. "
            "Choose scale_n based on operation type: N=10_000 for I/O-bound, "
            "N=100_000 for CPU-bound, N=1_000_000 for trivial ops. Example:\n"
            "  python -c \"import time; start=time.perf_counter(); [your_op() for _ in range(N)]; "
            "print((time.perf_counter()-start)*1000)\"\n"
            "Step 4 - Estimate peak memory usage (RSS) during the benchmark if measurable.\n"
            "Step 5 - Identify hotspots (function/line references).\n"
            "Step 6 - Rate readability_cost from 0.0 (simple, clear) to 1.0 (requires deep "
            "expertise to understand or maintain).\n"
            "Step 7 - Determine if this optimization targets a real measured bottleneck "
            "(is_premature=false) or an assumed one (is_premature=true).\n"
            "Step 8 - Determine if this resolves known tech debt (tech_debt_resolved=true/false).\n\n"
            "Output JSON:\n"
            "{\"test_duration_ms\": 0, \"memory_peak_mb\": 0, \"dep_count\": 0, \"hotspots\": [], "
            "\"complexity_score\": 0, \"big_o_class\": \"O(n)\", \"scale_n\": 10000, "
            "\"readability_cost\": 0.0, \"is_premature\": false, \"tech_debt_resolved\": false, "
            "\"notes\": \"\"}"
        )

        messages: list[dict] = [
            {"role": "system", "content": "You are a performance profiler. Output your report as JSON when ready."},
            {"role": "user", "content": prompt},
        ]

        max_turns = OPTIMIZATION_MAX_REVIEWER_TURNS

        for turn in range(max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")
            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=0.1,
                    tools=self._REVIEWER_SCHEMAS,
                    tool_choice="auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            except Exception as e:
                logger.warning(f"[{AGENT_NAME}] Profiling (%s) LLM call failed: %s", phase_name, e)
                return {"error": str(e)}

            self._track_tokens(response)
            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_msg)
            tool_calls = assistant_msg.get("tool_calls") or []
            content = assistant_msg.get("content") or ""

            if tool_calls:
                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": tc_result,
                    })
                continue

            raw = extract_json_block(content)
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and ("test_duration_ms" in data or "complexity_score" in data or "big_o_class" in data or "error" in data):
                        return data
                except (json.JSONDecodeError, ValueError):
                    pass

            turns_remaining = max_turns - turn - 1
            if turns_remaining <= 2:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] {turns_remaining} turns remaining. Output JSON profiling report now.",
                })

        return {"error": "Profiler exhausted turns"}

    # ------------------------------------------------------------------
    # Phase 2: Proposals
    # ------------------------------------------------------------------

    async def _run_single_proposer(
        self, lens_name: str, focus: str, baseline: dict
    ) -> dict | None:
        """Run a single optimization proposer using a mini-loop."""
        prompt = (
            f"You are an optimization proposer with lens: {lens_name}.\n"
            f"Focus: {focus}\n\n"
            f"Task: {self.task_description}\n"
            f"Baseline: {json.dumps(baseline, indent=1)[:2000]}\n\n"
            "You may use tools to inspect code files.\n\n"
            "Propose optimizations. Output JSON: "
            "{\"lens\": \"...\", \"proposals\": [{\"description\": \"...\", "
            "\"estimated_improvement_pct\": 0, \"risk\": \"low|medium|high\", "
            "\"implementation_steps\": [\"...\"]}]}"
        )

        messages: list[dict] = [
            {"role": "system", "content": "You are an optimization expert. Output your proposals as JSON when ready."},
            {"role": "user", "content": prompt},
        ]

        max_turns = OPTIMIZATION_MAX_REVIEWER_TURNS

        for turn in range(max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")
            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=OPTIMIZATION_PROPOSER_TEMPERATURE,
                    tools=self._REVIEWER_SCHEMAS,
                    tool_choice="auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            except Exception as e:
                logger.warning(f"[{AGENT_NAME}] Proposer (%s) LLM call failed: %s", lens_name, e)
                return None

            self._track_tokens(response)
            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_msg)
            tool_calls = assistant_msg.get("tool_calls") or []
            content = assistant_msg.get("content") or ""

            if tool_calls:
                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": tc_result,
                    })
                continue

            raw = extract_json_block(content)
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and "lens" in data:
                        return data
                except (json.JSONDecodeError, ValueError):
                    pass

            turns_remaining = max_turns - turn - 1
            if turns_remaining <= 2:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] {turns_remaining} turns remaining. Output JSON proposals now.",
                })

        return None

    async def _phase_proposals(self, baseline: dict) -> list[dict]:
        """Generate N optimization proposals in parallel."""
        lenses = [
            ("algorithmic", "Data structure choices, complexity reduction, asymptotic bounds"),
            ("dependency", "Eliminate unnecessary deps, stdlib over third-party"),
            ("memory", "__slots__, object overhead, zero-copy, memoryview"),
            ("distribution", "Binary size, static linking, compilation"),
            ("bit_level", "IntEnum vs str Enum, bitfield packing, binary formats"),
        ]

        tasks = [
            self._run_single_proposer(lens_name, focus, baseline)
            for lens_name, focus in lenses[:OPTIMIZATION_PROPOSAL_COUNT]
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        proposals = []
        for result in results:
            if result is None or isinstance(result, Exception):
                continue
            proposals.append(result)

        return proposals

    # ------------------------------------------------------------------
    # Phase 3: Judging
    # ------------------------------------------------------------------

    async def _run_single_judge(self, judge_prompt: str) -> dict | None:
        """Run a single optimization judge using a mini-loop."""
        messages: list[dict] = [
            {"role": "system", "content": "You are an optimization judge. Output your verdict as JSON when ready."},
            {"role": "user", "content": judge_prompt},
        ]

        max_turns = OPTIMIZATION_MAX_REVIEWER_TURNS

        for turn in range(max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")
            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=OPTIMIZATION_JUDGE_TEMPERATURE,
                    tools=self._REVIEWER_SCHEMAS,
                    tool_choice="auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            except Exception as e:
                logger.warning(f"[{AGENT_NAME}] Judge LLM call failed: %s", e)
                return None

            self._track_tokens(response)
            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_msg)
            tool_calls = assistant_msg.get("tool_calls") or []
            content = assistant_msg.get("content") or ""

            if tool_calls:
                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "name": tc["function"]["name"],
                        "content": tc_result,
                    })
                continue

            raw = extract_json_block(content)
            if raw:
                try:
                    data = json.loads(raw)
                    if isinstance(data, dict) and "winner_index" in data:
                        return data
                except (json.JSONDecodeError, ValueError):
                    pass

            turns_remaining = max_turns - turn - 1
            if turns_remaining <= 2:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] {turns_remaining} turns remaining. Output JSON judgment now.",
                })

        return None

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
            judge_tasks = [self._run_single_judge(judge_prompt) for _ in range(OPTIMIZATION_JUDGE_COUNT)]
            results = await asyncio.gather(*judge_tasks, return_exceptions=True)
            scores: list[dict] = []
            votes: dict[int, int] = {}
            for result in results:
                if result is None or isinstance(result, Exception):
                    continue
                scores.append(result)
                winner = result.get("winner_index", 0)
                votes[winner] = votes.get(winner, 0) + 1
            return scores, votes

        # --- Round 1 ---
        r1_scores, r1_votes = await _run_judges(_build_prompt())
        r1_tally = dict(r1_votes)
        best_r1 = max(r1_votes, key=r1_votes.get) if r1_votes else None
        if best_r1 is not None and r1_votes[best_r1] > majority_threshold:
            winner_score = r1_votes[best_r1] / OPTIMIZATION_JUDGE_COUNT
            return r1_scores, best_r1, winner_score, r1_tally, {}, False

        # --- No majority - revote with context ---
        logger.info(f"[{AGENT_NAME}] No majority in round 1 (tally=%s). Running revote.", r1_tally)
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
        logger.warning(f"[{AGENT_NAME}] No majority after revote (r1=%s, r2=%s).", r1_tally, r2_tally)
        return r1_scores + r2_scores, -1, 0.0, r1_tally, r2_tally, True

    # ------------------------------------------------------------------
    # Phase 4: Implementation
    # ------------------------------------------------------------------

    async def _phase_implementation(self, proposal: dict) -> bool:
        """
        Spawn Kanban sub-task cards for each winning optimization proposal so they
        flow through the full pipeline (IDEA -> PLANNING -> INDEV -> ...).
        Returns True when all sub-tasks reach a terminal state, False on timeout.
        """
        from app.database import create_task, get_task, update_task

        parent = get_task(self.task_id)
        if not parent:
            logger.warning(f"[{AGENT_NAME}] Parent task '%s' not found.", self.task_id)
            return False

        proposals_list = proposal.get("proposals", [proposal])
        sub_task_ids: list[str] = []

        for opt in proposals_list:
            description = self._build_subtask_description(opt, proposal.get("lens", "general"))
            sub_task = create_task(
                title=f"Opt: {opt.get('description', 'unnamed')[:60]}",
                task_type="idea",
                description=description,
                owner="maestro-optimizer",
                tags=["optimization", f"parent:{self.task_id}", f"risk:{opt.get('risk', 'unknown')}"],
                llm_id=parent.llm_id,
                budget_id=parent.budget_id,
                # Inherit parent's prereqs (NOT parent's ID - avoids deadlock)
                prerequisites=list(parent.prerequisites or []),
                project=parent.project or "TheMaestro",
            )
            if sub_task:
                update_task(
                    sub_task.id,
                    parent_task_id=self.task_id,
                    subdivision_generation=(parent.subdivision_generation or 0) + 1,
                )
                sub_task_ids.append(sub_task.id)
                logger.info(
                    f"[{AGENT_NAME}] Created sub-task '%s' for proposal: %s",
                    sub_task.id, opt.get("description", "?")[:60],
                )

        if not sub_task_ids:
            logger.warning(f"[{AGENT_NAME}] No sub-tasks created for task '%s'.", self.task_id)
            return False

        update_task(self.task_id, is_big_idea=True)
        return await self._wait_for_subtasks(sub_task_ids)

    def _build_subtask_description(self, opt: dict, lens: str) -> str:
        """Build a rich markdown task description for an optimization sub-task."""
        lines = [
            f"## Optimization Sub-task (lens: {lens})",
            "",
            f"**Parent task:** `{self.task_id}`",
            "",
            "### Proposal",
            f"{opt.get('description', 'No description provided.')}",
            "",
        ]
        if opt.get("rationale"):
            lines += ["### Rationale", opt["rationale"], ""]
        if opt.get("implementation_steps"):
            lines += ["### Implementation Steps"]
            for step in opt["implementation_steps"]:
                lines.append(f"- {step}")
            lines.append("")
        lines += [
            "### Benchmarking Requirements",
            "",
            "**Compute time is the most precious resource in this project.** "
            "Improvements to CPU/wall time are weighted highest in the decision framework.",
            "",
            "**Before making any code changes**, run a timed benchmark using `run_shell`:",
            "```bash",
            "python -c \"import time; start=time.perf_counter(); [YOUR_OP for _ in range(N)]; print((time.perf_counter()-start)*1000)\"",
            "```",
            "Choose `scale_n`: N=10_000 for I/O-bound, N=100_000 for CPU-bound, N=1_000_000 for trivial ops.",
            "",
            f"Then call `record_benchmark` with `benchmark_type='before'`, `parent_task_id='{self.task_id}'`, and all required metrics:",
            "```json",
            "{",
            "  \"test_duration_ms\": 0,",
            "  \"memory_peak_mb\": 0,",
            "  \"complexity_score\": 0,",
            "  \"big_o_class\": \"O(n)\",",
            "  \"scale_n\": 10000,",
            "  \"readability_cost\": 0.0,",
            "  \"is_premature\": false,",
            "  \"tech_debt_resolved\": false,",
            "  \"notes\": \"\"",
            "}",
            "```",
            "",
            "**Before writing any code, ask yourself:** Is this bottleneck real and measured, "
            "or assumed? If the profiling data doesn't show it as a hotspot, set `is_premature=true`. "
            "Premature optimizations require 2× the improvement threshold to be accepted.",
            "",
            "**After implementing and verifying changes**, run the same benchmark at the same `scale_n` "
            "and call `record_benchmark` with `benchmark_type='after'` and updated metrics.",
            "",
            "**Readability cost:** Rate 0.0 (identical readability to before) to 1.0 (requires deep "
            "expertise to maintain). Be honest - clever code has a carrying cost and will reduce your score.",
            "",
            f"Risk level: **{opt.get('risk', 'unknown')}**",
        ]
        return "\n".join(lines)

    async def _wait_for_subtasks(
        self,
        sub_task_ids: list[str],
        poll_interval: float = 10.0,
        timeout: float = 3600.0,
    ) -> bool:
        """
        Poll until all sub-tasks reach a terminal state (completed/accepted/rejected)
        or timeout is exceeded.  Returns True if all completed successfully.
        """
        from app.database import get_task
        from app.agent.config import PIPELINE_DONE_STATUSES

        terminal = PIPELINE_DONE_STATUSES | {"rejected", "idea"}  # idea = stuck/not advancing
        deadline = asyncio.get_event_loop().time() + timeout

        while asyncio.get_event_loop().time() < deadline:
            statuses = []
            for tid in sub_task_ids:
                task = get_task(tid)
                statuses.append(task.type if task else "missing")

            done = [s in terminal for s in statuses]
            logger.debug(
                f"[{AGENT_NAME}] Sub-task poll: %s",
                dict(zip(sub_task_ids, statuses)),
            )

            if all(done):
                succeeded = all(
                    s in PIPELINE_DONE_STATUSES for s in statuses
                )
                return succeeded

            await asyncio.sleep(poll_interval)

        logger.warning(
            f"[{AGENT_NAME}] Sub-task wait timed out after %.0fs for task '%s'.",
            timeout, self.task_id,
        )
        return False

    # ------------------------------------------------------------------
    # Comparison
    # ------------------------------------------------------------------

    def _compare_reports(
        self, baseline: dict, post: dict, parent_task_id: str | None = None
    ) -> tuple[str, str]:
        """Compare baseline vs post reports and determine outcome.

        If parent_task_id is given and the optimization_benchmarks table has
        before/after rows for it, use those real metrics.  Otherwise fall back
        to the complexity_score from the profiling dicts.
        """
        if parent_task_id:
            try:
                from app.database import get_optimization_benchmarks
                records = get_optimization_benchmarks(parent_task_id)
                before_records = [r for r in records if r.benchmark_type == "before"]
                after_records = [r for r in records if r.benchmark_type == "after"]
                if before_records and after_records:
                    return self._compare_benchmarks(before_records, after_records)
            except Exception as exc:
                logger.warning(f"[{AGENT_NAME}] Could not load benchmarks for '%s': %s", parent_task_id, exc)

        # Fallback: use complexity_score from profiling dicts
        b_score = baseline.get("complexity_score", 100)
        p_score = post.get("complexity_score", 100)

        if b_score == 0:
            improvement = 0.0
        else:
            improvement = ((b_score - p_score) / b_score) * 100

        # Apply Big O bonus if both dicts have big_o_class
        big_o_note = ""
        b_big_o = baseline.get("big_o_class", "")
        p_big_o = post.get("big_o_class", "")
        if b_big_o and p_big_o:
            b_rank = BIG_O_RANKING.get(b_big_o)
            p_rank = BIG_O_RANKING.get(p_big_o)
            if b_rank is not None and p_rank is not None:
                rank_delta = b_rank - p_rank
                if rank_delta > 0:
                    bonus = rank_delta * OPTIMIZATION_BIG_O_BONUS_PCT
                    improvement += bonus
                    big_o_note = f", Big O {b_big_o}->{p_big_o} (+{bonus:.0f}%)"

        suffix = f"profiling data{big_o_note}"

        if improvement < -OPTIMIZATION_MAX_REGRESSION_PCT:
            return "rejected", f"Regression of {abs(improvement):.1f}% exceeds {OPTIMIZATION_MAX_REGRESSION_PCT}% limit. ({suffix})"

        if improvement < OPTIMIZATION_MIN_IMPROVEMENT_PCT:
            return "skipped", f"Improvement of {improvement:.1f}% below {OPTIMIZATION_MIN_IMPROVEMENT_PCT}% threshold. ({suffix})"

        return "optimized", f"Improvement of {improvement:.1f}% achieved. ({suffix})"

    def _compare_benchmarks(self, before_records: list, after_records: list) -> tuple[str, str]:
        """Weighted multi-metric comparison of before/after benchmark records."""
        import json as _json

        def _parse_metrics(record) -> dict:
            try:
                return _json.loads(record.metrics)
            except Exception:
                return {}

        # Group by task_id
        before_by_task: dict[str, dict] = {}
        for r in before_records:
            m = _parse_metrics(r)
            if m:
                before_by_task[r.task_id] = m

        after_by_task: dict[str, dict] = {}
        for r in after_records:
            m = _parse_metrics(r)
            if m:
                after_by_task[r.task_id] = m

        common_tasks = set(before_by_task) & set(after_by_task)
        if not common_tasks:
            return "skipped", "Benchmark data present but no matching before/after task pairs found."

        task_scores: list[float] = []
        is_premature_any = False
        tech_debt_resolved_any = False
        big_o_transitions: list[str] = []

        for tid in common_tasks:
            bm = before_by_task[tid]
            am = after_by_task[tid]

            # --- Compute improvement (lower is better -> (before - after) / before × 100) ---
            compute_imp = 0.0
            compute_weight_used = 0.0
            b_dur = bm.get("test_duration_ms")
            a_dur = am.get("test_duration_ms")
            if b_dur is not None and a_dur is not None and float(b_dur) != 0:
                compute_imp = ((float(b_dur) - float(a_dur)) / float(b_dur)) * 100.0
                compute_weight_used = OPTIMIZATION_COMPUTE_WEIGHT

            memory_imp = 0.0
            memory_weight_used = 0.0
            b_mem = bm.get("memory_peak_mb")
            a_mem = am.get("memory_peak_mb")
            if b_mem is not None and a_mem is not None and float(b_mem) != 0:
                memory_imp = ((float(b_mem) - float(a_mem)) / float(b_mem)) * 100.0
                memory_weight_used = OPTIMIZATION_MEMORY_WEIGHT

            # Weighted aggregate (only include metrics we have data for)
            total_weight = compute_weight_used + memory_weight_used
            if total_weight == 0:
                # No duration or memory data - fall back to complexity_score
                b_score = bm.get("complexity_score", 100)
                a_score = am.get("complexity_score", 100)
                weighted_imp = ((float(b_score) - float(a_score)) / float(b_score) * 100.0) if float(b_score) != 0 else 0.0
            else:
                weighted_imp = (
                    compute_imp * compute_weight_used + memory_imp * memory_weight_used
                ) / total_weight

            # --- Big O bonus ---
            b_big_o = bm.get("big_o_class", "")
            a_big_o = am.get("big_o_class", "")
            big_o_bonus = 0.0
            if b_big_o and a_big_o:
                b_rank = BIG_O_RANKING.get(b_big_o)
                a_rank = BIG_O_RANKING.get(a_big_o)
                if b_rank is not None and a_rank is not None:
                    rank_delta = b_rank - a_rank  # positive = improvement
                    if rank_delta > 0:
                        big_o_bonus = rank_delta * OPTIMIZATION_BIG_O_BONUS_PCT
                        big_o_transitions.append(f"{b_big_o}->{a_big_o} (+{big_o_bonus:.0f}%)")
            weighted_imp += big_o_bonus

            # --- Readability penalty (from after record) ---
            readability_cost = am.get("readability_cost")
            if readability_cost is not None:
                penalty_fraction = float(readability_cost) * OPTIMIZATION_READABILITY_PENALTY_MAX
                weighted_imp *= (1.0 - penalty_fraction)

            # --- Qualitative flags ---
            if am.get("is_premature") is True:
                is_premature_any = True
            if am.get("tech_debt_resolved") is True:
                tech_debt_resolved_any = True

            task_scores.append(weighted_imp)

        improvement = sum(task_scores) / len(task_scores)

        # --- Tech debt bonus ---
        if tech_debt_resolved_any:
            improvement += OPTIMIZATION_TECH_DEBT_BONUS_PCT

        # --- Effective threshold (premature multiplier) ---
        effective_min = (
            OPTIMIZATION_MIN_IMPROVEMENT_PCT * OPTIMIZATION_PREMATURE_MULTIPLIER
            if is_premature_any
            else OPTIMIZATION_MIN_IMPROVEMENT_PCT
        )

        # --- Build summary ---
        details: list[str] = [f"weighted score {improvement:.1f}%"]
        if big_o_transitions:
            details.append("Big O: " + ", ".join(big_o_transitions))
        if is_premature_any:
            details.append(f"premature (threshold x{OPTIMIZATION_PREMATURE_MULTIPLIER:.0f}={effective_min:.1f}%)")
        if tech_debt_resolved_any:
            details.append(f"tech-debt bonus +{OPTIMIZATION_TECH_DEBT_BONUS_PCT:.1f}%")
        detail_str = "; ".join(details)
        n = len(common_tasks)

        if improvement < -OPTIMIZATION_MAX_REGRESSION_PCT:
            return "rejected", (
                f"Regression of {abs(improvement):.1f}% exceeds {OPTIMIZATION_MAX_REGRESSION_PCT}% limit. "
                f"(benchmark data, {detail_str}, {n} subtask(s))"
            )

        if improvement < effective_min:
            return "skipped", (
                f"Improvement of {improvement:.1f}% below {effective_min:.1f}% threshold. "
                f"(benchmark data, {detail_str}, {n} subtask(s))"
            )

        return "optimized", (
            f"Improvement of {improvement:.1f}% achieved. "
            f"(benchmark data, {detail_str}, {n} subtask(s))"
        )

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
            logger.error(f"[{AGENT_NAME}] Failed to store result: %s", e)


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
    project_path: str | None = None,
) -> dict:
    """Run the optimization pipeline and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)
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
