"""
app/agent/_intake_pipeline.py
------------------------------
Intake pipeline implementation: IntakePipeline class and per-stage wrapper functions.

Called exclusively by the decomposed intake stage executors in stage_executors.py.
Internal module — import from here, not from the deleted intake.py.

When a task is requested to move from IDEA to PLANNING, this module coordinates
four analysis stages to determine whether the transition should proceed:

  1. Scope Analysis (LLM) - determines task scope, complexity, and decomposition.
  2a. Static Analysis (deterministic) - tree-sitter code structure analysis.
  2b. Feasibility Analysis (LLM) - informed by stage 2a output.
  3. Conflict Detection (LLM) - checks against existing tasks for overlaps.

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
import os
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError, PipelineAbortedError
from app.database import get_project_path
from app.agent.verdicts import Verdict
from app.agent.tools import build_tool_schemas, dispatch_tool
from app.utils import normalize_path

logger = logging.getLogger(__name__)
AGENT_NAME = "Intake Pipeline"

# ---------------------------------------------------------------------------
# Verdict constants - derived from the canonical Verdict enum so that any
# future rename in verdicts.py propagates here automatically.
# ---------------------------------------------------------------------------

VERDICT_POSSIBLE = Verdict.POSSIBLE.value
VERDICT_LIKELY = Verdict.LIKELY.value
VERDICT_NOT_SUITABLE = Verdict.NOT_SUITABLE.value
VERDICT_REJECTED = Verdict.REJECTED.value
VERDICT_NEEDS_RESEARCH = Verdict.NEEDS_RESEARCH.value
VERDICT_SUBDIVIDE_IDEA = Verdict.SUBDIVIDE_IDEA.value

# All valid verdict strings (excludes CONDITIONAL_PASS which intake doesn't emit)
_VALID_VERDICTS = {v.value for v in Verdict} - {Verdict.CONDITIONAL_PASS.value}

# ---------------------------------------------------------------------------
# Stage system prompts
# ---------------------------------------------------------------------------

_SCOPE_SYSTEM_PROMPT = """\
You are a senior analyst performing scope analysis on a proposed task.

Analyze the task and determine:
- Overall scope (small / medium / large / epic)
- Complexity rating (1-10)
- Whether the task should be decomposed into subtasks
- Key areas of the project or system likely affected by this task
- Estimated effort category (trivial, minor, moderate, significant, major)

