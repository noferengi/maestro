"""
app/agent/research.py
---------------------
Research Agent - a lightweight agentic loop for investigating unknowns.

Used by the intake pipeline when:
  - A stage votes NEEDS_RESEARCH (insufficient info to assess)
  - Votes are tied (2-2 split) and a tie-breaker investigation is needed

The research agent has:
  - Restricted tools (read-only: read_file, search_files, find_files, list_directory, git_status, git_diff, git_log, git_blame)
  - A "lives" system: up to N sequential agent runs, each seeded with the previous run's findings
  - A structured vote as its terminal output (verdict + confidence + justification)
  - A lower turn cap per life (default 20 turns vs MaestroLoop's 150)

Each "life" is a fresh LLM conversation. If a life ends without rendering a confident
verdict, the next life starts with the accumulated findings as context. After all lives
are exhausted, the agent returns NOT_SUITABLE as a fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass, field
from itertools import zip_longest
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    PROJECT_ROOT,
    INTAKE_LLM_TEMPERATURE,
    RESEARCH_AGENT_MAX_LIVES,
    RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    RESEARCH_AGENT_TOOLS,
    RESEARCH_CONTEXT_BUDGET_RATIO,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError, ContextTooLargeError
from app.agent.tools import TOOL_SCHEMAS, TOOL_REGISTRY, dispatch_tool, LISTING_EXCLUDED_DIRS
from app.database import get_llm

logger = logging.getLogger(__name__)
AGENT_NAME = "Research Agent"
_INVESTIGATION_AGENT_NAME = "Investigation Agent"
_WEB_SEARCH_AGENT_NAME = "Web Search Agent"


# ---------------------------------------------------------------------------
# Source file extensions for greenfield detection
# ---------------------------------------------------------------------------

_SOURCE_EXTENSIONS = {
    '.py', '.js', '.ts', '.jsx', '.tsx', '.java', '.kt', '.go',
    '.rs', '.c', '.cpp', '.h', '.hpp', '.cs', '.rb', '.swift',
    '.dart', '.scala', '.php', '.lua', '.zig', '.nim',
}


def _has_meaningful_source_files(project_root: str = PROJECT_ROOT) -> bool:
    """Check if the project directory contains meaningful source code files."""
    from app.agent.path_filter import walk_safe
    for root, dirs, files in walk_safe(project_root):
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SOURCE_EXTENSIONS:
                return True
    return False


# ---------------------------------------------------------------------------
# Grammar-constrained epilogue - GBNF for llama.cpp
# ---------------------------------------------------------------------------
# Key order is fixed (grade → justification → verdict) so the sampler can
# pre-fill tokens in order.  justification-text excludes double-quotes to
# keep the grammar simple; truncation to 1024 chars is done in Python.
# NEEDS_RESEARCH is intentionally allowed here - it means "investigation
# budget was insufficient" and is tagged so tally_votes() won't re-spawn.

_FORCED_VERDICT_GRAMMAR = r"""
root             ::= "{" ws grade-kv "," ws justification-kv "," ws verdict-kv ws "}"
grade-kv         ::= "\"grade\": " grade-int
grade-int        ::= [0-9]+
justification-kv ::= "\"justification\": \"" [^"]+ "\""
verdict-kv       ::= "\"verdict\": \"" verdict "\""
verdict          ::= "REJECTED" | "NOT_SUITABLE" | "POSSIBLE" | "LIKELY" | "NEEDS_RESEARCH"
ws               ::= [ \t\n]*
"""

# grade is an integer 0-10000 representing investigation quality in hundredths
# of a percent (e.g. 9258 = 92.58%).  confidence = grade // 100 (0-100 int).
# Valid confidence range per verdict - used to clamp to a valid value so that
# Vote.__post_init__ validation never raises on epilogue results.
_VERDICT_CONFIDENCE_RANGES: dict[str, tuple[int, int]] = {
    "REJECTED":       (0,  50),
    "NOT_SUITABLE":   (51, 60),
    "NEEDS_RESEARCH": (61, 75),
    "POSSIBLE":       (76, 91),
    "LIKELY":         (92, 100),
}


# ---------------------------------------------------------------------------
# Post-mortem prompt - injected after turn exhaustion, before the next life
# ---------------------------------------------------------------------------

_POST_MORTEM_PROMPT = """\
[SYSTEM] You have exhausted your turns for this investigation life without rendering a verdict.
Before this life closes, produce a structured handoff summary so the next investigator can
continue from where you left off. Use this exact format:

WHAT I INVESTIGATED: <tools called, paths explored, questions asked>
WHAT I FOUND: <concrete facts, file contents, code patterns discovered>
WHAT I AM SATISFIED WITH: <aspects of the question that are now clear>
WHAT I AM UNSATISFIED WITH: <aspects still unresolved or uncertain>
WHAT THE NEXT INVESTIGATOR SHOULD FOCUS ON: <specific follow-up files, functions, or questions>

Write only the summary. Do not render a verdict here.\
"""


def _extract_section(text: str, header: str) -> str:
    """Extract the content under `header:` up to the next all-caps section header."""
    if not text:
        return ""
    lower_text = text.lower()
    lower_marker = (header + ":").lower()
    idx = lower_text.find(lower_marker)
    if idx == -1:
        return ""
    content_start = idx + len(lower_marker)
    remainder = text[content_start:]
    lines = remainder.split("\n")
    result: list[str] = []
    for line in lines:
        if re.match(r"^[A-Z][A-Z ]{4,}:", line.strip()):
            break
        result.append(line)
    return "\n".join(result).strip()


# ---------------------------------------------------------------------------
# Restricted tool schemas - only the tools a research agent is allowed to use
# ---------------------------------------------------------------------------

def _build_restricted_schemas(has_source: bool = True) -> list[dict]:
    """Filter TOOL_SCHEMAS to only include tools in RESEARCH_AGENT_TOOLS.

    If has_source is False (greenfield), excludes codebase read tools (read_file, search_files, etc).
    Always includes list_directory, find_files, git_status, git_log.
    """
    allowed = set(RESEARCH_AGENT_TOOLS)
    if not has_source:
        # Exclude deep-read tools for greenfield
        allowed.difference_update({"read_file", "search_files", "git_diff", "git_blame", "git_show"})

    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in allowed
    ]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class LifeResult:
    """Outcome of a single research agent life."""
    findings: str
    vote: dict | None  # None if the agent didn't render a verdict
    turns_used: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    handoff_summary: str = ""  # Structured post-mortem; empty when agent rendered a verdict


@dataclass(slots=True)
class ResearchResult:
    """Final outcome of the full research agent run (across all lives)."""
    vote: dict  # {verdict, confidence, justification}
    lives_used: int
    total_turns: int
    findings: str  # accumulated findings across all lives
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

_RESEARCH_SYSTEM_PROMPT = """You are a **Research Agent** inside the Maestro Orchestrator.

Your job is to INVESTIGATE a question about a software task's feasibility, scope, or
conflicts. You have read-only access to the codebase via tools. You cannot modify files.

== TOOLS AVAILABLE ==
- read_file(path): Read a file's contents
- search_files(pattern, directory): Regex search across file contents
- find_files(glob_pattern, directory): Find files by name pattern
- list_directory(path): List directory contents
- git_status(): Show current git status
- git_diff(path?): Show git diff (optionally scoped to a file)
- git_log(path?, max_count?): Show recent git history (optionally scoped to a file)
- git_blame(path): Show git blame for a file

== YOUR WORKFLOW ==
1. Read the investigation question and context carefully.
2. Use your tools to gather evidence from the codebase.
3. When you have enough information, render your verdict as a JSON object.

== OUTPUT FORMAT ==
When you are ready to render your verdict, output ONLY this JSON (no other text):

```json
{
  "verdict": "REJECTED" | "NOT_SUITABLE" | "NEEDS_RESEARCH" | "POSSIBLE" | "LIKELY",
  "confidence": <integer 0-100>,
  "justification": "<one paragraph explaining your findings and reasoning>",
  "findings": "<summary of what you discovered during investigation>"
}
```

Confidence ranges:
- REJECTED: [0, 50] - fundamental blocker found
- NOT_SUITABLE: [51, 60] - task is poorly scoped or inappropriate
- NEEDS_RESEARCH: [61, 75] - still insufficient info (only use if you truly cannot determine)
- POSSIBLE: [76, 91] - can probably be done
- LIKELY: [92, 100] - high confidence it can be accomplished

== RULES ==
- Be thorough but efficient. You have limited turns.
- Do NOT attempt to write or modify any files.
- Do NOT output free-form prose as your final action - always end with the JSON verdict.
- If you cannot determine feasibility, say so honestly with NEEDS_RESEARCH.
- Focus on evidence from the actual code, not assumptions.
"""

_TIEBREAKER_SYSTEM_PROMPT = """You are a **Tie-Breaker Research Agent** inside the Maestro Orchestrator.

A task advancement vote resulted in a tie - the voters are split on whether this task
should proceed. Your job is to investigate the SPECIFIC POINTS OF DISAGREEMENT between
the voters, gather evidence from the codebase, and cast the deciding vote.

== CONTEXT ==
You will receive:
1. The task description
2. All voter responses (their verdicts, confidence scores, and justifications)
3. The specific disagreements to investigate

== TOOLS AVAILABLE ==
- read_file(path): Read a file's contents
- search_files(pattern, directory): Regex search across file contents
- find_files(glob_pattern, directory): Find files by name pattern
- list_directory(path): List directory contents
- git_status(): Show current git status
- git_diff(path?): Show git diff (optionally scoped to a file)
- git_log(path?, max_count?): Show recent git history (optionally scoped to a file)
- git_blame(path): Show git blame for a file

== YOUR WORKFLOW ==
1. Read each voter's justification carefully. Identify where they disagree.
2. Use tools to gather evidence that resolves the disagreement.
3. Render your deciding verdict based on evidence, not opinion.

== OUTPUT FORMAT ==
```json
{
  "verdict": "REJECTED" | "NOT_SUITABLE" | "NEEDS_RESEARCH" | "POSSIBLE" | "LIKELY",
  "confidence": <integer 0-100>,
  "justification": "<explain which voter was correct and why, citing evidence>",
  "findings": "<summary of investigation>",
  "resolved_disagreements": ["<point 1>", "<point 2>"]
}
```

== RULES ==
- You MUST pick a side. Do not return NEEDS_RESEARCH unless you genuinely cannot find evidence.
- Cite specific files, line numbers, or code patterns as evidence.
- Be concise but thorough.
"""


# ---------------------------------------------------------------------------
# Research Agent
# ---------------------------------------------------------------------------

class ResearchAgent:
    """
    Lightweight agentic loop for investigating unknowns.

    Usage::

        agent = ResearchAgent(
            question="Is it feasible to add a WebSocket endpoint?",
            context={"task_description": "...", "stage_output": {...}},
        )
        result = await agent.run()
        # result.vote = {"verdict": "POSSIBLE", "confidence": 83, ...}
    """

    def __init__(
        self,
        question: str,
        context: dict[str, Any],
        max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
        max_lives: int | None = None,
        is_tiebreaker: bool = False,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        task_id: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        max_context: int = 0,
        project_root: str | None = None,
    ) -> None:
        self.question = question
        self.context = context
        self.max_turns_per_life = max_turns_per_life
        self.max_lives = max_lives or RESEARCH_AGENT_MAX_LIVES
        self.is_tiebreaker = is_tiebreaker
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.task_id = task_id
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_context = max_context
        self.project_root = project_root

        # Greenfield detection
        self._has_source = _has_meaningful_source_files(self.project_root or PROJECT_ROOT)
        self._restricted_schemas = _build_restricted_schemas(self._has_source)
        self._allowed_tools = {s["function"]["name"] for s in self._restricted_schemas}

        self._accumulated_findings: list[str] = []
        self._accumulated_summaries: list[str] = []  # post-mortem per exhausted life
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    async def run(self) -> ResearchResult:
        """Execute the research agent across all lives. Returns a ResearchResult."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        total_turns = 0

        for life_num in range(1, self.max_lives + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            logger.info("Research agent life %d/%d for: %s", life_num, self.max_lives, self.question[:80])

            life_context = self._build_life_context(life_num)
            life_result = await self._run_life(life_context, life_num)

            total_turns += life_result.turns_used
            self._total_prompt_tokens += life_result.prompt_tokens
            self._total_completion_tokens += life_result.completion_tokens

            if life_result.findings:
                self._accumulated_findings.append(f"[Life {life_num}] {life_result.findings}")
            self._accumulated_summaries.append(life_result.handoff_summary)

            # Context overflow - surface immediately, no point spawning more lives
            if life_result.vote and life_result.vote.get("verdict") == "TOO_LARGE":
                logger.info("Research agent TOO_LARGE - task scope exceeds context budget")
                return ResearchResult(
                    vote=life_result.vote,
                    lives_used=life_num,
                    total_turns=total_turns,
                    findings="\n\n".join(self._accumulated_findings),
                    prompt_tokens=self._total_prompt_tokens,
                    completion_tokens=self._total_completion_tokens,
                )

            # If the agent rendered a confident verdict, return it
            if life_result.vote and life_result.vote.get("verdict") != "NEEDS_RESEARCH":
                return ResearchResult(
                    vote=life_result.vote,
                    lives_used=life_num,
                    total_turns=total_turns,
                    findings="\n\n".join(self._accumulated_findings),
                    prompt_tokens=self._total_prompt_tokens,
                    completion_tokens=self._total_completion_tokens,
                )

            # If the agent said NEEDS_RESEARCH and has lives left, continue
            if life_num < self.max_lives:
                logger.info("Research agent life %d inconclusive, spawning life %d", life_num, life_num + 1)

        # All lives exhausted - fire a forced verdict epilogue call
        logger.info("Research agent all lives exhausted - firing forced verdict epilogue")
        forced_vote = await self._forced_verdict_call()

        return ResearchResult(
            vote=forced_vote,
            lives_used=self.max_lives,
            total_turns=total_turns,
            findings="\n\n".join(self._accumulated_findings),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
        )

    # ------------------------------------------------------------------
    # Forced verdict epilogue
    # ------------------------------------------------------------------

    async def _forced_verdict_call(self) -> dict:
        """
        Final call after all lives are exhausted.  Uses grammar-constrained
        generation (GBNF) to force the LLM to emit a structured verdict JSON
        with a grade (0-9), justification, and verdict.

        NEEDS_RESEARCH is now allowed - it means "investigation budget was
        insufficient."  The ``source`` tag prevents tally_votes() from
        re-spawning a research agent for this vote.
        """
        # Prefer the structured post-mortem summaries (rich handoff content) over
        # the generic "Life N exhausted N turns" fallback strings in _accumulated_findings.
        summary_parts = [s for s in self._accumulated_summaries if s]
        finding_parts = [f for f in self._accumulated_findings if f]
        if summary_parts:
            accumulated = "\n\n".join(summary_parts)
        elif finding_parts:
            accumulated = "\n\n".join(finding_parts)
        else:
            accumulated = "No findings were recorded."
        context_snippet = json.dumps(self.context, indent=1)[:2000]

        system_prompt = (
            "/no_think\n"
            "You are a Research Agent. ALL research turns have been exhausted. "
            "You cannot use any tools. You MUST render a final verdict RIGHT NOW "
            "based solely on the accumulated findings below.\n\n"
            "Output ONLY this JSON object - key order MUST be exactly: grade, justification, verdict.\n"
            "{ \n"
            '  "grade": <integer 0-10000 representing investigation quality in hundredths of a percent, e.g. 9258 = 92.58%>,\n'
            '  "justification": "<your synthesis of the evidence - no double-quote characters>",\n'
            '  "verdict": "<REJECTED|NOT_SUITABLE|POSSIBLE|LIKELY|NEEDS_RESEARCH>"\n'
            "} \n\n"
            "Use NEEDS_RESEARCH only if the investigation budget was genuinely insufficient "
            "to reach a conclusion.  Use a lower grade for lower-quality investigations."
        )

        user_msg = (
            f"Original question: {self.question}\n\n"
            f"Context:\n{context_snippet}\n\n"
            f"Accumulated findings from {self.max_lives} research lives:\n{accumulated}\n\n"
            "Render your final verdict now."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ]

        for use_grammar in (True, False):
            try:
                kwargs: dict = dict(
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=0.1,
                    max_tokens=4096,  # headroom for thinking mode; /no_think in system prompt suppresses it if server is constrained
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
                if use_grammar:
                    kwargs["grammar"] = _FORCED_VERDICT_GRAMMAR
                response = await call_llm(messages, **kwargs)
                usage = response.get("usage", {})
                self._total_prompt_tokens += usage.get("prompt_tokens", 0)
                self._total_completion_tokens += usage.get("completion_tokens", 0)

                content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
                raw = extract_json_block(content) or content.strip()
                parsed = json.loads(raw.strip()) if raw else None
                if parsed and isinstance(parsed, dict):
                    # grade: integer 0-10000 (hundredths of a percent, e.g. 9258 = 92.58%)
                    grade = min(10000, max(0, int(parsed.get("grade", 5000))))
                    verdict = str(parsed.get("verdict", "NOT_SUITABLE"))
                    # confidence (0-100) derived from grade; clamped to the valid
                    # range for the verdict so Vote.__post_init__ never raises.
                    raw_confidence = grade // 100
                    lo, hi = _VERDICT_CONFIDENCE_RANGES.get(verdict, (0, 100))
                    confidence = max(lo, min(hi, raw_confidence))
                    justification = str(parsed.get("justification", ""))[:1024]
                    logger.info(
                        "Forced verdict epilogue: %s (grade=%d [%.2f%%], confidence=%d)"
                        " [grammar=%s]",
                        verdict, grade, grade / 100.0, confidence, use_grammar,
                    )
                    return {
                        "verdict": verdict,
                        "confidence": confidence,
                        "grade": grade,
                        "justification": justification,
                        "findings": "\n\n".join(self._accumulated_findings),
                        "source": "research_agent_epilogue",
                    }
            except Exception as exc:
                if is_shutting_down():
                    raise
                logger.warning(
                    "Forced verdict epilogue attempt (grammar=%s) failed: %s", use_grammar, exc
                )

        # Ultimate fallback if the epilogue call itself fails
        return {
            "verdict": "NOT_SUITABLE",
            "confidence": 40,
            "grade": 4000,   # 40.00% - reflects that lives + epilogue all failed
            "justification": (
                f"Research agent exhausted {self.max_lives} lives and the forced-verdict "
                f"epilogue also failed. Accumulated findings: "
                f"{'; '.join(self._accumulated_findings) or 'none'}"
            ),
            "findings": "\n\n".join(self._accumulated_findings),
            "source": "research_agent_epilogue",
        }

    # ------------------------------------------------------------------
    # Life execution
    # ------------------------------------------------------------------

    async def _run_life(self, context: str, life_num: int) -> LifeResult:
        """Run a single life of the research agent."""
        system_prompt = _TIEBREAKER_SYSTEM_PROMPT if self.is_tiebreaker else _RESEARCH_SYSTEM_PROMPT
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ]

        turns_used = 0
        life_prompt_tokens = 0
        life_completion_tokens = 0
        _ctx_warned: set[float] = set()
        _last_prompt_tokens = 0  # actual context size from the most recent LLM call

        for turn in range(self.max_turns_per_life):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            turns_used += 1

            # Hard budget check: compare the CURRENT context window fill (from the
            # previous call) against the budget.  life_prompt_tokens is a cumulative
            # SUM across all calls (correct for cost accounting) but grows quadratically,
            # so it fires far too early.  _last_prompt_tokens is the actual prompt size
            # of the most recent call — the true measure of context fill.
            budget_tokens = int(self.max_context * RESEARCH_CONTEXT_BUDGET_RATIO) if self.max_context else 0
            budget_exceeded = budget_tokens > 0 and _last_prompt_tokens >= budget_tokens

            if budget_exceeded:
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM] TOKEN BUDGET EXCEEDED. Render your JSON verdict now. No more tool calls.",
                })

            # LLM call
            try:
                response = await self._call_llm(messages, no_tools=budget_exceeded)
            except ContextTooLargeError as exc:
                # Pre-flight check caught the oversized prompt — abort immediately.
                # This is a normal outcome: do not retry, do not accumulate more messages.
                logger.warning(
                    "Research agent pre-flight context check (life %d, turn %d): %s — emitting TOO_LARGE",
                    life_num, turns_used, exc,
                )
                return LifeResult(
                    findings=f"Life {life_num} prompt exceeded context window before being sent to LLM.",
                    vote={
                        "verdict": "TOO_LARGE",
                        "confidence": 100,
                        "justification": str(exc),
                        "findings": f"Context too large at turn {turns_used} of life {life_num}.",
                    },
                    turns_used=turns_used,
                )
            except Exception as exc:
                exc_str = str(exc)
                if "400" in exc_str or "Bad Request" in exc_str:
                    # HTTP 400 context overflow — terminate this life immediately.
                    # Continuing would only make the context larger and guarantee every
                    # subsequent call also fails.
                    logger.warning(
                        "Research agent context overflow (life %d, turn %d) - emitting TOO_LARGE",
                        life_num, turns_used,
                    )
                    return LifeResult(
                        findings=f"Life {life_num} hit context window limit at turn {turns_used}.",
                        vote={
                            "verdict": "TOO_LARGE",
                            "confidence": 100,
                            "justification": (
                                "Context window exceeded - task scope is too large "
                                "for a single research life."
                            ),
                            "findings": f"Context overflowed at turn {turns_used} of life {life_num}.",
                        },
                        turns_used=turns_used,
                    )
                # Propagate shutdown immediately - don't retry on a dying interpreter.
                if is_shutting_down():
                    raise
                # Failed call does not count as a turn - the LLM produced no work.
                turns_used -= 1
                logger.error("Research agent LLM call failed (life %d, turn %d): %s", life_num, turns_used, exc)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Try a different approach or render your verdict now.",
                })
                continue

            usage = response.get("usage", {})
            prompt_tokens_this_call = usage.get("prompt_tokens", 0)
            life_prompt_tokens += prompt_tokens_this_call
            life_completion_tokens += usage.get("completion_tokens", 0)
            _last_prompt_tokens = prompt_tokens_this_call  # track actual context fill for budget check

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # Check for verdict in content
            vote = self._extract_vote(content)
            if vote:
                return LifeResult(
                    findings=vote.get("findings", ""),
                    vote=vote,
                    turns_used=turns_used,
                    prompt_tokens=life_prompt_tokens,
                    completion_tokens=life_completion_tokens,
                )

            # If the LLM self-reported CONTEXT_TOO_LARGE, exit immediately without
            # dispatching the tool calls — they would only grow an already-oversized context.
            if content and '"CONTEXT_TOO_LARGE"' in content:
                logger.warning(
                    "Research agent CONTEXT_TOO_LARGE signal (life %d, turn %d) — exiting immediately",
                    life_num, turns_used,
                )
                return LifeResult(
                    findings=f"Life {life_num} self-reported CONTEXT_TOO_LARGE at turn {turns_used}.",
                    vote={
                        "verdict": "TOO_LARGE",
                        "confidence": 100,
                        "justification": "Agent signalled CONTEXT_TOO_LARGE — not dispatching further tool calls.",
                        "findings": f"Context too large at turn {turns_used} of life {life_num}.",
                    },
                    turns_used=turns_used,
                    prompt_tokens=life_prompt_tokens,
                    completion_tokens=life_completion_tokens,
                )

            # Dispatch tool calls (suppress if budget exceeded - force verdict path)
            if tool_calls:
                if budget_exceeded:
                    # Return error results for each tool call so the conversation
                    # stays structurally valid, but don't execute the tools.
                    error_results = [
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "name": tc.get("function", {}).get("name", "unknown"),
                            "content": "ERROR: Token budget exceeded. No tool calls allowed. Render your verdict now.",
                        }
                        for tc in tool_calls
                    ]
                    messages.extend(error_results)
                else:
                    tool_results = self._handle_tool_calls(tool_calls)
                    messages.extend(tool_results)
                continue

            # Context saturation check - only reached when no verdict and no tool calls
            if check_context_saturation(
                prompt_tokens_this_call, self.max_context, _ctx_warned, messages
            ):
                logger.warning(
                    "Research agent context saturation (life %d, turn %d) - terminating life gracefully",
                    life_num, turns_used,
                )
                break  # falls through to _post_mortem_call()

            # No tool calls and no verdict - nudge
            if not tool_calls and not vote:
                remaining = self.max_turns_per_life - turns_used
                if remaining <= 3:
                    messages.append({
                        "role": "user",
                        "content": (
                            f"[SYSTEM] You have {remaining} turns remaining. "
                            "You MUST render your JSON verdict now based on what you've found so far."
                        ),
                    })
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] You did not call any tool and did not render a verdict. "
                            "Either call a tool to investigate further, or output your JSON verdict."
                        ),
                    })

        # Turn cap hit without verdict - fire a post-mortem call to capture a structured
        # handoff summary for the next life.  Tokens are tracked inside the method.
        handoff_summary = await self._post_mortem_call(life_num, messages)
        return LifeResult(
            findings=f"Life {life_num} exhausted {turns_used} turns without rendering a verdict.",
            vote=None,
            turns_used=turns_used,
            prompt_tokens=life_prompt_tokens,
            completion_tokens=life_completion_tokens,
            handoff_summary=handoff_summary,
        )

    # ------------------------------------------------------------------
    # Post-mortem
    # ------------------------------------------------------------------

    async def _post_mortem_call(self, life_num: int, messages: list[dict]) -> str:
        """
        One extra no-tools LLM call after turn exhaustion.  Asks the agent to
        produce a structured handoff summary so the next life starts with richer
        context than "exhausted N turns without a verdict."

        Tokens are accumulated into the agent-level totals.  Returns an empty
        string on failure - the caller always gets a LifeResult regardless.
        """
        post_mortem_messages = list(messages) + [
            {"role": "user", "content": _POST_MORTEM_PROMPT}
        ]
        try:
            response = await call_llm(
                post_mortem_messages,
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=INTAKE_LLM_TEMPERATURE,
                max_tokens=1024,
                task_id=self.task_id,
                llm_id=self.llm_id,
                agent_name=AGENT_NAME,
                budget_id=self.budget_id,
            )
            usage = response.get("usage", {})
            self._total_prompt_tokens += usage.get("prompt_tokens", 0)
            self._total_completion_tokens += usage.get("completion_tokens", 0)
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            logger.debug("Research agent post-mortem (life %d): %s", life_num, content[:200])
            return content.strip()
        except Exception as exc:
            if is_shutting_down():
                raise
            logger.warning("Research agent post-mortem call failed (life %d): %s", life_num, exc)
            return ""

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_life_context(self, life_num: int) -> str:
        """Build the user message for a given life."""
        if life_num == 1:
            parts: list[str] = []

            # Project file structure snapshot
            if self.project_root:
                try:
                    from app.agent.project_snapshot import build_snapshot_with_summaries
                    from app.agent.config import SNAPSHOT_CONTEXT_RATIO
                    _snap_max = (
                        int(self.max_context * SNAPSHOT_CONTEXT_RATIO)
                        if self.max_context else None
                    )
                    parts.append(build_snapshot_with_summaries(self.project_root, max_tokens=_snap_max))
                except Exception:
                    pass

            # Architecture / constraint cards for this project
            if self.task_id:
                try:
                    from app.database import get_task as _get_task
                    from app.agent.project_snapshot import build_architecture_context
                    _task_rec = _get_task(self.task_id)
                    if _task_rec and _task_rec.project:
                        _arch = build_architecture_context(_task_rec.project, agent_type='research')
                        if _arch:
                            parts.append(_arch)
                except Exception:
                    pass

            parts.append(f"## Investigation Question\n{self.question}")
            parts.append(f"\n## Context\n```json\n{json.dumps(self.context, indent=2, default=str)}\n```")
        else:
            parts = [f"## Investigation Question (continued - life {life_num}/{self.max_lives})\n{self.question}"]
            parts.append("\n## Previous Investigation Findings")
            for i, (findings, summary) in enumerate(
                zip_longest(self._accumulated_findings, self._accumulated_summaries, fillvalue=""),
                1,
            ):
                parts.append(f"\n### Life {i} of {self.max_lives}")
                # Prefer the structured post-mortem summary over the raw findings line
                parts.append(summary if summary else findings)

            # Surface the last "still unresolved" section as a direct focus hint
            last_summary = self._accumulated_summaries[-1] if self._accumulated_summaries else ""
            unresolved = _extract_section(last_summary, "WHAT I AM UNSATISFIED WITH")
            if unresolved:
                parts.append(f"\n## Still Unresolved (from previous life)\n{unresolved}")

            is_final = life_num == self.max_lives
            parts.append(
                "\n## Instructions\n"
                "Continue investigating based on the findings above. "
                "Focus on resolving the outstanding questions. "
                f"This is life {life_num} of {self.max_lives} - "
                f"{'you MUST render a verdict this time.' if is_final else 'investigate further or render your verdict.'}"
            )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict], no_tools: bool = False) -> dict:
        """POST to the LLM endpoint with restricted tool schemas."""
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=INTAKE_LLM_TEMPERATURE,
            tools=None if no_tools else self._restricted_schemas,
            tool_choice=None if no_tools else "auto",
            task_id=self.task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
            agent_name=AGENT_NAME,
        )

    # ------------------------------------------------------------------
    # Tool call handling (restricted)
    # ------------------------------------------------------------------

    def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
        """Dispatch tool calls, but only if they're in the restricted set."""
        results = []
        for tc in tool_calls:
            tool_id = tc.get("id", "unknown")
            function_block = tc.get("function", {})
            name = function_block.get("name", "")
            raw_args = function_block.get("arguments", "{}")

            try:
                arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
            except json.JSONDecodeError:
                arguments = {}

            if name not in RESEARCH_AGENT_TOOLS:
                result_content = f"ERROR: Tool '{name}' is not available to the research agent. Available: {RESEARCH_AGENT_TOOLS}"
            else:
                result_content = dispatch_tool(name, arguments)

            results.append({
                "role": "tool",
                "tool_call_id": tool_id,
                "name": name,
                "content": result_content,
            })

        return results

    # ------------------------------------------------------------------
    # Vote extraction
    # ------------------------------------------------------------------

    def _extract_vote(self, content: str) -> dict | None:
        """Try to extract a verdict JSON from the assistant's content."""
        raw = extract_json_block(content)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict) and "verdict" in parsed and "confidence" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return None


