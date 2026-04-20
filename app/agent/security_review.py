"""
app/agent/security_review.py
------------------------------
Security Pipeline - 3-agent veto-power security gate.

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
    SECURITY_REVIEW_VETO_POWER,
    SECURITY_REVIEW_RESEARCH_LIVES,
    SECURITY_REVIEW_MAX_REVIEWER_TURNS,
    SECURITY_REVIEWER_TOOLS,
    PROJECT_ROOT,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.tools import _task_git_cwd, dispatch_tool, build_tool_schemas, set_task_git_cwd
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.research import run_research
from app.agent.verdicts import Vote, Verdict

logger = logging.getLogger(__name__)
AGENT_NAME = "Security Pipeline"


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


def run_shell_security(command: str, *, project_path: str | None = None) -> str:
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
        cwd = project_path or _task_git_cwd.get() or PROJECT_ROOT
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=SHELL_TIMEOUT_SECONDS,
            cwd=cwd,
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

    _REVIEWER_SCHEMAS: list[dict] = build_tool_schemas(SECURITY_REVIEWER_TOOLS)

    def __init__(
        self,
        task_id: str,
        task_description: str,
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
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.project_path = project_path
        self.max_context = max_context
        self._total_prompt = 0
        self._total_completion = 0

    _REVIEWERS = [
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

    async def run(self) -> SecurityReviewPipelineResult:
        """Run pre-scan + 3 security reviewers, then research any uncertainties."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        logger.info(f"[{AGENT_NAME}] Starting for task '%s'", self.task_id)

        # Phase 0: Deterministic pre-scan (bandit, detect-secrets)
        scan_context = await self._run_pre_scan()

        tasks = [self._run_reviewer(r, scan_context) for r in self._REVIEWERS]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        votes: list[Vote] = []
        findings: list[dict] = []

        for i, result in enumerate(results):
            reviewer_type = self._REVIEWERS[i]["type"]
            if isinstance(result, Exception):
                logger.warning(f"[{AGENT_NAME}] Reviewer '%s' failed: %s", reviewer_type, result)
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
            self._store_reviewer_result(vote, reviewer_findings, reviewer_type)

        # Phase 1: Handle NEEDS_RESEARCH via research agent
        needs_research = [v for v in votes if v.verdict == Verdict.NEEDS_RESEARCH]
        if needs_research:
            votes, extra_findings = await self._handle_needs_research(votes, scan_context)
            findings.extend(extra_findings)

        # Tally with veto rules
        outcome, summary, demotion_target = self._tally_security(votes, findings)

        logger.info(f"[{AGENT_NAME}] Task '%s': %s", self.task_id, outcome)

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

    async def _run_pre_scan(self) -> str:
        """Run allowlisted security scanners and return output as context string."""
        import functools

        commands = [
            "python -m bandit -r . -q --no-show-progress",
            "python -m detect_secrets scan",
        ]
        loop = asyncio.get_event_loop()
        shell_fn = functools.partial(run_shell_security, project_path=self.project_path)
        scan_outputs = []

        for cmd in commands:
            try:
                result = await loop.run_in_executor(None, shell_fn, cmd)
                if result and not result.startswith("ERROR:"):
                    scanner_name = cmd.split()[2]
                    scan_outputs.append(f"[{scanner_name}]\n{result[:2000]}")
                else:
                    logger.debug(f"[{AGENT_NAME}] Pre-scan '%s': %s", cmd, result[:200])
            except Exception as e:
                logger.warning(f"[{AGENT_NAME}] Pre-scan '%s' failed: %s", cmd, e)

        if not scan_outputs:
            return ""

        return (
            "=== SECURITY SCANNER RESULTS ===\n"
            + "\n\n".join(scan_outputs)
            + "\n=== END SCANNER RESULTS ===\n\n"
        )

    async def _handle_needs_research(
        self, votes: list[Vote], scan_context: str
    ) -> tuple[list[Vote], list[dict]]:
        """Spawn research agent for NEEDS_RESEARCH votes, then re-vote affected reviewers."""
        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        research_votes = [v for v in votes if v.verdict == Verdict.NEEDS_RESEARCH]
        questions = [f"[{v.stage}] {v.justification}" for v in research_votes]
        question = (
            f"Security review of task {self.task_id} found uncertain areas:\n"
            + "\n".join(questions)
            + "\n\nInvestigate these security concerns. Determine if they represent:\n"
            "1. Fundamental architectural security flaws -> recommend demotion to planning\n"
            "2. Implementation-level issues -> recommend demotion to indev\n"
            "3. Data handling issues -> recommend demotion to optimization\n"
            "4. False positives or minor concerns that can pass"
        )

        logger.info(
            f"[{AGENT_NAME}] NEEDS_RESEARCH from %d reviewer(s), spawning research agent.",
            len(research_votes),
        )
        try:
            research_result = await run_research(
                question=question,
                context={"task_id": self.task_id, "task_description": self.task_description},
                max_lives=SECURITY_REVIEW_RESEARCH_LIVES,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
                task_id=str(self.task_id),
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )
            self._total_prompt += research_result.prompt_tokens
            self._total_completion += research_result.completion_tokens
            findings_text = research_result.findings or "No specific findings."
        except Exception as e:
            logger.warning(f"[{AGENT_NAME}] Research agent failed: %s", e)
            return votes, []

        # Re-vote affected reviewers with research context appended
        needs_research_stages = {v.stage for v in research_votes}
        stage_to_reviewer = {f"security_{r['type']}": r for r in self._REVIEWERS}
        extra_context = f"{scan_context}\n## Security Research Findings\n{findings_text}\n\n"

        re_vote_tasks = []
        re_vote_stages = []
        for v in votes:
            if v.stage in needs_research_stages:
                reviewer = stage_to_reviewer.get(v.stage)
                if reviewer:
                    re_vote_tasks.append(self._run_reviewer(reviewer, extra_context))
                    re_vote_stages.append(v.stage)

        if not re_vote_tasks:
            return votes, []

        re_results = await asyncio.gather(*re_vote_tasks, return_exceptions=True)
        vote_map = {v.stage: v for v in votes}
        new_findings: list[dict] = []

        for i, stage in enumerate(re_vote_stages):
            if not isinstance(re_results[i], Exception):
                new_vote, reviewer_findings = re_results[i]
                vote_map[stage] = new_vote
                new_findings.extend(reviewer_findings)

        return list(vote_map.values()), new_findings

    async def _run_reviewer(
        self, reviewer: dict, scan_context: str = ""
    ) -> tuple[Vote, list[dict]]:
        """Run a single security reviewer using a mini-loop with tool access."""
        prompt = (
            f"You are a security reviewer ({reviewer['perspective']}).\n"
            f"Focus: {reviewer['focus']}\n\n"
            f"{scan_context}"
            f"Task being reviewed: {self.task_description}\n\n"
            "Analyze for security issues. You may use tools to inspect code files. "
            "For each finding, classify severity as critical/high/medium/low.\n\n"
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

        messages: list[dict] = [
            {"role": "system", "content": "You are a security expert. Output your verdict as JSON when ready."},
            {"role": "user", "content": prompt},
        ]

        max_turns = SECURITY_REVIEW_MAX_REVIEWER_TURNS
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
                tools=self._REVIEWER_SCHEMAS,
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
                continue

            raw = extract_json_block(content)
            if raw:
                try:
                    data = json.loads(raw)
                    if "verdict" in data:
                        verdict_str = data.get("verdict", "POSSIBLE").upper()
                        verdict = Verdict(verdict_str)
                        confidence = int(data.get("confidence", 80))
                        lo, hi = verdict.confidence_range
                        confidence = max(lo, min(hi, confidence))
                        justification = data.get("justification", "")
                        findings = data.get("findings", [])
                        vote = Vote(
                            stage=f"security_{reviewer['type']}",
                            verdict=verdict,
                            confidence=confidence,
                            justification=justification,
                            model=self.llm_model or "",
                        )
                        return vote, findings
                except (json.JSONDecodeError, ValueError):
                    pass

            turns_remaining = max_turns - turn - 1
            if turns_remaining <= 2:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] {turns_remaining} turns remaining. Output JSON verdict now.",
                })

        # Fallback: turns exhausted
        vote = Vote(
            stage=f"security_{reviewer['type']}",
            verdict=Verdict.NEEDS_RESEARCH,
            confidence=65,
            justification="Reviewer exhausted turns",
            model=self.llm_model or "",
        )
        return vote, []

    def _tally_security(
        self, votes: list[Vote], findings: list[dict]
    ) -> tuple[str, str, str | None]:
        """Tally with strict security rules.

        1. Any REJECTED -> immediate rejection
        2. Any NOT_SUITABLE -> rejection (veto power)
        3. NEEDS_RESEARCH -> security research agent (conservative default)
        4. All POSSIBLE or LIKELY -> passed
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

        # Any remaining NEEDS_RESEARCH after research agent = conservative reject
        remaining_research = [v for v in votes if v.verdict == Verdict.NEEDS_RESEARCH]
        if remaining_research:
            return (
                "rejected",
                f"Security research exhausted: {len(remaining_research)} reviewer(s) still uncertain.",
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
            logger.error(f"[{AGENT_NAME}] Failed to store reviewer result: %s", e)


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
    project_path: str | None = None,
) -> dict:
    """Run the security pipeline and return a result dict."""
    if project_path:
        set_task_git_cwd(project_path)

    _max_context = 0
    if llm_id is not None:
        from app.database import get_llm as _get_llm
        _llm_record = _get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    pipeline = SecurityPipeline(
        task_id=task_id,
        task_description=task_description,
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
        "findings": result.findings,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.votes
        ],
    }
