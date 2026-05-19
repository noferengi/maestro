"""
app/agent/goal_verifier.py
--------------------------
GoalVerifier — evaluates progress toward a MaestroGoal.

Flow per job:
  1. Load goal + linked pipeline cards (completed ones = evidence)
  2. Collect doc store evidence matching goal title slug
  3. Run per-criterion verifiers (sympy/pytest/llm_judge/manual)
  4. Single LLM judge call that synthesises everything into a structured verdict
  5. Update goal.progress, goal.last_verdict, goal.evidence
  6. Regenerate the linked arch card description
"""

from __future__ import annotations

import json
import logging
import subprocess
import asyncio
from datetime import datetime

logger = logging.getLogger(__name__)

_JUDGE_SYSTEM_PROMPT = """\
You are the Maestro Goal Evaluator. You assess progress toward a long-term project goal
by examining completed work, formal verification results, and accumulated evidence.
Be rigorous — partial progress should not be rated as complete. Be specific — name
which criteria are met and which are not.
"""

_JUDGE_USER_TEMPLATE = """\
## GOAL
{statement}

## CRITERIA ({n_criteria} total)
{criteria_block}

## COMPLETED LINKED CARDS
{cards_block}

## DOCUMENT STORE EVIDENCE
{docs_block}

## GIT ACTIVITY SINCE GOAL CREATION
{git_block}

Evaluate overall goal progress. Call submit_work with a JSON payload:
{{
  "progress": <float 0.0–1.0>,
  "verdict": "<ADVANCING|STALLED|COMPLETE>",
  "met_criteria": ["..."],
  "unmet_criteria": ["..."],
  "evidence_gaps": ["..."],
  "recommended_next_cards": ["..."]
}}

Rules:
- COMPLETE only if ALL criteria are verified met (progress = 1.0)
- STALLED if no criterion has advanced since last check (progress unchanged)
- ADVANCING otherwise
- recommended_next_cards: 1-3 short card title suggestions that would most advance the goal
"""


async def run_goal_verification(
    job_id: int,
    llm_id: int,
    budget_id: int,
) -> dict:
    """Main entry point called by the scheduler worker thread (inside asyncio loop)."""
    from app.database import (
        get_goal, update_goal, append_goal_evidence,
        update_goal_verification_job,
        get_tasks_by_project, get_project,
        list_documents_by_project,
    )

    goal = get_goal(job_id)  # Note: job_id here is goal_id passed from scheduler
    if not goal:
        raise RuntimeError(f"Goal {job_id} not found")

    project = get_project(goal.project_id)
    project_name = project.name if project else None

    # --- 1. Collect evidence ---
    linked_completed = _get_linked_completed_cards(goal.id, project_name)
    doc_evidence = _get_doc_evidence(project_name, goal.title)
    git_summary = _get_git_summary(project_name, goal.created_at)

    # --- 2. Run per-criterion verifiers ---
    criteria = goal.criteria or []
    criterion_results = []
    for c in criteria:
        result = await _verify_criterion(c, project_name)
        criterion_results.append(result)

    # --- 3. LLM judge call ---
    from app.agent.llm_client import call_llm
    from app.agent.tools import build_tool_schemas, dispatch_tool

    criteria_block = _format_criteria_block(criteria, criterion_results)
    cards_block = _format_cards_block(linked_completed)
    docs_block = _format_docs_block(doc_evidence)

    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": _JUDGE_USER_TEMPLATE.format(
            statement=goal.statement,
            n_criteria=len(criteria),
            criteria_block=criteria_block,
            cards_block=cards_block,
            docs_block=docs_block,
            git_block=git_summary,
        )},
    ]

    tool_schemas = build_tool_schemas(["submit_work"])
    resp = await call_llm(
        messages=messages,
        llm_id=llm_id,
        budget_id=budget_id,
        tools=tool_schemas,
        task_id=None,
        agent_name="GoalVerifier",
    )

    verdict_data = _extract_verdict(resp)

    # --- 4. Persist results ---
    now_ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    evidence_entry = (
        f"**Verification {now_ts}** — verdict: {verdict_data.get('verdict','?')} "
        f"({int(verdict_data.get('progress', 0) * 100)}%)\n"
        f"Met: {', '.join(verdict_data.get('met_criteria', [])) or 'none'}\n"
        f"Unmet: {', '.join(verdict_data.get('unmet_criteria', [])) or 'none'}"
    )
    append_goal_evidence(goal.id, evidence_entry)

    update_goal(
        goal.id,
        progress=verdict_data.get("progress", goal.progress),
        last_verdict=verdict_data,
        status="completed" if verdict_data.get("verdict") == "COMPLETE" else goal.status,
    )

    # --- 5. Regenerate arch card description ---
    if goal.arch_card_id:
        _update_arch_card(goal, verdict_data)

    return {
        "verdict": verdict_data,
        "prompt_tokens": resp.get("prompt_tokens", 0),
        "completion_tokens": resp.get("completion_tokens", 0),
    }


