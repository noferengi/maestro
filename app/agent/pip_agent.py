"""
PIP Agent — Performance Improvement Plan generator and pre-flight gate.

Responsibilities:
  - generate_pip()       — creates a PIP record after a task is demoted
  - run_pip_preflight()  — gates stage entry by verifying each PIP concurrently
  - _check_single_pip()  — single-PIP LLM check (called via asyncio.gather)
  - _get_git_diff_stat() — git diff --stat since the PIP was created

The old run_pip_verification_pipeline() (monolithic stage-level check) has been
removed.  Pre-flight is now per-PIP, per-stage, and runs before each review stage
rather than as a dedicated pipeline stage.
"""

import json
import logging
import asyncio
import subprocess
from typing import Any

from app.database import (
    get_task, get_pips_for_task, create_pip, create_pip_verification,
    get_project_path,
)
from app.agent.llm_client import call_llm, sanitize_user_content
from app.agent.project_snapshot import build_project_snapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

PIP_GENERATOR_PROMPT = """
You are the Maestro Quality Assurance Lead. A development task was just demoted from the {origin_stage} stage because it failed to meet the required standards.

TASK: {task_title}
REASON FOR DEMOTION:
{reason}

Your job is to generate a Performance Improvement Plan (PIP). A PIP is a concise list of specific, actionable technical requirements that MUST be satisfied before this task can be considered for review again.

Focus on:
1. Fixing the specific failures identified in the demotion reason.
2. Preventing regressions related to these failures.
3. Structural or quality improvements that were missing.

To complete your report, call the submit_work tool with:
submit_work(
  signal="ACCEPTED",
  summary="<one-line summary of the PIP>",
  payload={{
    "requirements": [
      "Ensure all error paths in the authentication controller log at level ERROR.",
      "Implement unit tests covering the edge case of empty JWT payloads.",
      "Refactor the session middleware to use a thread-safe connection pool."
    ]
  }}
)
"""

