"""
app/agent/intake.py
-------------------
Intake pipeline orchestrator for the IDEA -> PLANNING task transition.

When a task is requested to move from IDEA to PLANNING, this module coordinates
four analysis stages to determine whether the transition should proceed:

  1. Scope Analysis (LLM) — determines task scope, complexity, and decomposition.
  2a. Static Analysis (deterministic) — tree-sitter code structure analysis.
  2b. Feasibility Analysis (LLM) — informed by stage 2a output.
  3. Conflict Detection (LLM) — checks against existing tasks for overlaps.

Execution order: 1 -> {2a, 3} in parallel -> 2b -> Tally.

Each stage produces a vote dict with a verdict from the set:
  POSSIBLE, LIKELY, NOT_SUITABLE, REJECTED, NEEDS_RESEARCH

The tally aggregates all votes into a final outcome:
  passed, rejected, needs_research, or tie.

Usage::

    result = await run_intake_pipeline(
        task_id="task-42",
        task_description="Add OAuth2 login flow",
        task_title="OAuth2 Login",
        all_tasks=[...],
    )
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
)
from app.agent.llm_client import call_llm

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Verdict constants
# ---------------------------------------------------------------------------

VERDICT_POSSIBLE = "POSSIBLE"
VERDICT_LIKELY = "LIKELY"
VERDICT_NOT_SUITABLE = "NOT_SUITABLE"
VERDICT_REJECTED = "REJECTED"
VERDICT_NEEDS_RESEARCH = "NEEDS_RESEARCH"
VERDICT_SUBDIVIDE_IDEA = "SUBDIVIDE_IDEA"

_VALID_VERDICTS = {
    VERDICT_POSSIBLE,
    VERDICT_LIKELY,
    VERDICT_NOT_SUITABLE,
    VERDICT_REJECTED,
    VERDICT_NEEDS_RESEARCH,
    VERDICT_SUBDIVIDE_IDEA,
}

# ---------------------------------------------------------------------------
# Stage system prompts
# ---------------------------------------------------------------------------

_SCOPE_SYSTEM_PROMPT = """\
You are a senior software architect performing scope analysis on a proposed task.

Analyze the task and determine:
- Overall scope (small / medium / large / epic)
- Complexity rating (1-10)
- Whether the task should be decomposed into subtasks
- Key areas of the codebase likely affected
- Estimated effort category (trivial, minor, moderate, significant, major)

You MUST respond with a JSON object matching this exact schema:
{
  "scope": "small" | "medium" | "large" | "epic",
  "complexity": <integer 1-10>,
  "decomposition_needed": <boolean>,
  "subtasks": [<string>, ...],
  "affected_areas": [<string>, ...],
  "effort": "trivial" | "minor" | "moderate" | "significant" | "major",
  "vote": {
    "verdict": "POSSIBLE" | "LIKELY" | "NOT_SUITABLE" | "REJECTED" | "NEEDS_RESEARCH" | "SUBDIVIDE_IDEA",
    "confidence": <float 0.0-1.0>,
    "justification": "<one-paragraph explanation>"
  }
}

Verdict guidelines:
- LIKELY: Task is well-defined, reasonable scope, clearly feasible.
- POSSIBLE: Task is feasible but has some ambiguity or moderate complexity.
- NEEDS_RESEARCH: Task is too vague to assess — needs clarification before proceeding.
- NOT_SUITABLE: Task is poorly scoped, too large without decomposition, or architecturally questionable.
- REJECTED: Task is fundamentally unfeasible, contradictory, or harmful to the project.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large to implement in a single context window. Should be decomposed into smaller pieces. Only use when the task is good but genuinely too big — not vague (NEEDS_RESEARCH) or bad (REJECTED).

Respond ONLY with the JSON object. No markdown fences, no extra text.\
"""

_FEASIBILITY_SYSTEM_PROMPT = """\
You are a senior software engineer performing feasibility analysis on a proposed task.

You will receive:
1. The task description and title.
2. A structural analysis of the current codebase (file counts, languages, module structure).

Your job is to assess:
- Whether the task is technically feasible given the current codebase structure.
- What ambiguities or unknowns exist that could block implementation.
- Whether external dependencies or APIs are needed.
- What risks or edge cases should be considered.
- Whether the codebase is in a state that can accommodate this change.