To complete your analysis, call the submit_work tool with:
payload={
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
- NOT_SUITABLE: Task is poorly scoped, too large without decomposition, or fundamentally
  malformed (not just ambitious or greenfield).
- REJECTED: Reserve for tasks that are LOGICALLY IMPOSSIBLE, HARMFUL, or illegal.
  An empty project, missing infrastructure, or ambitious scope are NOT grounds for REJECTED.
  This verdict should be extremely rare — default to POSSIBLE or NEEDS_RESEARCH when uncertain.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large to implement in a single context window. Should be decomposed into smaller pieces. Only use when the task is good but genuinely too big - not vague (NEEDS_RESEARCH) or bad (REJECTED).

No prose after calling submit_work.\
"""

_PLATFORM_CAPABILITIES = """\
## Maestro Platform Capabilities

You are evaluating a task that will be executed by Maestro, an agentic workflow platform.
Before assessing feasibility, understand what Maestro CAN do — the absence of existing code
in the project directory is NEVER a reason to reject a task.

### Available tools (accessible to implementation agents):
- **Formal proof / mathematics**: `run_lean4` (Lean4 + Mathlib4 sandbox on a remote Docker host,
  pre-built image with Lean 4.29.1 + SymPy 1.14), `run_sympy` (Python SymPy), `search_mathlib`,
  `search_oeis`, `search_arxiv`, `list_mathlib_topics`
- **Code execution & testing**: `run_pytest`, `run_mypy`, `run_ruff`, `run_black_check`,
  `run_tsc`, `run_cargo_build`, `run_go_build`, `run_npm_build`
- **Research & search**: `web_search` (Brave/Tavily), `web_fetch`
- **File operations**: `read_file`, `write_file`, `search_files`, `find_files`, `list_directory`,
  `git_log`, `git_blame`, `git_add`, `git_restore`
- **Agent coordination**: `get_task`, `list_tasks`, `create_subtasks`, `consult_maestro`
- **Security**: `run_bandit`, `run_pip_audit`, `run_semgrep`

### Pipeline templates (the platform routes tasks through these automatically):
- **Mathematics / Proof Exploration** — 11 stages: exploration → Lean4 formalization → verification
- **Software Development** — INDEV → conceptual review → optimization → security → final review
- **Research Report** — research → synthesis → review
- **Data Analysis**, **Novel Writing**, **Bug Triage**, **Overnight Story Factory**

### Subdivision: oversized tasks are automatically decomposed into subtasks that run in parallel.

### Key principle: GREENFIELD IS THE NORMAL STARTING STATE.
Maestro is designed to BUILD things from scratch. An empty project directory means the
implementation agent will create all necessary files, structure, and infrastructure.
"No existing code" is never a reason to reject — it is the default starting condition.
The feasibility question is: "Can Maestro's tools and pipeline execute this work?" — not
"Does this infrastructure already exist?"
"""

_FEASIBILITY_SYSTEM_PROMPT = """\
You are an expert analyst performing feasibility analysis on a proposed task that will be
executed by the Maestro agentic platform.

""" + _PLATFORM_CAPABILITIES + """

You will receive:
1. The task description and title.
2. A structural analysis of the current project (file counts, languages or formats, component structure).

Your job is to assess:
- Whether Maestro's tools and pipeline can execute this task (NOT whether the code already exists).
- What ambiguities or unknowns exist that could block completion.
- Whether any external dependencies, APIs, or resources are unavailable to the platform.
- What risks or edge cases should be considered.

To complete your analysis, call the submit_work tool with:
payload={
  "feasibility_rating": <float 0.0-1.0>,
  "ambiguities": [<string>, ...],
  "external_dependencies": [<string>, ...],
  "risks": [<string>, ...],
  "project_readiness": "ready" | "needs_preparation" | "incompatible",
  "vote": {
    "verdict": "POSSIBLE" | "LIKELY" | "NOT_SUITABLE" | "REJECTED" | "NEEDS_RESEARCH" | "SUBDIVIDE_IDEA",
    "confidence": <float 0.0-1.0>,
    "justification": "<one-paragraph explanation>"
  }
}

Verdict guidelines:
- LIKELY: The platform has the tools to execute this; no fundamental blockers.
- POSSIBLE: Feasible but some preparation or unknowns to resolve during execution.
- NEEDS_RESEARCH: Cannot assess feasibility — key facts about the domain or environment are unknown.
- NOT_SUITABLE: The task is logically malformed, self-contradictory, or asks for something
  Maestro cannot meaningfully do (e.g. "deploy to production", "send a real email").
  Do NOT use this because existing code is absent — that is expected.
- REJECTED: Reserve for tasks that are LOGICALLY IMPOSSIBLE (mathematical contradiction),
  HARMFUL (destructive, illegal), or completely outside any agent's capability regardless
  of project state. This verdict should be extremely rare. Missing infrastructure, absent
  files, or an empty project directory never justify REJECTED.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large for a single context window.

No prose after calling submit_work.\
"""

_CONFLICT_SYSTEM_PROMPT = """\
You are a project coordinator performing conflict detection on a proposed task.

You will receive:
1. The proposed task description, title, and scope analysis.
2. A list of all current non-completed tasks in the project.

Your job is to detect:
- Artifact conflicts: tasks that are likely to modify the same files, documents, or outputs.
- Semantic conflicts: tasks with overlapping or contradictory goals.
- Priority conflicts: tasks that should be done first as prerequisites.
- Resource conflicts: tasks that compete for the same limited resources.

To complete your detection, call the submit_work tool with:
payload={
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
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large to implement in a single context window. Should be decomposed into smaller pieces. Only use when the task is good but genuinely too big - not vague (NEEDS_RESEARCH) or bad (REJECTED).

No prose after calling submit_work.\
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
        project: str | None = None,
    ) -> None:
        self.task_id = task_id
        self.task_description = task_description
        self.task_title = task_title
        self.all_tasks = all_tasks
        self.budget_id = budget_id
        self.llm_id = llm_id
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.project = project or "TheMaestro"
        self.votes: list[dict] = []  # Collect votes from each stage
        self._stage_cfg: dict = {}
        try:
            from app.agent.pipeline_router import get_stage_config as _gsc
            _sc = _gsc(task_id)
            self._stage_cfg = (_sc.config or {}) if _sc else {}
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Execute the full pipeline. Returns tally result dict."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        # Stage 1: Scope Analysis
        scope_vote = await self._stage_scope_analysis()
        self.votes.append(scope_vote)

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

        # Handle NEEDS_RESEARCH - spawn research agents for those stages
        if tally["outcome"] == "needs_research":
            tally = await self._handle_needs_research(tally)

        # Handle SUBDIVIDE - delegate to subdivision agent
        if tally["outcome"] == "subdivide":
            tally = await self._handle_subdivide(tally)

        # Handle TIE - spawn tie-breaker research agent
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
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

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
                    project_root=get_project_path(self.project),
                )
                raw_verdict = research_result.vote.get("verdict", VERDICT_NOT_SUITABLE)
                # TOO_LARGE means the task overflowed the research agent's context window -
                # treat it as SUBDIVIDE_IDEA so the pipeline routes to subdivision.
                if raw_verdict == "TOO_LARGE":
                    logger.info(
                        "Research agent returned TOO_LARGE for stage '%s' - routing to subdivision",
                        stage_name,
                    )
                    effective_verdict = VERDICT_SUBDIVIDE_IDEA
                    effective_justification = (
                        f"Task scope exceeded research agent context window: "
                        f"{research_result.vote.get('justification', '')}"
                    )
                else:
                    effective_verdict = raw_verdict
                    effective_justification = research_result.vote.get("justification", "Research completed.")

                research_vote = {
                    "stage": f"{stage_name}_research",
                    "verdict": effective_verdict,
                    "confidence": research_result.vote.get("confidence", 55),
                    "justification": effective_justification,
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
        Make a structured LLM call via submit_work and return the parsed response.

        Returns a dict with keys:
          - content: parsed JSON object from the submit_work payload
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

        tool_schemas = build_tool_schemas(["submit_work"])

        # Intake stages are single-call (no loop) for performance, but use tool-calling
        # to signal completion.
        data = await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            tools=tool_schemas,
            tool_choice="auto",
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            agent_name=AGENT_NAME,
        )

        # Extract usage stats
        usage = data.get("usage", {})
        assistant_msg = data.get("choices", [{}])[0].get("message", {})
        tool_calls = assistant_msg.get("tool_calls") or []
        
        parsed_content = None
        if tool_calls:
            for tc in tool_calls:
                tc_result = dispatch_tool(
                    tc["function"]["name"],
                    json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                )
                if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                    parsed_content = json.loads(tc_result).get("payload")
                    break
        
        if parsed_content is None:
            # Fallback to content parsing if tool call was missed
            raw_content = assistant_msg.get("content", "{}")
            cleaned = raw_content.strip()
            if cleaned.startswith("```"):
                lines = cleaned.split("\n")
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].strip() == "```":
                    lines = lines[:-1]
                cleaned = "\n".join(lines)
            try:
                parsed_content, _ = json.JSONDecoder().raw_decode(cleaned.lstrip())
            except (json.JSONDecodeError, ValueError):
                parsed_content = {}

        return {
            "content": parsed_content,
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
        Build a fallback NEEDS_RESEARCH vote when a stage fails with an
        application error (bad JSON, schema mismatch, logic error).

        For infrastructure errors (server down/overloaded, shutdown), raises
        PipelineAbortedError instead — the card stays in its current stage and
        the scheduler will re-dispatch when the endpoint recovers.
        """
        import httpx
        if isinstance(error, (
            httpx.ConnectError, httpx.ConnectTimeout,
            httpx.ReadTimeout, ShutdownError,
        )) or (isinstance(error, httpx.HTTPStatusError)
               and error.response.status_code >= 500):
            raise PipelineAbortedError(stage, error)

        logger.error("Stage '%s' failed with application error: %s", stage, error)
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
            _scope_sys = self._stage_cfg.get("system_prompt") or _SCOPE_SYSTEM_PROMPT
            result = await self._call_llm(_scope_sys, user_prompt)
            return self._extract_vote("scope_analysis", result)
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

            # Get the project root for this task - MUST be configured
            project_root = normalize_path(get_project_path(self.project))
            if not project_root:
                raise ValueError(
                    f"Project '{self.project}' has no configured path. "
                    "Static analysis cannot proceed. Add this project to the projects table with its filesystem path."
                )

            # Differentiate empty-project scenarios so the feasibility LLM gets
            # a meaningful signal instead of silently seeing "0 files parsed".
            _STATIC_SKIP = {
                "stage": "static_analysis",
                "verdict": VERDICT_POSSIBLE,
                "confidence": 0.3,
                "raw_response": None,
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "model": "static_analysis",
            }
            from app.agent.worktree import is_git_repo, ensure_project_ready
            
            # Ensure the project directory exists
            if not os.path.exists(project_root):
                try:
                    os.makedirs(project_root, exist_ok=True)
                    logger.info("Created missing project directory: %s", project_root)
                except OSError as exc:
                    msg = f"ERROR: Could not create project directory '{project_root}': {exc}"
                    return {
                        "stage": "static_analysis",
                        "verdict": VERDICT_REJECTED,
                        "confidence": 1.0,
                        "justification": msg,
                        "raw_response": None,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "model": "static_analysis",
                        "static_summary_override": msg
                    }

            # Automatically bootstrap Git if needed
            if not is_git_repo(project_root):
                logger.info("Initializing Git repository for project '%s' at '%s'", self.project, project_root)
                if not ensure_project_ready(project_root):
                    msg = (
                        f"ERROR: Failed to initialize Git repository at '{project_root}'. "
                        "Maestro requires a valid Git repository to manage task branches. "
                        "Check filesystem permissions and Git installation."
                    )
                    return {
                        "stage": "static_analysis",
                        "verdict": VERDICT_REJECTED,
                        "confidence": 1.0,
                        "justification": msg,
                        "raw_response": None,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "model": "static_analysis",
                        "static_summary_override": msg
                    }
                else:
                    # Success - static analysis can proceed (even if 0 files)
                    pass

            # Collect Python files from affected areas in scope analysis
            # and fall back to analyzing all Python files in the project
            from app.agent.path_filter import walk_safe
            raw_scope = scope_vote.get("raw_response") or {}
            affected_areas = raw_scope.get("affected_areas", [])
            if isinstance(affected_areas, list) and len(affected_areas) > 0:
                file_paths = []
                for area in affected_areas:
                    area_path = os.path.join(project_root, area.lstrip("/"))
                    if os.path.isdir(area_path):
                        for root, dirs, files in walk_safe(area_path):
                            for f in files:
                                if f.endswith(".py"):
                                    file_paths.append(os.path.join(root, f))
                    elif os.path.isfile(area_path) and area_path.endswith(".py"):
                        file_paths.append(area_path)

                # Deduplicate and normalize paths
                file_paths = list(set(os.path.normpath(p) for p in file_paths if os.path.isfile(p)))
            else:
                # Fall back: analyze all Python files in the project
                file_paths = []
                for root, dirs, files in walk_safe(project_root):
                    for f in files:
                        if f.endswith(".py"):
                            file_paths.append(os.path.join(root, f))
                file_paths = list(set(os.path.normpath(p) for p in file_paths if os.path.isfile(p)))

            if not file_paths:
                msg = (
                    f"NOTE: The project directory '{project_root}' exists but contains no Python "
                    "(.py) files. The project may use a different language (e.g. Kotlin, Java, JS). "
                    "Static analysis produced no output. Assess feasibility from the task description alone."
                )
                return {**_STATIC_SKIP,
                        "justification": msg,
                        "static_summary_override": msg}

            loop = asyncio.get_running_loop()
            # Run CPU-bound analysis in a thread executor
            analysis_result = await loop.run_in_executor(None, analyze_project, file_paths)
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
                                 "Defaulting to POSSIBLE - no structural objections raised.",
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
        # Build a summary of the static analysis for the LLM.
        # static_summary_override is set when the project dir is missing or has no .py files —
        # use it verbatim so the LLM sees a clear explanation instead of "0 files parsed".
        static_summary = "No structural data available."
        if static_vote.get("static_summary_override"):
            static_summary = static_vote["static_summary_override"]
        elif static_vote.get("raw_response") is not None:
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
            f"--- Project Structure (from Static Analysis) ---\n{static_summary}\n\n"
            f"Please assess the feasibility of this task."
        )

        try:
            system_prompt = self._stage_cfg.get("system_prompt") or _FEASIBILITY_SYSTEM_PROMPT
            result = await self._call_llm(system_prompt, user_prompt)
            return self._extract_vote("feasibility_analysis", result)
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
            system_prompt = self._stage_cfg.get("system_prompt") or _CONFLICT_SYSTEM_PROMPT
            result = await self._call_llm(system_prompt, user_prompt)
            return self._extract_vote("conflict_detection", result)
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

        # Rule 0: SUBDIVIDE_IDEA requires majority of LLM stages (>=2 of 3).
        # Static analysis never emits SUBDIVIDE_IDEA, so only LLM stage votes count.
        subdivide_votes = [v for v in self.votes if v["verdict"] == VERDICT_SUBDIVIDE_IDEA]
        llm_stage_count = sum(1 for v in self.votes if v["stage"] != "static_analysis")
        subdivide_threshold = max(2, (llm_stage_count // 2) + 1)
        if len(subdivide_votes) >= subdivide_threshold:
            result["outcome"] = "subdivide"
            result["summary"] = (
                f"{len(subdivide_votes)}/{llm_stage_count} LLM stages voted SUBDIVIDE_IDEA "
                f"(threshold: {subdivide_threshold})."
            )
            return result

        # Rejection requires a MAJORITY of votes to be negative (REJECTED or NOT_SUITABLE
        # combined). A single REJECTED vote no longer short-circuits — it is treated as a
        # strong negative signal but not a veto. This prevents one over-cautious stage from
        # blocking a task that three other stages consider feasible.
        negative_votes = [
            v for v in self.votes
            if v["verdict"] in (VERDICT_REJECTED, VERDICT_NOT_SUITABLE)
        ]
        majority_threshold = (len(self.votes) // 2) + 1
        if len(negative_votes) >= majority_threshold:
            result["outcome"] = "rejected"
            for v in negative_votes:
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
# Per-stage standalone functions (used by decomposed stage executors)
# ---------------------------------------------------------------------------

def _make_pipeline(
    task_id: str,
    task_title: str,
    task_description: str,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
    project: str | None,
    all_tasks: list[dict] | None = None,
    stage_cfg: dict | None = None,
) -> "IntakePipeline":
    p = IntakePipeline(
        task_id=task_id,
        task_description=task_description,
        task_title=task_title,
        all_tasks=all_tasks or [],
        budget_id=budget_id,
        llm_id=llm_id,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        project=project,
    )
    if stage_cfg:
        p._stage_cfg = stage_cfg
    return p


async def run_intake_scope_stage(
    task_id: str,
    task_title: str,
    task_description: str,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project: str | None = None,
    stage_cfg: dict | None = None,
) -> dict:
    """Run only the scope analysis stage and return its vote dict."""
    p = _make_pipeline(task_id, task_title, task_description,
                       llm_base_url, llm_model, llm_id, budget_id, project,
                       stage_cfg=stage_cfg)
    return await p._stage_scope_analysis()


async def run_intake_static_stage(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    project: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Run only the static analysis stage and return its vote dict."""
    p = _make_pipeline(task_id, task_title, task_description,
                       None, None, None, None, project)
    if project_path:
        import app.database as _db
        _orig = _db.get_project_path
        try:
            _db.get_project_path = lambda _: project_path
            return await p._stage_static_analysis(scope_vote)
        finally:
            _db.get_project_path = _orig
    return await p._stage_static_analysis(scope_vote)


async def run_intake_conflict_stage(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    all_tasks: list[dict],
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project: str | None = None,
    stage_cfg: dict | None = None,
) -> dict:
    """Run only the conflict detection stage and return its vote dict."""
    p = _make_pipeline(task_id, task_title, task_description,
                       llm_base_url, llm_model, llm_id, budget_id, project,
                       all_tasks=all_tasks, stage_cfg=stage_cfg)
    return await p._stage_conflict_detection(scope_vote)


async def run_intake_feasibility_stage(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    static_vote: dict,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project: str | None = None,
    stage_cfg: dict | None = None,
) -> dict:
    """Run only the feasibility analysis stage and return its vote dict."""
    p = _make_pipeline(task_id, task_title, task_description,
                       llm_base_url, llm_model, llm_id, budget_id, project,
                       stage_cfg=stage_cfg)
    return await p._stage_feasibility(scope_vote, static_vote)


async def run_intake_gate(
    task_id: str,
    task_title: str,
    task_description: str,
    votes: list[dict],
    all_tasks: list[dict],
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project: str | None = None,
) -> dict:
    """Tally all intake votes and run post-tally handlers (research/subdivide/tie).

    Returns the final tally dict (same structure as run_intake_pipeline).
    """
    p = _make_pipeline(task_id, task_title, task_description,
                       llm_base_url, llm_model, llm_id, budget_id, project,
                       all_tasks=all_tasks)
    p.votes = votes
    tally = p._build_tally()

    if tally["outcome"] == "needs_research":
        tally = await p._handle_needs_research(tally)
    if tally["outcome"] == "subdivide":
        tally = await p._handle_subdivide(tally)
    if tally["outcome"] == "tie":
        tally = await p._handle_tie(tally)

    return tally