PIP_PREFLIGHT_PROMPT = """
You are the Maestro PIP Pre-flight Verifier.

STAGE BEING ATTEMPTED: {stage}
PIP REQUIREMENT (task was demoted from: {origin_stage}):
{requirements_as_bullets}

WORK DONE SINCE PIP WAS CREATED:
{git_diff_stat}

CURRENT PROJECT SNAPSHOT:
{snapshot}

Has this PIP requirement been meaningfully addressed in the code and/or documentation?
Be rigorous. A requirement is only satisfied if there is concrete evidence in the diff
or current snapshot — not just intent or comments.

To complete your verification, call the submit_work tool with:
submit_work(
  signal="ACCEPTED",
  summary="<one-sentence verdict>",
  payload={{
    "outcome": "passed" or "failed",
    "summary": "One sentence verdict.",
    "findings": [
      {{"requirement": "...", "status": "satisfied" or "missing", "detail": "..."}}
    ]
  }}
)
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_git_diff_stat(project_root: str | None, from_commit: str) -> str:
    """Return `git diff --stat` from the given commit to HEAD.

    Returns a human-readable fallback string on any error or missing context.
    """
    if not from_commit or from_commit == "none":
        return "No commit history to diff against."
    if not project_root:
        return "No project root configured — cannot compute diff."
    try:
        result = subprocess.run(
            ["git", "diff", f"{from_commit}..HEAD", "--stat"],
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        return "No changes since PIP was created."
    except Exception as exc:
        logger.debug("git diff stat failed: %s", exc)
        return "Unable to compute diff (git unavailable or error)."


# ---------------------------------------------------------------------------
# PIP Generator
# ---------------------------------------------------------------------------

async def generate_pip(
    task_id: str,
    origin_stage: str,
    reason: str,
    llm_id: int | None = None,
    budget_id: int | None = None,
):
    """Generate a new PIP for a task after demotion.

    Captures the current HEAD commit so that the pre-flight verifier can later
    diff the work done since the PIP was created.
    """
    task = get_task(task_id)
    if not task:
        logger.error("Cannot generate PIP: task %s not found.", task_id)
        return None

    # Capture git HEAD at PIP creation time
    project_path = get_project_path(task.project) if task.project else None
    created_at_commit = "none"
    if project_path:
        try:
            res = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=project_path,
                capture_output=True,
                text=True,
                timeout=10,
            )
            if res.returncode == 0:
                created_at_commit = res.stdout.strip()
        except Exception as exc:
            logger.debug("git rev-parse HEAD failed for task %s: %s", task_id, exc)

    prompt = PIP_GENERATOR_PROMPT.format(
        origin_stage=origin_stage,
        task_title=sanitize_user_content(task.title),
        reason=sanitize_user_content(reason),
    )

    from app.agent.tools import build_tool_schemas, dispatch_tool
    pip_tools = build_tool_schemas(["submit_work"])

    try:
        response = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            llm_id=llm_id or task.llm_id,
            budget_id=budget_id or task.budget_id,
            tools=pip_tools,
            tool_choice="auto",
        )
        stats = response.get("usage", {})

        assistant_msg = response.get("choices", [{}])[0].get("message", {})
        tool_calls = assistant_msg.get("tool_calls") or []
        
        data = None
        if tool_calls:
            for tc in tool_calls:
                tc_result = dispatch_tool(
                    tc["function"]["name"],
                    json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                )
                if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                    data = json.loads(tc_result).get("payload")
                    break
        
        if data is None:
            # Fallback
            from app.agent.llm_client import extract_text_response
            response_text = extract_text_response(response)
            from app.agent.json_utils import extract_json_block
            raw = extract_json_block(response_text) or response_text
            data = json.loads(raw)

        requirements = json.dumps(data.get("requirements", []))

        pip = create_pip(
            task_id=task_id,
            origin_stage=origin_stage,
            requirements=requirements,
            llm_id=llm_id or task.llm_id,
            budget_id=budget_id or task.budget_id,
            prompt_tokens=stats.get("prompt_tokens", 0),
            completion_tokens=stats.get("completion_tokens", 0),
            created_at_commit=created_at_commit,
        )
        logger.info(
            "Generated PIP for task %s with %d requirements (commit=%s).",
            task_id, len(data.get("requirements", [])), created_at_commit,
        )
        return pip
    except Exception:
        logger.exception("Failed to generate PIP for task %s.", task_id)
        return None


# ---------------------------------------------------------------------------
# Pre-flight gate
# ---------------------------------------------------------------------------

async def _check_single_pip(
    pip: Any,
    task: Any,
    stage: str,
    snapshot: str,
    llm_id: int,
    budget_id: int,
    project_root: str | None,
) -> dict:
    """Run the pre-flight LLM check for a single PIP. Called via asyncio.gather."""
    reqs = json.loads(pip.requirements) if isinstance(pip.requirements, str) else pip.requirements
    req_bullets = "\n".join(f"- {r}" for r in reqs)
    created_at_commit = getattr(pip, "created_at_commit", "none")
    diff_stat = _get_git_diff_stat(project_root, created_at_commit)

    prompt = PIP_PREFLIGHT_PROMPT.format(
        stage=stage,
        origin_stage=pip.origin_stage,
        requirements_as_bullets=sanitize_user_content(req_bullets),
        git_diff_stat=sanitize_user_content(diff_stat),
        snapshot=sanitize_user_content(snapshot),
    )

    from app.agent.tools import build_tool_schemas, dispatch_tool
    pip_tools = build_tool_schemas(["submit_work"])

    try:
        response = await call_llm(
            messages=[{"role": "user", "content": prompt}],
            llm_id=llm_id,
            budget_id=budget_id,
            tools=pip_tools,
            tool_choice="auto",
        )

        assistant_msg = response.get("choices", [{}])[0].get("message", {})
        tool_calls = assistant_msg.get("tool_calls") or []
        
        data = None
        if tool_calls:
            for tc in tool_calls:
                tc_result = dispatch_tool(
                    tc["function"]["name"],
                    json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                )
                if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                    data = json.loads(tc_result).get("payload")
                    break
        
        if data is None:
            # Fallback
            from app.agent.llm_client import extract_text_response
            response_text = extract_text_response(response)
            from app.agent.json_utils import extract_json_block
            raw = extract_json_block(response_text) or response_text
            data = json.loads(raw)
        return {
            "pip_id": pip.id,
            "outcome": data.get("outcome", "failed"),
            "summary": data.get("summary", ""),
            "findings": data.get("findings", []),
        }
    except Exception as exc:
        logger.exception("PIP pre-flight check failed for pip %d: %s", pip.id, exc)
        return {
            "pip_id": pip.id,
            "outcome": "failed",
            "summary": f"Pre-flight error: {exc}",
            "findings": [],
        }


async def run_pip_preflight(
    task_id: str,
    stage: str,
    llm_id: int,
    budget_id: int,
    project_root: str | None,
) -> dict:
    """Gate a stage transition by verifying all PIPs for the task concurrently.

    Returns:
        {
            "all_passed": bool,
            "results": [{"pip_id", "outcome", "summary", "findings"}, ...]
        }
    """
    task = get_task(task_id)
    if not task:
        return {"all_passed": True, "results": []}

    pips = get_pips_for_task(task_id)
    if not pips:
        return {"all_passed": True, "results": []}

    snapshot = build_project_snapshot(project_root)

    results = list(await asyncio.gather(*[
        _check_single_pip(pip, task, stage, snapshot, llm_id, budget_id, project_root)
        for pip in pips
    ]))

    # Persist verification rows
    for result in results:
        create_pip_verification(
            pip_id=result["pip_id"],
            task_id=task_id,
            stage=stage,
            outcome=result["outcome"],
            summary=result["summary"],
            findings=json.dumps(result["findings"]),
        )

    all_passed = all(r["outcome"] == "passed" for r in results)
    logger.info(
        "[pip_preflight] Task %s stage=%s: %d pip(s) checked, all_passed=%s.",
        task_id, stage, len(results), all_passed,
    )
    return {"all_passed": all_passed, "results": results}