You MUST respond with a JSON object matching this exact schema:
{
  "feasibility_rating": <float 0.0-1.0>,
  "ambiguities": [<string>, ...],
  "external_dependencies": [<string>, ...],
  "risks": [<string>, ...],
  "codebase_readiness": "ready" | "needs_refactoring" | "incompatible",
  "vote": {
    "verdict": "POSSIBLE" | "LIKELY" | "NOT_SUITABLE" | "REJECTED" | "NEEDS_RESEARCH" | "SUBDIVIDE_IDEA",
    "confidence": <float 0.0-1.0>,
    "justification": "<one-paragraph explanation>"
  }
}

Verdict guidelines:
- LIKELY: Codebase is ready, no major blockers, dependencies are available.
- POSSIBLE: Feasible but some refactoring or dependency resolution needed.
- NEEDS_RESEARCH: Cannot determine feasibility — too many unknowns.
- NOT_SUITABLE: Significant architectural incompatibilities or missing foundations.
- REJECTED: Fundamentally impossible given the current system.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large to implement in a single context window. Should be decomposed into smaller pieces. Only use when the task is good but genuinely too big — not vague (NEEDS_RESEARCH) or bad (REJECTED).

Respond ONLY with the JSON object. No markdown fences, no extra text.\
"""

_CONFLICT_SYSTEM_PROMPT = """\
You are a project coordinator performing conflict detection on a proposed task.

You will receive:
1. The proposed task description, title, and scope analysis.
2. A list of all current non-completed tasks in the project.

Your job is to detect:
- File-level conflicts: tasks that are likely to modify the same files.
- Semantic conflicts: tasks with overlapping or contradictory goals.
- Priority conflicts: tasks that should be done first as prerequisites.
- Resource conflicts: tasks that compete for the same limited resources.

You MUST respond with a JSON object matching this exact schema:
{
  "file_conflicts": [
    {"task_id": "<id>", "task_title": "<title>", "shared_files": [<string>, ...], "severity": "low" | "medium" | "high"}
  ],
  "semantic_conflicts": [
    {"task_id": "<id>", "task_title": "<title>", "overlap": "<description>", "severity": "low" | "medium" | "high"}
  ],
  "priority_conflicts": [
    {"task_id": "<id>", "task_title": "<title>", "reason": "<why this should come first>"}
  ],
  "resource_conflicts": [
    {"task_id": "<id>", "task_title": "<title>", "resource": "<what they compete for>"}
  ],
  "vote": {
    "verdict": "POSSIBLE" | "LIKELY" | "NOT_SUITABLE" | "REJECTED" | "NEEDS_RESEARCH" | "SUBDIVIDE_IDEA",
    "confidence": <float 0.0-1.0>,
    "justification": "<one-paragraph explanation>"
  }
}

Verdict guidelines:
- LIKELY: No significant conflicts detected; safe to proceed.
- POSSIBLE: Minor conflicts exist but are manageable with coordination.
- NEEDS_RESEARCH: Potential conflicts detected but need human review to resolve.
- NOT_SUITABLE: High-severity conflicts that would cause integration problems.
- REJECTED: Direct contradictions with active tasks that cannot be reconciled.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large to implement in a single context window. Should be decomposed into smaller pieces. Only use when the task is good but genuinely too big — not vague (NEEDS_RESEARCH) or bad (REJECTED).