# ---------------------------------------------------------------------------
# Investigation Agent - open-ended research, no verdict required
# ---------------------------------------------------------------------------

_INVESTIGATION_SYSTEM_PROMPT = """You are an **Investigation Agent** inside the Maestro Orchestrator.

Your job is to INVESTIGATE a question thoroughly and produce a detailed report. Unlike the
Research Agent, you are NOT asked for a feasibility verdict — just comprehensive findings.

== TOOLS AVAILABLE ==
- read_file(path): Read a file's contents
- search_files(pattern, directory): Regex search across file contents
- find_files(glob_pattern, directory): Find files by name pattern
- list_directory(path): List directory contents
- git_status(): Show current git status
- git_diff(path?): Show git diff (optionally scoped to a file)
- git_log(path?, max_count?): Show recent git history (optionally scoped to a file)
- git_blame(path): Show git blame for a file

== YOUR WORKFLOW ==
1. Read the investigation question carefully.
2. Use tools to explore the codebase and gather concrete evidence.
3. When you have enough information, produce a structured JSON report.

== OUTPUT FORMAT ==
When ready, output a JSON block with this exact structure:
```json
{
  "answer": "<direct answer to the question in 1-3 sentences>",
  "key_findings": ["<finding 1>", "<finding 2>", "..."],
  "evidence": ["<file:line or quote 1>", "<evidence 2>", "..."],
  "gaps": ["<unanswered question 1>", "..."],
  "recommendation": "<what should be done next, if anything>"
}
```

== RULES ==
- Be specific: cite exact file paths, function names, and line numbers.
- Do not pad the report with obvious filler. Quality over quantity.
- If you cannot find enough information, say so explicitly in `gaps`.
- Do NOT include a "verdict" field — this is an investigation, not a vote.
"""


