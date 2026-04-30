"""
app/agent/full_review.py
-------------------------
Full/Final Review Pipeline - 4-agent final judgment.

4 Parallel Reviewer Agents (3 if no frontend changes):
  - Functional: requirements traceability, missing features, scope creep
  - Code Quality: run pytest + linting, code style, dead code
  - Integration: import graph, API breaks, migration validity
  - UX: accessibility, responsive design (only if app/web/ files changed)

Standard majority-based tally. Research agent available.
Includes allowlist-only shell for test runners.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    FULL_REVIEW_AUTO_UX,
    FULL_REVIEW_FRONTEND_PATTERNS,
    FULL_REVIEW_MAX_REVIEWER_TURNS,
    FULL_REVIEW_CODE_QUALITY_TOOLS,
    FULL_REVIEW_FUNCTIONAL_TOOLS,
    PROJECT_ROOT,
    SHELL_TIMEOUT_SECONDS,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.tools import _task_git_cwd, dispatch_tool, build_tool_schemas
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.verdicts import Vote, Verdict, tally_votes

logger = logging.getLogger(__name__)
AGENT_NAME = "Full Review Pipeline"


# ---------------------------------------------------------------------------
# Allowlisted test runner shell
# ---------------------------------------------------------------------------

REVIEW_SHELL_ALLOWLIST = [
    r"^python\s+-m\s+pytest\b",
    r"^python\s+-m\s+ruff\b",
    r"^python\s+-m\s+mypy\b",
    r"^python\s+-m\s+black\s+--check\b",
    r"^npm\s+test\b",
    r"^npm\s+run\s+lint\b",
]

_REVIEW_ALLOWLIST_RE = [re.compile(p) for p in REVIEW_SHELL_ALLOWLIST]


def run_shell_review(command: str, *, project_path: str | None = None, timeout: int | None = None) -> str:
    """Execute a shell command from the review runner allowlist only."""
    import subprocess

    command = command.strip()
    allowed = any(pat.match(command) for pat in _REVIEW_ALLOWLIST_RE)
    if not allowed:
        return (
            f"ERROR: Command not in review runner allowlist. "
            f"Allowed patterns: {', '.join(REVIEW_SHELL_ALLOWLIST)}"
        )

    effective_timeout = timeout if timeout is not None else SHELL_TIMEOUT_SECONDS

    try:
        cwd = project_path or _task_git_cwd.get() or PROJECT_ROOT
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=effective_timeout,
            cwd=cwd,
        )
        output = result.stdout + result.stderr
        return output[:8000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return (
            f"ERROR: Command timed out after {effective_timeout}s. "
            "This may indicate a hang or high computational complexity."
        )
    except Exception as e:
        return f"ERROR: {e}"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FullReviewPipelineResult:
    task_id: str
    outcome: str  # "passed" | "rejected"
    votes: list[Vote] = field(default_factory=list)
    demotion_target: str | None = None
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class FullReviewPipeline:
    """4-agent final review pipeline."""

    def _get_reviewer_schemas(self, reviewer_type: str) -> list[dict]:
        """Return tool schemas appropriate for the reviewer type."""
        if reviewer_type in ("code_quality", "integration"):
            return build_tool_schemas(FULL_REVIEW_CODE_QUALITY_TOOLS)
        return build_tool_schemas(FULL_REVIEW_FUNCTIONAL_TOOLS)

    def __init__(
        self,
        task_id: str,
        task_description: str,
        files_changed: list[str] | None = None,
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        project_path: str | None = None,
        max_context: int = 0,
    ):
        self.task_id = task_id
        self.task_description = task_description
        self.files_changed = files_changed or []
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.project_path = project_path
        self.max_context = max_context
        self._total_prompt = 0
        self._total_completion = 0

    async def run(self) -> FullReviewPipelineResult:
        """Run all review agents in parallel."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        logger.info(f"[{AGENT_NAME}] Starting for task '%s'", self.task_id)

        reviewers = [
            {
                "type": "functional",
                "focus": (
                    "Requirements traceability: does implementation match the IDEA card? "
                    "Missing features? Scope creep? Edge cases?"
                ),
                "demotion_target": "planning",
            },
            {
                "type": "code_quality",
                "focus": (
                    "Code quality: test results, code style, error handling, test coverage, "
                    "dead code, naming conventions, magic values."
                ),
                "demotion_target": "indev",
            },
            {
                "type": "integration",
                "focus": (
                    "Integration: import graph cycles, API signature breaks, migration validity, "
                    "cross-feature interactions, full test suite on merge simulation."
                ),
                "demotion_target": "indev",
            },
        ]

        # Add UX reviewer if frontend files changed
        if self._has_frontend_changes():
            reviewers.append({
                "type": "ux",
                "focus": (
                    "UX review: accessibility, responsive design, visual consistency, "
                    "error state UX, loading states."
                ),
                "demotion_target": "indev",
            })

        tasks = [self._run_reviewer(r) for r in reviewers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        votes: list[Vote] = []
        for i, result in enumerate(results):
            reviewer_type = reviewers[i]["type"]
            if isinstance(result, Exception):
                logger.warning(f"[{AGENT_NAME}] Reviewer '%s' failed: %s", reviewer_type, result)
                votes.append(Vote(
                    stage=f"review_{reviewer_type}",
                    verdict=Verdict.NEEDS_RESEARCH,
                    confidence=65,
                    justification=f"Reviewer failed: {result}",
                ))
            else:
                votes.append(result)
                self._store_reviewer_result(result, reviewer_type)

        # Standard tally
        tally = tally_votes(votes)

        if tally.outcome in ("passed", "tie"):
            outcome = "passed"
        elif tally.outcome == "needs_research":
            outcome = "passed"  # Pass with notes
        else:
            outcome = "rejected"

        # Determine demotion target from rejecting reviewers
        demotion_target = None
        if outcome == "rejected":
            for i, v in enumerate(votes):
                if v.verdict in (Verdict.REJECTED, Verdict.NOT_SUITABLE):
                    if i < len(reviewers):
                        demotion_target = reviewers[i].get("demotion_target", "indev")
                        break
            if not demotion_target:
                demotion_target = "indev"

        logger.info(f"[{AGENT_NAME}] Task '%s': %s", self.task_id, outcome)

        return FullReviewPipelineResult(
            task_id=self.task_id,
            outcome=outcome,
            votes=votes,
            demotion_target=demotion_target,
            summary=tally.summary,
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    async def _run_reviewer(self, reviewer: dict) -> Vote:
        """Run a single reviewer agent using a mini-loop with tool access."""
        prompt = (
            f"You are a final reviewer ({reviewer['type']}).\n"
            f"Focus: {reviewer['focus']}\n\n"
            f"Task: {self.task_description}\n"
            f"Files changed: {json.dumps(self.files_changed[:20])}\n\n"
            "You may use tools to read code files before giving your verdict.\n\n"
            "To complete your review, call the submit_work tool with:\n"
            "- signal: 'ACCEPTED' if the implementation is correct, or 'REJECTED' if there are defects.\n"
            "- summary: Your justification.\n"
            "- payload: {\"verdict\": \"LIKELY|POSSIBLE|NEEDS_RESEARCH|NOT_SUITABLE|REJECTED\", "
            "\"confidence\": <0-100>}"
        )

        messages: list[dict] = [
            {"role": "system", "content": "You are a code reviewer. Use submit_work to output your verdict when ready."},
            {"role": "user", "content": prompt},
        ]

        schemas = self._get_reviewer_schemas(reviewer["type"])
        max_turns = FULL_REVIEW_MAX_REVIEWER_TURNS
        _ctx_warned: set[float] = set()
        _turn_warned: set[int] = set()

        for turn in range(max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            # Turn saturation check
            from app.agent.config import check_turn_saturation
            if check_turn_saturation(
                turn, max_turns, _turn_warned, messages
            ):
                # Turn nudge was injected
                pass

            response = await call_llm(
                messages,
                base_url=self.llm_base_url,
                model=self.llm_model,
                tools=schemas,
                tool_choice="auto",
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=AGENT_NAME,
            )

            usage = response.get("usage", {})
            prompt_tokens_this_call = usage.get("prompt_tokens", 0)
            self._total_prompt += prompt_tokens_this_call
            self._total_completion += usage.get("completion_tokens", 0)

            # Context saturation check
            if check_context_saturation(
                prompt_tokens_this_call, self.max_context, _ctx_warned, messages
            ):
                logger.warning(
                    f"[{AGENT_NAME}] Reviewer '%s' context saturation (turn %d) - terminating",
                    reviewer["type"], turn + 1,
                )
                break

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

                    # Check for terminal signal from submit_work
                    if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                        try:
                            data = json.loads(tc_result)
                            payload = data.get("payload", {})
                            verdict_str = payload.get("verdict", "POSSIBLE").upper()
                            verdict = Verdict(verdict_str)
                            confidence = int(payload.get("confidence", 80))
                            lo, hi = verdict.confidence_range
                            confidence = max(lo, min(hi, confidence))
                            justification = data.get("summary", "")
                            return Vote(
                                stage=f"review_{reviewer['type']}",
                                verdict=verdict,
                                confidence=confidence,
                                justification=justification,
                                model=self.llm_model or "",
                            )
                        except (json.JSONDecodeError, ValueError):
                            pass
                continue

            turns_remaining = max_turns - turn - 1
            if turns_remaining <= 2:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] {turns_remaining} turns remaining. Call submit_work with your verdict now.",
                })

        # Fallback: turns exhausted
        return Vote(
            stage=f"review_{reviewer['type']}",
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=65,
            justification="Reviewer exhausted turns",
            model=self.llm_model or "",
        )

    def _has_frontend_changes(self) -> bool:
        """Check if any changed files match frontend patterns."""
        if not FULL_REVIEW_AUTO_UX:
            return False
        import fnmatch
        for fpath in self.files_changed:
            for pattern in FULL_REVIEW_FRONTEND_PATTERNS:
                if fnmatch.fnmatch(fpath, pattern):
                    return True
        return False

    def _store_reviewer_result(self, vote: Vote, reviewer_type: str) -> None:
        try:
            from app.database import create_full_review_result
            create_full_review_result(
                task_id=self.task_id,
                reviewer_type=reviewer_type,
                verdict=vote.verdict.value,
                confidence=vote.confidence,
                justification=vote.justification,
                model=self.llm_model,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                prompt_tokens=vote.prompt_tokens,
                completion_tokens=vote.completion_tokens,
            )
        except Exception as e:
            logger.error(f"[{AGENT_NAME}] Failed to store result: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_full_review_pipeline(
    task_id: str,
    task_description: str,
    files_changed: list[str] | None = None,
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_path: str | None = None,
) -> dict:
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)

    _max_context = 0
    if llm_id is not None:
        from app.database import get_llm as _get_llm
        _llm_record = _get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    pipeline = FullReviewPipeline(
        task_id=task_id,
        task_description=task_description,
        files_changed=files_changed,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
        project_path=project_path,
        max_context=_max_context,
    )
    result = await pipeline.run()
    return {
        "task_id": result.task_id,
        "outcome": result.outcome,
        "summary": result.summary,
        "demotion_target": result.demotion_target,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.votes
        ],
    }