# ---------------------------------------------------------------------------
# Criterion verifiers
# ---------------------------------------------------------------------------

async def _verify_criterion(criterion: dict, project_name: "str | None") -> dict:
    vtype = criterion.get("verifier_type", "llm_judge")
    text = criterion.get("text", "")
    arg = criterion.get("verifier_arg", "")

    if vtype == "sympy":
        return _run_sympy_criterion(text, arg)
    elif vtype == "pytest":
        return _run_pytest_criterion(text, arg, project_name)
    elif vtype == "manual":
        return {"criterion": text, "status": "pending_human", "detail": "Requires manual check"}
    else:
        return {"criterion": text, "status": "llm_deferred", "detail": "Evaluated by LLM judge"}


def _run_sympy_criterion(text: str, code_or_path: str) -> dict:
    if not code_or_path:
        return {"criterion": text, "status": "skipped", "detail": "No verifier_arg provided"}
    try:
        if code_or_path.endswith(".py"):
            result = subprocess.run(
                ["python", code_or_path],
                capture_output=True, text=True, timeout=300,
            )
        else:
            result = subprocess.run(
                ["python", "-c", code_or_path],
                capture_output=True, text=True, timeout=300,
            )
        if result.returncode == 0:
            return {"criterion": text, "status": "passed", "detail": result.stdout[:500]}
        return {"criterion": text, "status": "failed", "detail": result.stderr[:500]}
    except subprocess.TimeoutExpired:
        return {"criterion": text, "status": "failed", "detail": "Timeout after 300s"}
    except Exception as exc:
        return {"criterion": text, "status": "error", "detail": str(exc)}


def _run_pytest_criterion(text: str, test_path: str, project_name: "str | None") -> dict:
    if not test_path:
        return {"criterion": text, "status": "skipped", "detail": "No test path provided"}
    try:
        from app.database import get_project_path
        project_root = get_project_path(project_name) if project_name else None
        cmd = ["python", "-m", "pytest", test_path, "-q", "--tb=short"]
        result = subprocess.run(
            cmd,
            capture_output=True, text=True, timeout=120,
            cwd=project_root,
        )
        if result.returncode == 0:
            return {"criterion": text, "status": "passed", "detail": result.stdout[:500]}
        return {"criterion": text, "status": "failed", "detail": (result.stdout + result.stderr)[:500]}
    except subprocess.TimeoutExpired:
        return {"criterion": text, "status": "failed", "detail": "Timeout after 120s"}
    except Exception as exc:
        return {"criterion": text, "status": "error", "detail": str(exc)}


# ---------------------------------------------------------------------------
# Evidence collectors
# ---------------------------------------------------------------------------

def _get_linked_completed_cards(goal_id: int, project_name: "str | None") -> list:
    from app.database import SessionLocal
    from app.database.models import Task
    db = SessionLocal()
    try:
        rows = (
            db.query(Task)
            .filter(
                Task.goal_id == goal_id,
                Task.is_active == True,
                Task.stage_key == "completed",
            )
            .order_by(Task.updated_at.desc())
            .limit(20)
            .all()
        )
        return [{"title": t.title, "stage": t.stage_key, "description": (t.description or "")[:300]} for t in rows]
    finally:
        db.close()