@dataclass(slots=True)
class InvestigationResult:
    """Final outcome of an ad-hoc investigation run."""
    report: dict          # {answer, key_findings, evidence, gaps, recommendation}
    lives_used: int
    total_turns: int
    raw_findings: str     # accumulated free-text findings across lives
    prompt_tokens: int = 0
    completion_tokens: int = 0


class InvestigationAgent:
    """
    Open-ended investigation agent for ad-hoc research triggered from the card toolbar.

    Unlike ResearchAgent, this agent produces a structured report rather than a
    feasibility verdict.  It uses the same tool set and lives system.

    Usage::

        agent = InvestigationAgent(
            question="How does the scheduler prioritise arch_gen_jobs?",
            context={"task_id": "...", "task_title": "...", "task_description": "..."},
        )
        result = await agent.run()
        # result.report = {"answer": "...", "key_findings": [...], ...}
    """

    def __init__(
        self,
        question: str,
        context: dict[str, Any],
        max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
        max_lives: int | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        task_id: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        max_context: int = 0,
        project_root: str | None = None,
    ) -> None:
        self.question = question
        self.context = context
        self.max_turns_per_life = max_turns_per_life
        self.max_lives = max_lives or RESEARCH_AGENT_MAX_LIVES
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.task_id = task_id
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_context = max_context
        self.project_root = project_root

        self._has_source = _has_meaningful_source_files(self.project_root or PROJECT_ROOT)
        self._restricted_schemas = _build_restricted_schemas(self._has_source)
        self._allowed_tools = {s["function"]["name"] for s in self._restricted_schemas}

        self._accumulated_findings: list[str] = []
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    async def run(self) -> InvestigationResult:
        """Execute investigation across up to max_lives. Returns InvestigationResult."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(_INVESTIGATION_AGENT_NAME)
        total_turns = 0
        final_report: dict = {}

        for life_num in range(1, self.max_lives + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            logger.info(
                "Investigation agent life %d/%d for: %s",
                life_num, self.max_lives, self.question[:80],
            )

            life_context = self._build_life_context(life_num)
            report, turns, pt, ct, findings = await self._run_life(life_context, life_num)

            total_turns += turns
            self._total_prompt_tokens += pt
            self._total_completion_tokens += ct
            if findings:
                self._accumulated_findings.append(f"[Life {life_num}] {findings}")

            if report:
                final_report = report
                break

        if not final_report:
            # Synthesise a minimal report from accumulated findings
            accumulated = "\n\n".join(self._accumulated_findings) or "No findings recorded."
            final_report = {
                "answer": "Investigation exhausted without producing a structured report.",
                "key_findings": [accumulated[:2000]],
                "evidence": [],
                "gaps": ["Investigation budget was insufficient to reach a conclusion."],
                "recommendation": "Retry with a more specific question or a higher turn budget.",
            }

        return InvestigationResult(
            report=final_report,
            lives_used=min(self.max_lives, life_num),
            total_turns=total_turns,
            raw_findings="\n\n".join(self._accumulated_findings),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
        )

    def _build_life_context(self, life_num: int) -> str:
        """Build the user-turn context injected at the start of each life."""
        from app.agent.project_snapshot import build_architecture_context
        parts: list[str] = []

        # Architecture context on first life
        if life_num == 1 and self.task_id:
            task_project = self.context.get("task_project")
            if task_project:
                arch_ctx = build_architecture_context(task_project, agent_type="research")
                if arch_ctx:
                    parts.append(arch_ctx)

        task_desc = self.context.get("task_description", "")
        parts.append(
            f"== TASK CONTEXT ==\n"
            f"Task: {self.context.get('task_title', 'Unknown')}\n"
            f"Description: {task_desc[:1000]}\n"
            f"Stage: {self.context.get('task_type', 'unknown')}"
        )

        if life_num > 1 and self._accumulated_findings:
            prev = "\n\n".join(self._accumulated_findings)
            parts.append(
                f"== FINDINGS FROM PREVIOUS LIVES ==\n{prev}\n\n"
                f"Continue investigating. Focus on gaps not yet resolved."
            )

        parts.append(f"== INVESTIGATION QUESTION ==\n{self.question}")
        return "\n\n".join(parts)

    async def _run_life(
        self, context: str, life_num: int
    ) -> tuple[dict | None, int, int, int, str]:
        """Run one investigation life. Returns (report, turns, prompt_tokens, completion_tokens, findings_text)."""
        messages = [
            {"role": "system", "content": _INVESTIGATION_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]

        turns_used = 0
        life_prompt_tokens = 0
        life_completion_tokens = 0
        _ctx_warned: set[float] = set()
        _last_prompt_tokens = 0  # actual context size from the most recent LLM call
        findings_text = ""

        for turn in range(self.max_turns_per_life):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            turns_used += 1
            budget_tokens = int(self.max_context * RESEARCH_CONTEXT_BUDGET_RATIO) if self.max_context else 0
            budget_exceeded = budget_tokens > 0 and _last_prompt_tokens >= budget_tokens

            if budget_exceeded:
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM] TOKEN BUDGET EXCEEDED. Produce your JSON report now. No more tool calls.",
                })

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=INTAKE_LLM_TEMPERATURE,
                    tools=None if budget_exceeded else self._restricted_schemas,
                    tool_choice=None if budget_exceeded else "auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=_INVESTIGATION_AGENT_NAME,
                )
            except Exception as exc:
                if is_shutting_down():
                    raise
                turns_used -= 1
                logger.error("Investigation agent LLM call failed (life %d, turn %d): %s", life_num, turns_used, exc)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Try a different approach or produce your report.",
                })
                continue

            usage = response.get("usage", {})
            _last_prompt_tokens = usage.get("prompt_tokens", 0)
            life_prompt_tokens += _last_prompt_tokens
            life_completion_tokens += usage.get("completion_tokens", 0)

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # Look for structured JSON report
            report = self._extract_report(content)
            if report:
                findings_text = content
                return report, turns_used, life_prompt_tokens, life_completion_tokens, findings_text

            # Dispatch tool calls
            if tool_calls:
                if budget_exceeded:
                    messages.extend([
                        {
                            "role": "tool",
                            "tool_call_id": tc.get("id", "unknown"),
                            "name": tc.get("function", {}).get("name", "unknown"),
                            "content": "ERROR: Token budget exceeded. Produce your JSON report now.",
                        }
                        for tc in tool_calls
                    ])
                else:
                    results = []
                    for tc in tool_calls:
                        tool_id = tc.get("id", "unknown")
                        fn = tc.get("function", {})
                        name = fn.get("name", "")
                        raw_args = fn.get("arguments", "{}")
                        try:
                            arguments = json.loads(raw_args) if isinstance(raw_args, str) else raw_args
                        except json.JSONDecodeError:
                            arguments = {}
                        if name not in self._allowed_tools:
                            result_content = f"ERROR: Tool '{name}' is not available. Available: {sorted(self._allowed_tools)}"
                        else:
                            result_content = dispatch_tool(name, arguments)
                        results.append({
                            "role": "tool",
                            "tool_call_id": tool_id,
                            "name": name,
                            "content": result_content,
                        })
                    messages.extend(results)
                continue

            # Saturation check
            if check_context_saturation(_last_prompt_tokens, self.max_context, _ctx_warned, messages):
                logger.warning("Investigation agent context saturation (life %d, turn %d)", life_num, turns_used)
                break

            # Nudge
            remaining = self.max_turns_per_life - turns_used
            if remaining <= 3:
                messages.append({
                    "role": "user",
                    "content": (
                        f"[SYSTEM] You have {remaining} turns remaining. "
                        "You MUST output your JSON report now."
                    ),
                })
            elif content:
                findings_text = content
            else:
                messages.append({
                    "role": "user",
                    "content": (
                        "[SYSTEM] You did not call any tool and did not produce a report. "
                        "Either call a tool to investigate further, or output your JSON report."
                    ),
                })

        return None, turns_used, life_prompt_tokens, life_completion_tokens, findings_text

    def _extract_report(self, content: str) -> dict | None:
        """Try to extract an investigation report JSON from the assistant's content."""
        raw = extract_json_block(content)
        if raw is None:
            return None
        try:
            parsed = json.loads(raw.strip())
            if isinstance(parsed, dict) and "answer" in parsed:
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        return None


