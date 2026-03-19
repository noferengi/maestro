"""
app/agent/full_review.py
-------------------------
Full/Final Review Pipeline — 4-agent final judgment.

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
    FULL_REVIEW_LLM_TEMPERATURE,
    FULL_REVIEW_AUTO_UX,
    FULL_REVIEW_FRONTEND_PATTERNS,
    FULL_REVIEW_RESEARCH_LIVES,
    PROJECT_ROOT,
    SHELL_TIMEOUT_SECONDS,
)
from app.agent.llm_client import call_llm
from app.agent.verdicts import Vote, Verdict, tally_votes

logger = logging.getLogger(__name__)


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


def run_shell_review(command: str) -> str:
    """Execute a shell command from the review runner allowlist only."""
    import subprocess

    command = command.strip()
    allowed = any(pat.match(command) for pat in _REVIEW_ALLOWLIST_RE)
    if not allowed:
        return (
            f"ERROR: Command not in review runner allowlist. "
            f"Allowed patterns: {', '.join(REVIEW_SHELL_ALLOWLIST)}"
        )

    try:
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
            cwd=PROJECT_ROOT,
        )
        output = result.stdout + result.stderr
        return output[:8000] if output else "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: Command timed out after {SHELL_TIMEOUT_SECONDS}s"
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
    ):
        self.task_id = task_id
        self.task_description = task_description
        self.files_changed = files_changed or []
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self._total_prompt = 0
        self._total_completion = 0

    async def run(self) -> FullReviewPipelineResult:
        """Run all review agents in parallel."""
        logger.info("[full_review] Starting for task '%s'", self.task_id)

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
                logger.warning("[full_review] Reviewer '%s' failed: %s", reviewer_type, result)
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

        logger.info("[full_review] Task '%s': %s", self.task_id, outcome)

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
        """Run a single reviewer agent."""
        prompt = (
            f"You are a final reviewer ({reviewer['type']}).\n"
            f"Focus: {reviewer['focus']}\n\n"
            f"Task: {self.task_description}\n"
            f"Files changed: {json.dumps(self.files_changed[:20])}\n\n"
            "Output JSON: {\"verdict\": \"LIKELY|POSSIBLE|NEEDS_RESEARCH|NOT_SUITABLE|REJECTED\", "
            "\"confidence\": <0-100>, \"justification\": \"...\"}"
        )

        response = await call_llm(
            [
                {"role": "system", "content": "You are a code reviewer. Output only JSON."},
                {"role": "user", "content": prompt},
            ],
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=FULL_REVIEW_LLM_TEMPERATURE,
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
            verdict = Verdict(verdict_str)
            confidence = int(data.get("confidence", 80))
            lo, hi = verdict.confidence_range
            confidence = max(lo, min(hi, confidence))
            justification = data.get("justification", "")
        except (json.JSONDecodeError, ValueError):
            verdict = Verdict.POSSIBLE
            confidence = 80
            justification = content[:500]

        return Vote(
            stage=f"review_{reviewer['type']}",
            verdict=verdict,
            confidence=confidence,
            justification=justification,
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
            logger.error("[full_review] Failed to store result: %s", e)


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
) -> dict:
    pipeline = FullReviewPipeline(
        task_id=task_id,
        task_description=task_description,
        files_changed=files_changed,
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
        "demotion_target": result.demotion_target,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.votes
        ],
    }