Respond ONLY with the JSON object. No markdown fences, no extra text.\
"""


# ---------------------------------------------------------------------------
# IntakePipeline
# ---------------------------------------------------------------------------

class IntakePipeline:
    """
    Orchestrates the IDEA -> PLANNING transition pipeline.

    Stages:
      1. Scope Analysis (LLM) - determines task scope and decomposition
      2a. Static Analysis (deterministic) - tree-sitter code structure analysis
      2b. Feasibility Analysis (LLM) - informed by 2a output
      3. Conflict Detection (LLM) - checks against existing tasks

    Execution order: 1 -> {2a, 3} in parallel -> 2b -> Tally
    """

    def __init__(
        self,
        task_id: str,
        task_description: str,
        task_title: str,
        all_tasks: list[dict],
        budget_id: int | None = None,
        llm_id: int | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.task_description = task_description
        self.task_title = task_title
        self.all_tasks = all_tasks
        self.budget_id = budget_id
        self.llm_id = llm_id
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.votes: list[dict] = []  # Collect votes from each stage

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Execute the full pipeline. Returns tally result dict."""
        # Stage 1: Scope Analysis
        scope_vote = await self._stage_scope_analysis()
        self.votes.append(scope_vote)

        # Check for immediate rejection
        if scope_vote["verdict"] == VERDICT_REJECTED:
            return self._build_tally()

        # Stage 2a and Stage 3 in parallel
        static_vote, conflict_vote = await asyncio.gather(
            self._stage_static_analysis(scope_vote),
            self._stage_conflict_detection(scope_vote),
        )
        self.votes.append(static_vote)
        self.votes.append(conflict_vote)

        # Stage 2b: Feasibility (needs 2a output)
        feasibility_vote = await self._stage_feasibility(scope_vote, static_vote)
        self.votes.append(feasibility_vote)

        # Initial tally
        tally = self._build_tally()

        # Handle NEEDS_RESEARCH — spawn research agents for those stages
        if tally["outcome"] == "needs_research":
            tally = await self._handle_needs_research(tally)

        # Handle SUBDIVIDE — delegate to subdivision agent
        if tally["outcome"] == "subdivide":
            tally = await self._handle_subdivide(tally)

        # Handle TIE — spawn tie-breaker research agent
        if tally["outcome"] == "tie":
            tally = await self._handle_tie(tally)

        return tally

    # ------------------------------------------------------------------
    # Research & tie-breaker handling
    # ------------------------------------------------------------------

    async def _handle_needs_research(self, tally: dict) -> dict:
        """Spawn research agents for stages that voted NEEDS_RESEARCH, then re-tally."""
        from app.agent.research import run_research

        stages_needing_research = tally.get("research_needed", [])
        logger.info("Spawning research agents for stages: %s", stages_needing_research)

        for stage_name in stages_needing_research:
            # Find the original vote for context
            original_vote = next(
                (v for v in self.votes if v["stage"] == stage_name), None
            )
            context = {
                "task_id": self.task_id,
                "task_title": self.task_title,
                "task_description": self.task_description,
                "original_vote": original_vote,
                "stage": stage_name,
            }
            question = (
                f"Stage '{stage_name}' could not determine feasibility for task "
                f"'{self.task_title}'. Original justification: "
                f"{original_vote['justification'] if original_vote else 'unknown'}. "
                f"Investigate the codebase and determine if this task is feasible."
            )

            try:
                research_result = await run_research(
                    question=question,
                    context=context,
                    llm_base_url=self.llm_base_url,
                    llm_model=self.llm_model,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                )
                research_vote = {
                    "stage": f"{stage_name}_research",
                    "verdict": research_result.vote.get("verdict", VERDICT_NOT_SUITABLE),
                    "confidence": research_result.vote.get("confidence", 55),
                    "justification": research_result.vote.get("justification", "Research completed."),
                    "raw_response": research_result.vote,
                    "prompt_tokens": research_result.prompt_tokens,
                    "completion_tokens": research_result.completion_tokens,
                    "model": "research_agent",
                }

                # Replace the original NEEDS_RESEARCH vote with the research result
                self.votes = [
                    v if v["stage"] != stage_name else research_vote
                    for v in self.votes
                ]
                # Update the stage name in the replacement vote
                research_vote["stage"] = stage_name

            except Exception as exc:
                logger.error("Research agent for stage '%s' failed: %s", stage_name, exc)
                # Replace with NOT_SUITABLE on failure
                fallback = {
                    "stage": stage_name,
                    "verdict": VERDICT_NOT_SUITABLE,
                    "confidence": 55,
                    "justification": f"Research agent failed: {exc}",
                    "raw_response": None,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "model": "research_agent",
                }
                self.votes = [
                    v if v["stage"] != stage_name else fallback
                    for v in self.votes
                ]

        # Re-tally with updated votes
        return self._build_tally()

    async def _handle_tie(self, tally: dict) -> dict:
        """Spawn a tie-breaker research agent, then re-tally."""
        from app.agent.config import TIEBREAKER_ENABLED
        from app.agent.research import run_tiebreaker

        if not TIEBREAKER_ENABLED:
            logger.info("Tie-breaker disabled; returning tie result as-is.")
            return tally

        logger.info("Vote tie detected for task '%s'; spawning tie-breaker agent.", self.task_id)

        try:
            tiebreaker_result = await run_tiebreaker(
                task_description=f"{self.task_title}: {self.task_description}",
                votes=self.votes,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )

            tiebreaker_vote = {
                "stage": "tiebreaker",
                "verdict": tiebreaker_result.vote.get("verdict", VERDICT_NOT_SUITABLE),
                "confidence": tiebreaker_result.vote.get("confidence", 55),
                "justification": tiebreaker_result.vote.get("justification", "Tie-breaker completed."),
                "raw_response": tiebreaker_result.vote,
                "prompt_tokens": tiebreaker_result.prompt_tokens,
                "completion_tokens": tiebreaker_result.completion_tokens,
                "model": "tiebreaker_agent",
            }

            # Add the tie-breaker vote (5th voter)
            self.votes.append(tiebreaker_vote)

        except Exception as exc:
            logger.error("Tie-breaker agent failed: %s", exc)
            # On failure, add a NOT_SUITABLE vote to break the tie conservatively
            self.votes.append({
                "stage": "tiebreaker",
                "verdict": VERDICT_NOT_SUITABLE,
                "confidence": 55,
                "justification": f"Tie-breaker agent failed: {exc}. Defaulting to conservative NOT_SUITABLE.",
                "raw_response": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": "tiebreaker_agent",
            })

        # Re-tally with the tie-breaker vote included
        return self._build_tally()

    # ------------------------------------------------------------------
    # Subdivision handling
    # ------------------------------------------------------------------

    async def _handle_subdivide(self, tally: dict) -> dict:
        """Delegate to the SubdivisionAgent when votes indicate SUBDIVIDE_IDEA."""
        logger.info("Subdivision triggered for task '%s'.", self.task_id)
        # The actual subdivision work is done in main.py's _run_intake_pipeline
        # after receiving the "subdivide" outcome. We just pass the tally through.
        return tally

    # ------------------------------------------------------------------
    # LLM calling helper
    # ------------------------------------------------------------------

    async def _call_llm(self, system_prompt: str, user_prompt: str) -> dict:
        """
        Make a structured LLM call and return the parsed response.

        Returns a dict with keys:
          - content: parsed JSON object from the LLM response
          - prompt_tokens: number of prompt tokens used
          - completion_tokens: number of completion tokens used
          - model: model identifier string

        Raises httpx.HTTPStatusError on non-2xx responses.
        Raises json.JSONDecodeError if the response is not valid JSON.
        Raises httpx.TimeoutException on timeout.
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        data = await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=0.1,
            response_format={"type": "json_object"},
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
        )

        # Extract usage stats
        usage = data.get("usage", {})
        raw_content = data["choices"][0]["message"]["content"]

        # Parse the JSON content — strip markdown fences if the model added them
        cleaned = raw_content.strip()
        if cleaned.startswith("```"):
            # Remove ```json ... ``` wrapping
            lines = cleaned.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            cleaned = "\n".join(lines)

        return {
            "content": json.loads(cleaned),
            "prompt_tokens": usage.get("prompt_tokens", 0),
            "completion_tokens": usage.get("completion_tokens", 0),
            "model": data.get("model", self.llm_model),
        }

    # ------------------------------------------------------------------
    # Vote extraction helper
    # ------------------------------------------------------------------

    def _extract_vote(self, stage: str, llm_result: dict) -> dict:
        """
        Extract and normalize a vote dict from an LLM response.

        Ensures the vote has all required fields and that the verdict
        is one of the valid values. Falls back to NEEDS_RESEARCH if
        the LLM returned an unrecognized verdict.
        """
        content = llm_result["content"]
        raw_vote = content.get("vote", {})

        verdict = raw_vote.get("verdict", VERDICT_NEEDS_RESEARCH)
        if verdict not in _VALID_VERDICTS:
            logger.warning(
                "Stage '%s' returned unrecognized verdict '%s'; defaulting to NEEDS_RESEARCH.",
                stage, verdict,
            )
            verdict = VERDICT_NEEDS_RESEARCH

        confidence = raw_vote.get("confidence", 0.5)
        if not isinstance(confidence, (int, float)):
            confidence = 0.5
        confidence = max(0.0, min(1.0, float(confidence)))

        return {
            "stage": stage,
            "verdict": verdict,
            "confidence": confidence,
            "justification": raw_vote.get("justification", "No justification provided."),
            "raw_response": content,
            "prompt_tokens": llm_result.get("prompt_tokens", 0),
            "completion_tokens": llm_result.get("completion_tokens", 0),
            "model": llm_result.get("model", self.llm_model),
        }

    def _error_vote(self, stage: str, error: Exception) -> dict:
        """
        Build a fallback NEEDS_RESEARCH vote when a stage fails.

        This ensures the pipeline always produces a result even when
        LLM calls time out, return malformed JSON, or encounter
        network errors.
        """
        logger.error("Stage '%s' failed with error: %s", stage, error)
        return {
            "stage": stage,
            "verdict": VERDICT_NEEDS_RESEARCH,
            "confidence": 0.0,
            "justification": f"Stage failed with error: {error}",
            "raw_response": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model": self.llm_model,
        }

    # ------------------------------------------------------------------
    # Stage 1: Scope Analysis
    # ------------------------------------------------------------------

    async def _stage_scope_analysis(self) -> dict:
        """
        LLM-based scope analysis of the proposed task.

        Evaluates the task description to determine scope, complexity,
        whether decomposition is needed, and which areas of the codebase
        are likely affected.
        """
        user_prompt = (
            f"Task ID: {self.task_id}\n"
            f"Task Title: {self.task_title}\n\n"
            f"Task Description:\n{self.task_description}\n\n"
            f"Please analyze this task's scope and provide your assessment."
        )

        try:
            result = await self._call_llm(_SCOPE_SYSTEM_PROMPT, user_prompt)
            return self._extract_vote("scope_analysis", result)
        except Exception as exc:
            return self._error_vote("scope_analysis", exc)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            return self._error_vote("scope_analysis", exc)
        except Exception as exc:
            return self._error_vote("scope_analysis", exc)

    # ------------------------------------------------------------------
    # Stage 2a: Static Analysis (deterministic, no LLM)
    # ------------------------------------------------------------------

    async def _stage_static_analysis(self, scope_vote: dict) -> dict:
        """
        Deterministic code structure analysis using tree-sitter.

        Attempts to import and use the static_analysis module. If
        tree-sitter is not available, returns a POSSIBLE vote with
        a note that static analysis was unavailable.

        This stage runs in an executor to avoid blocking the event loop
        since tree-sitter parsing is CPU-bound.
        """
        try:
            from app.agent.static_analysis import analyze_project, generate_vote

            loop = asyncio.get_running_loop()
            # Run CPU-bound analysis in a thread executor
            analysis_result = await loop.run_in_executor(None, analyze_project)
            vote_data = await loop.run_in_executor(
                None, generate_vote, analysis_result, self.task_description,
            )

            # Normalize the vote
            verdict = vote_data.get("verdict", VERDICT_POSSIBLE)
            if verdict not in _VALID_VERDICTS:
                verdict = VERDICT_POSSIBLE

            return {
                "stage": "static_analysis",
                "verdict": verdict,
                "confidence": float(vote_data.get("confidence", 0.5)),
                "justification": vote_data.get("justification", "Static analysis completed."),
                "raw_response": vote_data,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": "static_analysis",
            }
        except ImportError:
            logger.info(
                "Static analysis module not available; returning default POSSIBLE vote."
            )
            return {
                "stage": "static_analysis",
                "verdict": VERDICT_POSSIBLE,
                "confidence": 0.3,
                "justification": "Static analysis unavailable (tree-sitter not installed). "
                                 "Defaulting to POSSIBLE — no structural objections raised.",
                "raw_response": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": "static_analysis",
            }
        except Exception as exc:
            return self._error_vote("static_analysis", exc)

    # ------------------------------------------------------------------
    # Stage 2b: Feasibility Analysis (LLM, depends on 2a)
    # ------------------------------------------------------------------

    async def _stage_feasibility(self, scope_vote: dict, static_vote: dict) -> dict:
        """
        LLM-based feasibility analysis informed by the static analysis output.

        Evaluates whether the task is technically feasible given the
        current codebase structure, identifies ambiguities, external
        dependencies, and risks.
        """
        # Build a summary of the static analysis for the LLM
        static_summary = "No structural data available."
        if static_vote.get("raw_response") is not None:
            try:
                static_summary = json.dumps(static_vote["raw_response"], indent=2, default=str)
            except (TypeError, ValueError):
                static_summary = str(static_vote["raw_response"])

        # Build a summary of the scope analysis for context
        scope_summary = "No scope data available."
        if scope_vote.get("raw_response") is not None:
            try:
                scope_summary = json.dumps(scope_vote["raw_response"], indent=2, default=str)
            except (TypeError, ValueError):
                scope_summary = str(scope_vote["raw_response"])

        user_prompt = (
            f"Task ID: {self.task_id}\n"
            f"Task Title: {self.task_title}\n\n"
            f"Task Description:\n{self.task_description}\n\n"
            f"--- Scope Analysis (from Stage 1) ---\n{scope_summary}\n\n"
            f"--- Codebase Structure (from Static Analysis) ---\n{static_summary}\n\n"
            f"Please assess the technical feasibility of this task."
        )

        try:
            result = await self._call_llm(_FEASIBILITY_SYSTEM_PROMPT, user_prompt)
            return self._extract_vote("feasibility_analysis", result)
        except Exception as exc:
            return self._error_vote("feasibility_analysis", exc)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            return self._error_vote("feasibility_analysis", exc)
        except Exception as exc:
            return self._error_vote("feasibility_analysis", exc)

    # ------------------------------------------------------------------
    # Stage 3: Conflict Detection (LLM)
    # ------------------------------------------------------------------

    async def _stage_conflict_detection(self, scope_vote: dict) -> dict:
        """
        LLM-based conflict detection against existing project tasks.

        Checks for file-level, semantic, priority, and resource conflicts
        between the proposed task and all current non-completed tasks.
        """
        # Filter to non-completed tasks for the conflict check
        active_tasks = [
            t for t in self.all_tasks
            if t.get("type", "").lower() != "completed"
        ]

        # Build a compact task list for the prompt
        task_lines: list[str] = []
        for t in active_tasks:
            task_lines.append(
                f"- ID: {t.get('id', 'unknown')}, "
                f"Title: {t.get('title', 'untitled')}, "
                f"Type/Column: {t.get('type', 'unknown')}, "
                f"Description: {t.get('description', 'no description')[:200]}"
            )
        task_list_str = "\n".join(task_lines) if task_lines else "(no active tasks)"

        # Include scope info if available
        scope_summary = "No scope data available."
        if scope_vote.get("raw_response") is not None:
            try:
                scope_summary = json.dumps(scope_vote["raw_response"], indent=2, default=str)
            except (TypeError, ValueError):
                scope_summary = str(scope_vote["raw_response"])

        user_prompt = (
            f"PROPOSED TASK:\n"
            f"  ID: {self.task_id}\n"
            f"  Title: {self.task_title}\n"
            f"  Description: {self.task_description}\n\n"
            f"--- Scope Analysis ---\n{scope_summary}\n\n"
            f"--- Current Active Tasks ---\n{task_list_str}\n\n"
            f"Please check for conflicts between the proposed task and existing tasks."
        )

        try:
            result = await self._call_llm(_CONFLICT_SYSTEM_PROMPT, user_prompt)
            return self._extract_vote("conflict_detection", result)
        except Exception as exc:
            return self._error_vote("conflict_detection", exc)
        except (json.JSONDecodeError, KeyError, IndexError) as exc:
            return self._error_vote("conflict_detection", exc)
        except Exception as exc:
            return self._error_vote("conflict_detection", exc)

    # ------------------------------------------------------------------
    # Tally builder
    # ------------------------------------------------------------------

    def _build_tally(self) -> dict:
        """
        Aggregate all votes into a final tally result.

        Outcome logic:
          - Any REJECTED verdict -> outcome is "rejected".
          - Majority NOT_SUITABLE verdicts -> outcome is "rejected".
          - Any NEEDS_RESEARCH verdict -> outcome is "needs_research".
          - Equal pass/fail counts -> outcome is "tie".
          - Otherwise -> outcome is "passed".
        """
        result: dict[str, Any] = {
            "task_id": self.task_id,
            "transition": "idea_to_planning",
            "votes": self.votes,
            "outcome": "passed",  # default
            "rejection_reasons": [],
            "research_needed": [],
            "total_prompt_tokens": sum(v.get("prompt_tokens", 0) for v in self.votes),
            "total_completion_tokens": sum(v.get("completion_tokens", 0) for v in self.votes),
        }

        # Check for SUBDIVIDE_IDEA — immediate subdivision (Rule 0)
        subdivide_votes = [v for v in self.votes if v["verdict"] == VERDICT_SUBDIVIDE_IDEA]
        if subdivide_votes:
            result["outcome"] = "subdivide"
            result["summary"] = f"{len(subdivide_votes)} stage(s) voted SUBDIVIDE_IDEA."
            return result

        # Check for REJECTED — immediate rejection
        for v in self.votes:
            if v["verdict"] == VERDICT_REJECTED:
                result["outcome"] = "rejected"
                result["rejection_reasons"].append(
                    f"Stage '{v['stage']}': {v['justification']}"
                )
                return result

        # Check for majority NOT_SUITABLE
        not_suitable_count = sum(
            1 for v in self.votes if v["verdict"] == VERDICT_NOT_SUITABLE
        )
        if not_suitable_count >= len(self.votes) / 2:
            result["outcome"] = "rejected"
            for v in self.votes:
                if v["verdict"] == VERDICT_NOT_SUITABLE:
                    result["rejection_reasons"].append(
                        f"Stage '{v['stage']}': {v['justification']}"
                    )
            return result

        # Check for NEEDS_RESEARCH
        for v in self.votes:
            if v["verdict"] == VERDICT_NEEDS_RESEARCH:
                result["outcome"] = "needs_research"
                result["research_needed"].append(v["stage"])

        if result["outcome"] == "needs_research":
            return result

        # Check for tie between pass and fail verdicts
        pass_count = sum(
            1 for v in self.votes
            if v["verdict"] in (VERDICT_POSSIBLE, VERDICT_LIKELY)
        )
        fail_count = sum(
            1 for v in self.votes
            if v["verdict"] in (VERDICT_REJECTED, VERDICT_NOT_SUITABLE)
        )
        if pass_count == fail_count and pass_count > 0:
            result["outcome"] = "tie"
            return result

        return result


# ---------------------------------------------------------------------------
# Module-level convenience function
# ---------------------------------------------------------------------------

async def run_intake_pipeline(
    task_id: str,
    task_description: str,
    task_title: str,
    all_tasks: list[dict],
    budget_id: int | None = None,
    llm_id: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
) -> dict:
    """
    Convenience function to run the full intake pipeline.

    Creates an IntakePipeline instance and executes all stages,
    returning the aggregated tally result dict.

    Parameters
    ----------
    task_id : str
        The unique identifier of the task being evaluated.
    task_description : str
        The full description/body of the task.
    task_title : str
        The short title of the task.
    all_tasks : list[dict]
        All current tasks in the project (used for conflict detection).
    budget_id : int | None
        Optional LLM budget identifier for token tracking.
    llm_base_url : str | None
        Base URL for the LLM endpoint (e.g. ``http://localhost:8008/v1``).
        Falls back to the global ``LLM_BASE_URL`` config when *None*.
    llm_model : str | None
        Model identifier to send in the request payload.
        Falls back to the global ``LLM_MODEL`` config when *None*.

    Returns
    -------
    dict
        Tally result with keys: task_id, transition, votes, outcome,
        rejection_reasons, research_needed, total_prompt_tokens,
        total_completion_tokens.
    """
    pipeline = IntakePipeline(
        task_id=task_id,
        task_description=task_description,
        task_title=task_title,
        all_tasks=all_tasks,
        budget_id=budget_id,
        llm_id=llm_id,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
    )
    return await pipeline.run()