# ---------------------------------------------------------------------------
# Web Search Agent - Synthesis and Fetching
# ---------------------------------------------------------------------------

_WEB_SEARCH_SYSTEM_PROMPT = """You are a **Web Search Synthesis Agent**.
Your job is to find the answer to a specific user query by analyzing web search results.

== CONTEXT ==
You will receive:
1. The original user query.
2. A list of search result hits (titles, URLs, and snippets).

== YOUR MISSION ==
1.  Review the search snippets.
2.  Use the `web_fetch` tool to read the full content of the most promising hits (up to 5).
3.  **QUIT EARLY** if you find a definitive answer to the user's query.
4.  Produce a highly compact Markdown summary of the facts you found.

== RULES ==
- Use `web_fetch(url)` to get the actual content of a page.
- Focus exclusively on the user's query. Ignore irrelevant content.
- Format your final response as a Markdown summary.
- Do NOT return a JSON verdict like the Research Agent. Output prose Markdown.
- If you find the answer, end your response with: "== ANSWER FOUND =="
"""

class WebSearchAgent:
    """
    Agent that visits web results and synthesizes a summary.
    """
    def __init__(
        self,
        query: str,
        results: list[dict],
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        task_id: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ) -> None:
        self.query = query
        self.results = results
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.task_id = task_id
        self.llm_id = llm_id
        self.budget_id = budget_id

    async def run(self) -> str:
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(_WEB_SEARCH_AGENT_NAME)
        messages = [
            {"role": "system", "content": _WEB_SEARCH_SYSTEM_PROMPT},
            {"role": "user", "content": f"Query: {self.query}\n\nSearch Results:\n{json.dumps(self.results, indent=2)}"}
        ]

        # Allowed tools: only web_fetch
        fetch_schema = [s for s in TOOL_SCHEMAS if s["function"]["name"] == "web_fetch"]

        max_turns = 10
        total_prompt_tokens = 0
        total_completion_tokens = 0

        for turn in range(max_turns):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            response = await call_llm(
                messages,
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=0.1,
                tools=fetch_schema,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=_WEB_SEARCH_AGENT_NAME,
            )

            usage = response.get("usage", {})
            total_prompt_tokens += usage.get("prompt_tokens", 0)
            total_completion_tokens += usage.get("completion_tokens", 0)

            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_msg)

            tool_calls = assistant_msg.get("tool_calls") or []
            if not tool_calls:
                # No more tools - this is the final synthesis
                return assistant_msg.get("content", "").strip()

            # Execute tool calls
            for tc in tool_calls:
                t_id = tc.get("id", "unknown")
                f_block = tc.get("function", {})
                name = f_block.get("name", "")
                args = json.loads(f_block.get("arguments", "{}"))

                if name == "web_fetch":
                    result = dispatch_tool(name, args)
                else:
                    result = f"ERROR: Tool '{name}' not available to WebSearchAgent."

                messages.append({
                    "role": "tool",
                    "tool_call_id": t_id,
                    "name": name,
                    "content": result,
                })

            # Check for early exit signal in content (if any)
            content = assistant_msg.get("content", "")
            if "== ANSWER FOUND ==" in content:
                # Need one more turn to produce the final Markdown without tool calls?
                # Actually if they said it in the content but also made tool calls,
                # we should probably continue. If they JUST said it, they are done.
                if not tool_calls:
                    return content.strip()

        return messages[-1].get("content", "Failed to synthesize web results.")


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

