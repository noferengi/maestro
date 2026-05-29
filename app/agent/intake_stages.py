"""
app/agent/intake_stages.py
--------------------------
Intake pipeline: module-level async functions (no class wrapper).

Called by stage_executors.py (individual node executors) and by
run_intake_pipeline() (used in tests and legacy call-sites).

Public entry points for node executors:
  run_intake_scope_stage()
  run_intake_static_stage()
  run_intake_conflict_stage()
  run_intake_feasibility_stage()
  run_intake_gate()

Full-pipeline convenience entry (tests / legacy):
  run_intake_pipeline()
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
# Verdict constants
# ---------------------------------------------------------------------------

VERDICT_POSSIBLE = Verdict.POSSIBLE.value
VERDICT_LIKELY = Verdict.LIKELY.value
VERDICT_NOT_SUITABLE = Verdict.NOT_SUITABLE.value
VERDICT_REJECTED = Verdict.REJECTED.value
VERDICT_NEEDS_RESEARCH = Verdict.NEEDS_RESEARCH.value
VERDICT_SUBDIVIDE_IDEA = Verdict.SUBDIVIDE_IDEA.value

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
# Private helpers
# ---------------------------------------------------------------------------

def _get_stage_cfg(task_id: str) -> dict:
    try:
        from app.agent.pipeline_router import get_stage_config as _gsc
        _sc = _gsc(task_id)
        return (_sc.config or {}) if _sc else {}
    except Exception:
        return {}


async def _intake_call_llm_stage(
    system_prompt: str,
    user_prompt: str,
    *,
    task_id: str,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
) -> dict:
    """Single-call LLM helper that parses a submit_work payload. Returns dict with content, tokens, model."""
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    tool_schemas = build_tool_schemas(["submit_work"])
    data = await call_llm(
        messages,
        base_url=llm_base_url,
        model=llm_model,
        tools=tool_schemas,
        tool_choice="auto",
        task_id=task_id,
        llm_id=llm_id,
        budget_id=budget_id,
        agent_name=AGENT_NAME,
    )
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
        "model": data.get("model", llm_model),
    }


def _intake_extract_vote(stage: str, llm_result: dict, llm_model: str | None) -> dict:
    """Normalize an LLM result dict into a vote dict."""
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
        "model": llm_result.get("model", llm_model),
    }


def _intake_error_vote(stage: str, error: Exception, llm_model: str | None) -> dict:
    """Return a NEEDS_RESEARCH fallback vote, or raise PipelineAbortedError for infra errors."""
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
        "model": llm_model,
    }


# ---------------------------------------------------------------------------
# Standalone stage functions
# ---------------------------------------------------------------------------

async def _intake_scope_analysis(
    task_id: str,
    task_title: str,
    task_description: str,
    *,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
    stage_cfg: dict,
) -> dict:
    user_prompt = (
        f"Task ID: {task_id}\n"
        f"Task Title: {task_title}\n\n"
        f"Task Description:\n{task_description}\n\n"
        "Please analyze this task's scope and provide your assessment."
    )
    try:
        _scope_sys = stage_cfg.get("system_prompt") or _SCOPE_SYSTEM_PROMPT
        result = await _intake_call_llm_stage(
            _scope_sys, user_prompt,
            task_id=task_id, llm_base_url=llm_base_url, llm_model=llm_model,
            llm_id=llm_id, budget_id=budget_id,
        )
        return _intake_extract_vote("scope_analysis", result, llm_model)
    except Exception as exc:
        return _intake_error_vote("scope_analysis", exc, llm_model)


async def _intake_static_analysis(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    *,
    project: str,
    llm_model: str | None,
    project_root_override: str | None = None,
) -> dict:
    try:
        from app.agent.static_analysis import analyze_project, generate_vote

        if project_root_override:
            project_root = normalize_path(project_root_override)
        else:
            project_root = normalize_path(get_project_path(project))

        if not project_root:
            raise ValueError(
                f"Project '{project}' has no configured path. "
                "Static analysis cannot proceed. Add this project to the projects table with its filesystem path."
            )

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
                    "static_summary_override": msg,
                }

        if not is_git_repo(project_root):
            logger.info("Initializing Git repository for project '%s' at '%s'", project, project_root)
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
                    "static_summary_override": msg,
                }

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
            file_paths = list(set(os.path.normpath(p) for p in file_paths if os.path.isfile(p)))
        else:
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
            return {**_STATIC_SKIP, "justification": msg, "static_summary_override": msg}

        loop = asyncio.get_running_loop()
        analysis_result = await loop.run_in_executor(None, analyze_project, file_paths)
        vote_data = await loop.run_in_executor(None, generate_vote, analysis_result, task_description)

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
        logger.info("Static analysis module not available; returning default POSSIBLE vote.")
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
        return _intake_error_vote("static_analysis", exc, llm_model)


async def _intake_feasibility(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    static_vote: dict,
    *,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
    stage_cfg: dict,
) -> dict:
    static_summary = "No structural data available."
    if static_vote.get("static_summary_override"):
        static_summary = static_vote["static_summary_override"]
    elif static_vote.get("raw_response") is not None:
        try:
            static_summary = json.dumps(static_vote["raw_response"], indent=2, default=str)
        except (TypeError, ValueError):
            static_summary = str(static_vote["raw_response"])

    scope_summary = "No scope data available."
    if scope_vote.get("raw_response") is not None:
        try:
            scope_summary = json.dumps(scope_vote["raw_response"], indent=2, default=str)
        except (TypeError, ValueError):
            scope_summary = str(scope_vote["raw_response"])

    user_prompt = (
        f"Task ID: {task_id}\n"
        f"Task Title: {task_title}\n\n"
        f"Task Description:\n{task_description}\n\n"
        f"--- Scope Analysis (from Stage 1) ---\n{scope_summary}\n\n"
        f"--- Project Structure (from Static Analysis) ---\n{static_summary}\n\n"
        "Please assess the feasibility of this task."
    )
    try:
        system_prompt = stage_cfg.get("system_prompt") or _FEASIBILITY_SYSTEM_PROMPT
        result = await _intake_call_llm_stage(
            system_prompt, user_prompt,
            task_id=task_id, llm_base_url=llm_base_url, llm_model=llm_model,
            llm_id=llm_id, budget_id=budget_id,
        )
        return _intake_extract_vote("feasibility_analysis", result, llm_model)
    except Exception as exc:
        return _intake_error_vote("feasibility_analysis", exc, llm_model)


async def _intake_conflict_detection(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    all_tasks: list[dict],
    *,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
    stage_cfg: dict,
) -> dict:
    active_tasks = [t for t in all_tasks if t.get("type", "").lower() != "completed"]
    task_lines: list[str] = []
    for t in active_tasks:
        task_lines.append(
            f"- ID: {t.get('id', 'unknown')}, "
            f"Title: {t.get('title', 'untitled')}, "
            f"Type/Column: {t.get('type', 'unknown')}, "
            f"Description: {t.get('description', 'no description')[:200]}"
        )
    task_list_str = "\n".join(task_lines) if task_lines else "(no active tasks)"

    scope_summary = "No scope data available."
    if scope_vote.get("raw_response") is not None:
        try:
            scope_summary = json.dumps(scope_vote["raw_response"], indent=2, default=str)
        except (TypeError, ValueError):
            scope_summary = str(scope_vote["raw_response"])

    user_prompt = (
        f"PROPOSED TASK:\n"
        f"  ID: {task_id}\n"
        f"  Title: {task_title}\n"
        f"  Description: {task_description}\n\n"
        f"--- Scope Analysis ---\n{scope_summary}\n\n"
        f"--- Current Active Tasks ---\n{task_list_str}\n\n"
        "Please check for conflicts between the proposed task and existing tasks."
    )
    try:
        system_prompt = stage_cfg.get("system_prompt") or _CONFLICT_SYSTEM_PROMPT
        result = await _intake_call_llm_stage(
            system_prompt, user_prompt,
            task_id=task_id, llm_base_url=llm_base_url, llm_model=llm_model,
            llm_id=llm_id, budget_id=budget_id,
        )
        return _intake_extract_vote("conflict_detection", result, llm_model)
    except Exception as exc:
        return _intake_error_vote("conflict_detection", exc, llm_model)


def _intake_build_tally(task_id: str, votes: list[dict]) -> dict:
    """Aggregate votes into a final tally dict."""
    result: dict[str, Any] = {
        "task_id": task_id,
        "transition": "idea_to_planning",
        "votes": votes,
        "outcome": "passed",
        "rejection_reasons": [],
        "research_needed": [],
        "total_prompt_tokens": sum(v.get("prompt_tokens", 0) for v in votes),
        "total_completion_tokens": sum(v.get("completion_tokens", 0) for v in votes),
    }

    # Rule 0: SUBDIVIDE_IDEA requires majority of LLM stages (>=2 of 3).
    subdivide_votes = [v for v in votes if v["verdict"] == VERDICT_SUBDIVIDE_IDEA]
    llm_stage_count = sum(1 for v in votes if v["stage"] != "static_analysis")
    subdivide_threshold = max(2, (llm_stage_count // 2) + 1)
    if len(subdivide_votes) >= subdivide_threshold:
        result["outcome"] = "subdivide"
        result["summary"] = (
            f"{len(subdivide_votes)}/{llm_stage_count} LLM stages voted SUBDIVIDE_IDEA "
            f"(threshold: {subdivide_threshold})."
        )
        return result

    negative_votes = [v for v in votes if v["verdict"] in (VERDICT_REJECTED, VERDICT_NOT_SUITABLE)]
    majority_threshold = (len(votes) // 2) + 1
    if len(negative_votes) >= majority_threshold:
        result["outcome"] = "rejected"
        for v in negative_votes:
            result["rejection_reasons"].append(f"Stage '{v['stage']}': {v['justification']}")
        return result

    for v in votes:
        if v["verdict"] == VERDICT_NEEDS_RESEARCH:
            result["outcome"] = "needs_research"
            result["research_needed"].append(v["stage"])

    if result["outcome"] == "needs_research":
        return result

    pass_count = sum(1 for v in votes if v["verdict"] in (VERDICT_POSSIBLE, VERDICT_LIKELY))
    fail_count = sum(1 for v in votes if v["verdict"] in (VERDICT_REJECTED, VERDICT_NOT_SUITABLE))
    if pass_count == fail_count and pass_count > 0:
        result["outcome"] = "tie"
        return result

    return result


async def _intake_handle_needs_research(
    task_id: str,
    task_title: str,
    task_description: str,
    votes: list[dict],
    tally: dict,
    *,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
    project: str,
) -> tuple[list[dict], dict]:
    """Spawn research agents for NEEDS_RESEARCH stages, re-tally, and return (updated_votes, tally)."""
    from app.agent.research import run_research

    updated_votes = list(votes)
    for stage_name in tally.get("research_needed", []):
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        original_vote = next((v for v in updated_votes if v["stage"] == stage_name), None)
        context = {
            "task_id": task_id,
            "task_title": task_title,
            "task_description": task_description,
            "original_vote": original_vote,
            "stage": stage_name,
        }
        question = (
            f"Stage '{stage_name}' could not determine feasibility for task "
            f"'{task_title}'. Original justification: "
            f"{original_vote['justification'] if original_vote else 'unknown'}. "
            f"Investigate the codebase and determine if this task is feasible."
        )

        try:
            research_result = await run_research(
                question=question,
                context=context,
                llm_base_url=llm_base_url,
                llm_model=llm_model,
                task_id=task_id,
                llm_id=llm_id,
                budget_id=budget_id,
                project_root=get_project_path(project),
            )
            raw_verdict = research_result.vote.get("verdict", VERDICT_NOT_SUITABLE)
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
                "stage": stage_name,
                "verdict": effective_verdict,
                "confidence": research_result.vote.get("confidence", 55),
                "justification": effective_justification,
                "raw_response": research_result.vote,
                "prompt_tokens": research_result.prompt_tokens,
                "completion_tokens": research_result.completion_tokens,
                "model": "research_agent",
            }
            updated_votes = [v if v["stage"] != stage_name else research_vote for v in updated_votes]

        except Exception as exc:
            logger.error("Research agent for stage '%s' failed: %s", stage_name, exc)
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
            updated_votes = [v if v["stage"] != stage_name else fallback for v in updated_votes]

    return updated_votes, _intake_build_tally(task_id, updated_votes)


async def _intake_handle_tie(
    task_id: str,
    task_title: str,
    task_description: str,
    votes: list[dict],
    tally: dict,
    *,
    llm_base_url: str | None,
    llm_model: str | None,
    llm_id: int | None,
    budget_id: int | None,
) -> tuple[list[dict], dict]:
    """Spawn a tie-breaker research agent, return (updated_votes, tally)."""
    from app.agent.config import TIEBREAKER_ENABLED
    from app.agent.research import run_tiebreaker

    updated_votes = list(votes)
    if not TIEBREAKER_ENABLED:
        logger.info("Tie-breaker disabled; returning tie result as-is.")
        return updated_votes, tally

    logger.info("Vote tie detected for task '%s'; spawning tie-breaker agent.", task_id)
    try:
        tiebreaker_result = await run_tiebreaker(
            task_description=f"{task_title}: {task_description}",
            votes=updated_votes,
            llm_base_url=llm_base_url,
            llm_model=llm_model,
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
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
    except Exception as exc:
        logger.error("Tie-breaker agent failed: %s", exc)
        tiebreaker_vote = {
            "stage": "tiebreaker",
            "verdict": VERDICT_NOT_SUITABLE,
            "confidence": 55,
            "justification": f"Tie-breaker agent failed: {exc}. Defaulting to conservative NOT_SUITABLE.",
            "raw_response": None,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "model": "tiebreaker_agent",
        }

    updated_votes = updated_votes + [tiebreaker_vote]
    return updated_votes, _intake_build_tally(task_id, updated_votes)


async def _intake_handle_subdivide(tally: dict) -> dict:
    """Pass the subdivide tally through unchanged (actual subdivision is handled by stage_executors)."""
    logger.info("Subdivision triggered for task '%s'.", tally.get("task_id"))
    return tally


# ---------------------------------------------------------------------------
# Full-pipeline orchestration (used by tests and legacy call-sites;
# production scheduler calls individual node executors instead)
# ---------------------------------------------------------------------------

async def run_intake_pipeline(
    task_id: str,
    task_title: str,
    task_description: str,
    all_tasks: list[dict],
    *,
    budget_id: int | None = None,
    llm_id: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    project: str | None = None,
    stage_cfg: dict | None = None,
) -> dict:
    """Run all four intake stages plus the tally gate. Returns the final tally dict."""
    from app.agent.llm_client import set_llm_session_context
    set_llm_session_context(AGENT_NAME)
    if is_shutting_down():
        raise ShutdownError("Server is shutting down")

    _llm_base_url = llm_base_url or LLM_BASE_URL
    _llm_model = llm_model or LLM_MODEL
    _project = project or "TheMaestro"
    _stage_cfg = stage_cfg or _get_stage_cfg(task_id)

    scope_vote = await _intake_scope_analysis(
        task_id, task_title, task_description,
        llm_base_url=_llm_base_url, llm_model=_llm_model,
        llm_id=llm_id, budget_id=budget_id, stage_cfg=_stage_cfg,
    )
    votes: list[dict] = [scope_vote]

    static_vote, conflict_vote = await asyncio.gather(
        _intake_static_analysis(
            task_id, task_title, task_description, scope_vote,
            project=_project, llm_model=_llm_model,
        ),
        _intake_conflict_detection(
            task_id, task_title, task_description, scope_vote, all_tasks,
            llm_base_url=_llm_base_url, llm_model=_llm_model,
            llm_id=llm_id, budget_id=budget_id, stage_cfg=_stage_cfg,
        ),
    )
    votes.extend([static_vote, conflict_vote])

    feasibility_vote = await _intake_feasibility(
        task_id, task_title, task_description, scope_vote, static_vote,
        llm_base_url=_llm_base_url, llm_model=_llm_model,
        llm_id=llm_id, budget_id=budget_id, stage_cfg=_stage_cfg,
    )
    votes.append(feasibility_vote)

    tally = _intake_build_tally(task_id, votes)

    if tally["outcome"] == "needs_research":
        votes, tally = await _intake_handle_needs_research(
            task_id, task_title, task_description, votes, tally,
            llm_base_url=_llm_base_url, llm_model=_llm_model,
            llm_id=llm_id, budget_id=budget_id, project=_project,
        )
    if tally["outcome"] == "subdivide":
        tally = await _intake_handle_subdivide(tally)
    if tally["outcome"] == "tie":
        votes, tally = await _intake_handle_tie(
            task_id, task_title, task_description, votes, tally,
            llm_base_url=_llm_base_url, llm_model=_llm_model,
            llm_id=llm_id, budget_id=budget_id,
        )

    return tally


# ---------------------------------------------------------------------------
# Public entry points for individual node executors (stage_executors.py)
# ---------------------------------------------------------------------------

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
    return await _intake_scope_analysis(
        task_id, task_title, task_description,
        llm_base_url=llm_base_url or LLM_BASE_URL,
        llm_model=llm_model or LLM_MODEL,
        llm_id=llm_id,
        budget_id=budget_id,
        stage_cfg=stage_cfg or _get_stage_cfg(task_id),
    )


async def run_intake_static_stage(
    task_id: str,
    task_title: str,
    task_description: str,
    scope_vote: dict,
    project: str | None = None,
    project_path: str | None = None,
) -> dict:
    """Run only the static analysis stage and return its vote dict."""
    return await _intake_static_analysis(
        task_id, task_title, task_description, scope_vote,
        project=project or "TheMaestro",
        llm_model=LLM_MODEL,
        project_root_override=project_path,
    )


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
    return await _intake_conflict_detection(
        task_id, task_title, task_description, scope_vote, all_tasks,
        llm_base_url=llm_base_url or LLM_BASE_URL,
        llm_model=llm_model or LLM_MODEL,
        llm_id=llm_id,
        budget_id=budget_id,
        stage_cfg=stage_cfg or _get_stage_cfg(task_id),
    )


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
    return await _intake_feasibility(
        task_id, task_title, task_description, scope_vote, static_vote,
        llm_base_url=llm_base_url or LLM_BASE_URL,
        llm_model=llm_model or LLM_MODEL,
        llm_id=llm_id,
        budget_id=budget_id,
        stage_cfg=stage_cfg or _get_stage_cfg(task_id),
    )


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

    Returns the final tally dict.
    """
    _llm_base_url = llm_base_url or LLM_BASE_URL
    _llm_model = llm_model or LLM_MODEL
    _project = project or "TheMaestro"

    tally = _intake_build_tally(task_id, votes)

    if tally["outcome"] == "needs_research":
        votes, tally = await _intake_handle_needs_research(
            task_id, task_title, task_description, votes, tally,
            llm_base_url=_llm_base_url, llm_model=_llm_model,
            llm_id=llm_id, budget_id=budget_id, project=_project,
        )
    if tally["outcome"] == "subdivide":
        tally = await _intake_handle_subdivide(tally)
    if tally["outcome"] == "tie":
        votes, tally = await _intake_handle_tie(
            task_id, task_title, task_description, votes, tally,
            llm_base_url=_llm_base_url, llm_model=_llm_model,
            llm_id=llm_id, budget_id=budget_id,
        )

    return tally
