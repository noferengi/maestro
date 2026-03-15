"""
app/agent/research.py
---------------------
Research Agent — a lightweight agentic loop for investigating unknowns.

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
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    LLM_TIMEOUT_SECONDS,
    MAX_TOKENS_PER_TURN,
    INTAKE_LLM_TEMPERATURE,
    RESEARCH_AGENT_MAX_LIVES,
    RESEARCH_AGENT_TOOLS,
)
from app.agent.tools import TOOL_SCHEMAS, TOOL_REGISTRY, dispatch_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Restricted tool schemas — only the tools a research agent is allowed to use
# ---------------------------------------------------------------------------

def _build_restricted_schemas() -> list[dict]:
    """Filter TOOL_SCHEMAS to only include tools in RESEARCH_AGENT_TOOLS."""
    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in RESEARCH_AGENT_TOOLS
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
- REJECTED: [0, 50] — fundamental blocker found
- NOT_SUITABLE: [51, 60] — task is poorly scoped or inappropriate
- NEEDS_RESEARCH: [61, 75] — still insufficient info (only use if you truly cannot determine)
- POSSIBLE: [76, 91] — can probably be done
- LIKELY: [92, 100] — high confidence it can be accomplished

== RULES ==
- Be thorough but efficient. You have limited turns.
- Do NOT attempt to write or modify any files.
- Do NOT output free-form prose as your final action — always end with the JSON verdict.
- If you cannot determine feasibility, say so honestly with NEEDS_RESEARCH.
- Focus on evidence from the actual code, not assumptions.
"""

_TIEBREAKER_SYSTEM_PROMPT = """You are a **Tie-Breaker Research Agent** inside the Maestro Orchestrator.

A task advancement vote resulted in a tie — the voters are split on whether this task
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
        max_turns_per_life: int = 20,
        max_lives: int | None = None,
        is_tiebreaker: bool = False,
    ) -> None:
        self.question = question
        self.context = context
        self.max_turns_per_life = max_turns_per_life
        self.max_lives = max_lives or RESEARCH_AGENT_MAX_LIVES
        self.is_tiebreaker = is_tiebreaker

        self._restricted_schemas = _build_restricted_schemas()
        self._accumulated_findings: list[str] = []
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    async def run(self) -> ResearchResult:
        """Execute the research agent across all lives. Returns a ResearchResult."""
        total_turns = 0

        for life_num in range(1, self.max_lives + 1):
            logger.info("Research agent life %d/%d for: %s", life_num, self.max_lives, self.question[:80])

            life_context = self._build_life_context(life_num)
            life_result = await self._run_life(life_context, life_num)

            total_turns += life_result.turns_used
            self._total_prompt_tokens += life_result.prompt_tokens
            self._total_completion_tokens += life_result.completion_tokens

            if life_result.findings:
                self._accumulated_findings.append(f"[Life {life_num}] {life_result.findings}")

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

        # All lives exhausted — return the last vote or a fallback
        fallback_vote = {
            "verdict": "NOT_SUITABLE",
            "confidence": 55,
            "justification": (
                f"Research agent exhausted {self.max_lives} lives without reaching a confident verdict. "
                f"Accumulated findings: {'; '.join(self._accumulated_findings) or 'none'}"
            ),
            "findings": "\n\n".join(self._accumulated_findings),
        }

        return ResearchResult(
            vote=fallback_vote,
            lives_used=self.max_lives,
            total_turns=total_turns,
            findings="\n\n".join(self._accumulated_findings),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
        )

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

        for turn in range(self.max_turns_per_life):
            turns_used += 1

            # LLM call
            try:
                response = await self._call_llm(messages)
            except Exception as exc:
                logger.error("Research agent LLM call failed (life %d, turn %d): %s", life_num, turns_used, exc)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Try a different approach or render your verdict now.",
                })
                continue

            usage = response.get("usage", {})
            life_prompt_tokens += usage.get("prompt_tokens", 0)
            life_completion_tokens += usage.get("completion_tokens", 0)

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

            # Dispatch tool calls
            if tool_calls:
                tool_results = self._handle_tool_calls(tool_calls)
                messages.extend(tool_results)
                continue

            # No tool calls and no verdict — nudge
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

        # Turn cap hit without verdict
        return LifeResult(
            findings=f"Life {life_num} exhausted {self.max_turns_per_life} turns without rendering a verdict.",
            vote=None,
            turns_used=turns_used,
            prompt_tokens=life_prompt_tokens,
            completion_tokens=life_completion_tokens,
        )

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_life_context(self, life_num: int) -> str:
        """Build the user message for a given life."""
        parts = []

        if life_num == 1:
            parts.append(f"## Investigation Question\n{self.question}")
            parts.append(f"\n## Context\n```json\n{json.dumps(self.context, indent=2, default=str)}\n```")
        else:
            parts.append(f"## Investigation Question (continued — life {life_num}/{self.max_lives})\n{self.question}")
            parts.append("\n## Previous Investigation Findings")
            for finding in self._accumulated_findings:
                parts.append(finding)
            parts.append(
                "\n## Instructions\n"
                "Continue investigating based on the findings above. "
                "Focus on resolving the remaining unknowns. "
                f"This is life {life_num} of {self.max_lives} — "
                f"{'you must render a verdict this time.' if life_num == self.max_lives else 'investigate further or render your verdict.'}"
            )

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        """POST to the LLM endpoint with restricted tool schemas."""
        payload = {
            "model": LLM_MODEL,
            "messages": messages,
            "tools": self._restricted_schemas,
            "tool_choice": "auto",
            "temperature": INTAKE_LLM_TEMPERATURE,
            "max_tokens": MAX_TOKENS_PER_TURN,
        }

        async with httpx.AsyncClient(timeout=LLM_TIMEOUT_SECONDS) as client:
            response = await client.post(
                f"{LLM_BASE_URL}/chat/completions",
                json=payload,
                headers={"Content-Type": "application/json"},
            )
            response.raise_for_status()
            return response.json()

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
        if not content:
            return None

        # Try fenced JSON block first
        import re
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if fenced:
            try:
                parsed = json.loads(fenced.group(1))
                if "verdict" in parsed and "confidence" in parsed:
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass

        # Try bare JSON object
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            try:
                parsed = json.loads(content[start:end + 1])
                if "verdict" in parsed and "confidence" in parsed:
                    return parsed
            except (json.JSONDecodeError, ValueError):
                pass

        return None


# ---------------------------------------------------------------------------
# Convenience functions
# ---------------------------------------------------------------------------

async def run_research(
    question: str,
    context: dict[str, Any],
    max_turns_per_life: int = 20,
    max_lives: int | None = None,
) -> ResearchResult:
    """Run a research agent and return its result."""
    agent = ResearchAgent(
        question=question,
        context=context,
        max_turns_per_life=max_turns_per_life,
        max_lives=max_lives,
    )
    return await agent.run()


async def run_tiebreaker(
    task_description: str,
    votes: list[dict],
    max_turns_per_life: int = 20,
    max_lives: int | None = None,
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

    agent = ResearchAgent(
        question=question,
        context=context,
        max_turns_per_life=max_turns_per_life,
        max_lives=max_lives,
        is_tiebreaker=True,
    )
    return await agent.run()