async def run_research(
    question: str,
    context: dict[str, Any],
    max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    max_lives: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_root: str | None = None,
) -> ResearchResult:
    """Run a research agent and return its result."""
    _max_context = 0
    if llm_id is not None:
        _llm_record = get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    agent = ResearchAgent(
        question=question,
        context=context,
        max_turns_per_life=max_turns_per_life,
        max_lives=max_lives,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        task_id=task_id,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=_max_context,
        project_root=project_root,
    )
    return await agent.run()


async def run_investigation(
    question: str,
    context: dict[str, Any],
    max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    max_lives: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_root: str | None = None,
) -> InvestigationResult:
    """Run an investigation agent and return its structured report."""
    _max_context = 0
    if llm_id is not None:
        _llm_record = get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    agent = InvestigationAgent(
        question=question,
        context=context,
        max_turns_per_life=max_turns_per_life,
        max_lives=max_lives,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        task_id=task_id,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=_max_context,
        project_root=project_root,
    )
    return await agent.run()


async def run_tiebreaker(
    task_description: str,
    votes: list[dict],
    max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    max_lives: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    task_id: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> ResearchResult:
    """Run a tie-breaker research agent with all voter context."""
    # Build the investigation question from the disagreement
    pass_votes = [v for v in votes if v.get("verdict") in ("POSSIBLE", "LIKELY")]
    fail_votes = [v for v in votes if v.get("verdict") in ("REJECTED", "NOT_SUITABLE")]

    question = (
        f"The following task advancement vote resulted in a tie "
        f"({len(pass_votes)} pass vs {len(fail_votes)} fail). "
        f"Investigate the specific points of disagreement and cast a deciding vote.\n\n"
        f"Task: {task_description}"
    )

    context = {
        "task_description": task_description,
        "pass_votes": pass_votes,
        "fail_votes": fail_votes,
        "all_votes": votes,
    }

    _max_context = 0
    if llm_id is not None:
        _llm_record = get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    agent = ResearchAgent(
        question=question,
        context=context,
        max_turns_per_life=max_turns_per_life,
        max_lives=max_lives,
        is_tiebreaker=True,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        task_id=task_id,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=_max_context,
    )
    return await agent.run()