def _get_doc_evidence(project_name: "str | None", goal_title: str) -> list:
    if not project_name:
        return []
    try:
        from app.database import list_documents_by_project, fuzzy_get_document_by_project
        slug = goal_title.lower().replace(" ", "-")[:40]
        docs = fuzzy_get_document_by_project(project_name, slug, threshold=0.2)
        if docs:
            return [{"key": d["key"], "content": d["content"][:400]} for d in docs[:5]]
        return []
    except Exception:
        return []


def _get_git_summary(project_name: "str | None", since: "datetime | None") -> str:
    if not project_name or not since:
        return "(no git history available)"
    try:
        from app.database import get_project_path
        project_root = get_project_path(project_name)
        if not project_root:
            return "(project has no path)"
        since_str = since.strftime("%Y-%m-%dT%H:%M:%S")
        result = subprocess.run(
            ["git", "log", f"--since={since_str}", "--oneline", "--no-merges", "--max-count=30"],
            capture_output=True, text=True, timeout=10,
            cwd=project_root,
        )
        lines = result.stdout.strip()
        return lines[:1000] if lines else "(no commits since goal creation)"
    except Exception as exc:
        return f"(git error: {exc})"


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _format_criteria_block(criteria: list, results: list) -> str:
    if not criteria:
        return "(no formal criteria defined — LLM will assess based on goal statement)"
    lines = []
    for c, r in zip(criteria, results):
        status = r.get("status", "?")
        detail = r.get("detail", "")
        lines.append(f"- [{status.upper()}] {c.get('text', '')} | {detail[:200]}")
    return "\n".join(lines)


def _format_cards_block(cards: list) -> str:
    if not cards:
        return "(no completed linked cards)"
    return "\n".join(f"- {c['title']}: {c['description'][:150]}" for c in cards)


def _format_docs_block(docs: list) -> str:
    if not docs:
        return "(no matching document store entries)"
    return "\n".join(f"- [{d['key']}]: {d['content'][:200]}" for d in docs)


def _extract_verdict(resp: dict) -> dict:
    """Extract submit_work payload from LLM response."""
    tool_calls = resp.get("tool_calls") or []
    for tc in tool_calls:
        fn = tc.get("function", {})
        if fn.get("name") == "submit_work":
            try:
                args = fn.get("arguments", "{}")
                parsed = json.loads(args) if isinstance(args, str) else args
                payload = parsed.get("payload", parsed)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                pass

    # Fallback: try to parse content as JSON
    content = resp.get("content", "")
    if content:
        try:
            return json.loads(content)
        except Exception:
            pass

    return {"progress": 0.0, "verdict": "STALLED", "met_criteria": [], "unmet_criteria": [], "evidence_gaps": ["LLM response not parseable"]}


def _update_arch_card(goal, verdict: dict) -> None:
    """Rewrite the linked arch card description to reflect current goal state."""
    try:
        from app.database import update_task
        progress_pct = int(verdict.get("progress", 0) * 100)
        verdict_label = verdict.get("verdict", "UNKNOWN")
        n_met = len(verdict.get("met_criteria", []))
        n_total = n_met + len(verdict.get("unmet_criteria", []))
        description = (
            f"**Status:** {goal.status} | **Progress:** {progress_pct}% | **Verdict:** {verdict_label}\n\n"
            f"{goal.statement}\n\n"
            f"**Criteria:** {n_met}/{n_total} met"
        )
        if verdict.get("recommended_next_cards"):
            recs = verdict["recommended_next_cards"][:3]
            description += "\n\n**Suggested next cards:** " + ", ".join(f'"{r}"' for r in recs)
        update_task(goal.arch_card_id, description=description)
    except Exception as exc:
        logger.warning("[goal_verifier] Failed to update arch card for goal %d: %s", goal.id, exc)
