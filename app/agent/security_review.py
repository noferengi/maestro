"""
app/agent/security_review.py
------------------------------
Security Pipeline — 3-agent veto-power security gate.

SECURITY GETS VETO POWER. A single security reviewer can block advancement.

3 Parallel Agents:
  - Offensive (Red Team): OWASP Top 10, input validation, secret exposure
  - Defensive (Blue Team): Auth/authz, error handling, dependency CVEs
  - Compliance & Data Flow: Data flow tracing, PCI-DSS, GDPR, CCPA, HIPAA

Includes allowlist-only shell for security scanners.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    SECURITY_REVIEW_LLM_TEMPERATURE,
    SECURITY_REVIEW_VETO_POWER,
    SECURITY_REVIEW_RESEARCH_LIVES,
    PROJECT_ROOT,
)
from app.agent.llm_client import call_llm
from app.agent.verdicts import Vote, Verdict

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Allowlisted security scanner shell
# ---------------------------------------------------------------------------

SECURITY_SCANNER_ALLOWLIST = [
    r"^python\s+-m\s+bandit\b",
    r"^python\s+-m\s+safety\b",
    r"^python\s+-m\s+pip\s+audit\b",
    r"^python\s+-m\s+detect_secrets\b",
    r"^semgrep\b",
    r"^trivy\b",
    r"^npm\s+audit\b",
]

_SECURITY_ALLOWLIST_RE = [re.compile(p) for p in SECURITY_SCANNER_ALLOWLIST]


def run_shell_security(command: str) -> str:
    """Execute a shell command from the security scanner allowlist only.

    Unlike run_shell (blocklist), this uses a strict allowlist.
    Only commands matching SECURITY_SCANNER_ALLOWLIST patterns are permitted.
    """
    import subprocess
    from app.agent.config import SHELL_TIMEOUT_SECONDS

    command = command.strip()
    allowed = any(pat.match(command) for pat in _SECURITY_ALLOWLIST_RE)
    if not allowed:
        return (
            f"ERROR: Command not in security scanner allowlist. "
            f"Allowed: {', '.join(SECURITY_SCANNER_ALLOWLIST)}"
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
class SecurityReviewPipelineResult:
    task_id: str
    outcome: str  # "passed" | "rejected"
    votes: list[Vote] = field(default_factory=list)
    findings: list[dict] = field(default_factory=list)
    demotion_target: str | None = None  # "planning" | "development" | "optimization"
    summary: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------

class SecurityPipeline:
    """3-agent veto-power security gate."""

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

    async def run(self) -> SecurityReviewPipelineResult:
        """Run all 3 security reviewers in parallel."""
        logger.info("[security] Starting for task '%s'", self.task_id)

        reviewers = [
            {
                "type": "offensive",
                "perspective": "Red Team / Attacker mindset",
                "focus": (
                    "OWASP Top 10 vulnerabilities, input validation gaps, "
                    "secret/credential exposure, command injection, path traversal, "
                    "data exfiltration vectors."
                ),
            },
            {
                "type": "defensive",
                "perspective": "Blue Team / Defender mindset",
                "focus": (
                    "Auth/authz coverage, error handling & info disclosure, "
                    "dependency CVEs, encryption at rest/transit, security headers, "
                    "rate limiting."
                ),
            },
            {
                "type": "compliance",
                "perspective": "Compliance & Data Flow",
                "focus": (
                    "Data flow tracing (input->output->stores), PCI-DSS, GDPR, "
                    "CCPA, HIPAA compliance, data minimization, optimization regression check."
                ),
            },
        ]

        tasks = [self._run_reviewer(r) for r in reviewers]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        votes: list[Vote] = []
        findings: list[dict] = []
        demotion_target = None

        for i, result in enumerate(results):
            reviewer_type = reviewers[i]["type"]
            if isinstance(result, Exception):
                logger.warning("[security] Reviewer '%s' failed: %s", reviewer_type, result)
                votes.append(Vote(
                    stage=f"security_{reviewer_type}",
                    verdict=Verdict.NEEDS_RESEARCH,
                    confidence=65,
                    justification=f"Security reviewer failed: {result}",
                ))
                continue

            vote, reviewer_findings = result
            votes.append(vote)
            findings.extend(reviewer_findings)

            # Store individual result
            self._store_reviewer_result(vote, reviewer_findings, reviewer_type)

        # Tally with veto rules
        outcome, summary, demotion_target = self._tally_security(votes, findings)

        logger.info("[security] Task '%s': %s", self.task_id, outcome)

        return SecurityReviewPipelineResult(
            task_id=self.task_id,
            outcome=outcome,
            votes=votes,
            findings=findings,
            demotion_target=demotion_target,
            summary=summary,
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    async def _run_reviewer(
        self, reviewer: dict
    ) -> tuple[Vote, list[dict]]:
        """Run a single security reviewer."""
        prompt = (
            f"You are a security reviewer ({reviewer['perspective']}).\n"
            f"Focus: {reviewer['focus']}\n\n"
            f"Task being reviewed: {self.task_description}\n\n"
            "Analyze for security issues. For each finding, classify severity as "
            "critical/high/medium/low.\n\n"
            "Output JSON: {\n"
            "  \"verdict\": \"LIKELY|POSSIBLE|NEEDS_RESEARCH|NOT_SUITABLE|REJECTED\",\n"
            "  \"confidence\": <0-100>,\n"
            "  \"justification\": \"...\",\n"
            "  \"findings\": [{\"type\": \"...\", \"severity\": \"critical|high|medium|low\", "
            "\"description\": \"...\", \"demotion_target\": \"development|planning|optimization\"}],\n"
            "  \"critical_count\": 0,\n"
            "  \"high_count\": 0\n"
            "}"
        )

        response = await call_llm(
            [
                {"role": "system", "content": "You are a security expert. Output only JSON."},
                {"role": "user", "content": prompt},
            ],
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=SECURITY_REVIEW_LLM_TEMPERATURE,
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
            findings = data.get("findings", [])
        except (json.JSONDecodeError, ValueError):
            verdict = Verdict.NEEDS_RESEARCH
            confidence = 65
            justification = content[:500]
            findings = []

        vote = Vote(
            stage=f"security_{reviewer['type']}",
            verdict=verdict,
            confidence=confidence,
            justification=justification,
            model=self.llm_model or "",
        )

        return vote, findings

    def _tally_security(
        self, votes: list[Vote], findings: list[dict]
    ) -> tuple[str, str, str | None]:
        """Tally with strict security rules.

        1. Any REJECTED → immediate rejection
        2. Any NOT_SUITABLE → rejection (veto power)
        3. NEEDS_RESEARCH → security research agent (conservative default)
        4. All POSSIBLE or LIKELY → passed
        """
        demotion_target = None

        # Check for critical/high findings
        critical_findings = [f for f in findings if f.get("severity") in ("critical", "high")]

        if SECURITY_REVIEW_VETO_POWER:
            # Any REJECTED or NOT_SUITABLE blocks
            blocking = [v for v in votes if v.verdict in (Verdict.REJECTED, Verdict.NOT_SUITABLE)]
            if blocking:
                # Determine demotion target from findings
                for f in critical_findings:
                    dt = f.get("demotion_target")
                    if dt:
                        demotion_target = dt
                        break
                if not demotion_target:
                    demotion_target = "development"

                reasons = [f"{v.stage}: {v.justification}" for v in blocking]
                return (
                    "rejected",
                    f"Security VETO: {len(blocking)} reviewer(s) blocked. {'; '.join(reasons[:3])}",
                    demotion_target,
                )

        # NEEDS_RESEARCH → conservative rejection
        research = [v for v in votes if v.verdict == Verdict.NEEDS_RESEARCH]
        if research:
            return (
                "rejected",
                f"Security research needed: {len(research)} reviewer(s) uncertain. Conservative reject.",
                "development",
            )

        return "passed", f"All {len(votes)} security reviewers passed.", None

    def _store_reviewer_result(
        self, vote: Vote, findings: list[dict], reviewer_type: str
    ) -> None:
        """Persist individual reviewer result to database."""
        try:
            from app.database import create_security_review_result
            critical_count = sum(1 for f in findings if f.get("severity") == "critical")
            high_count = sum(1 for f in findings if f.get("severity") == "high")

            create_security_review_result(
                task_id=self.task_id,
                reviewer_type=reviewer_type,
                owasp_findings=json.dumps([f for f in findings if "owasp" in f.get("type", "").lower()]),
                verdict=vote.verdict.value,
                confidence=vote.confidence,
                justification=vote.justification,
                critical_count=critical_count,
                high_count=high_count,
                model=self.llm_model,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                prompt_tokens=vote.prompt_tokens,
                completion_tokens=vote.completion_tokens,
            )
        except Exception as e:
            logger.error("[security] Failed to store reviewer result: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_security_pipeline(
    task_id: str,
    task_description: str,
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> dict:
    """Run the security pipeline and return a result dict."""
    pipeline = SecurityPipeline(
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
        "summary": result.summary,
        "demotion_target": result.demotion_target,
        "findings": result.findings,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.votes
        ],
    }
