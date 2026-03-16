"""
app/agent/subdivide.py
----------------------
Subdivision Agent — decomposes oversized ideas into smaller sub-ideas.

Invoked by the intake pipeline when any stage votes SUBDIVIDE_IDEA.
Follows the ResearchAgent pattern: restricted tools, structured output,
configurable turns.

On retry (after a previous decomposition's sub-ideas failed intake),
receives rejection_context with the previous decomposition and failure
details so it can try a different split strategy.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    SUBDIVISION_LLM_TEMPERATURE,
    SUBDIVISION_AGENT_TOOLS,
)
from app.agent.llm_client import call_llm
from app.agent.tools import TOOL_SCHEMAS, dispatch_tool

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Restricted tool schemas
# ---------------------------------------------------------------------------

def _build_restricted_schemas() -> list[dict]:
    """Filter TOOL_SCHEMAS to only include tools in SUBDIVISION_AGENT_TOOLS."""
    return [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in SUBDIVISION_AGENT_TOOLS
    ]


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SubIdeaSpec:
    """Specification for a single sub-idea produced by decomposition."""
    title: str
    description: str
    prerequisites: list[str] = field(default_factory=list)  # references to sibling sub-idea indices
    estimated_scope: str = "medium"  # small | medium
    rationale: str = ""


@dataclass(slots=True)
class SubdivisionResult:
    """Final outcome of a subdivision agent run."""
    sub_ideas: list[SubIdeaSpec]
    decomposition_rationale: str
    coverage_check: str
    confidence: int
    prompt_tokens: int = 0
    completion_tokens: int = 0
    raw_output: dict | None = None


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

_SUBDIVISION_SYSTEM_PROMPT = """\
You are a **Subdivision Agent** inside the Maestro Orchestrator.

A task has been evaluated by the intake pipeline and determined to be too large
for a single LLM context window. Your job is to decompose it into 2-7 smaller,
independently actionable sub-ideas.

== TOOLS AVAILABLE ==
You have read-only access to the codebase via tools. Use them to understand
the current code structure before deciding how to split the task.

== DECOMPOSITION RULES ==
1. Produce 2-7 sub-ideas. Each must be independently implementable and testable.
2. Specify prerequisites between sub-ideas using their index (e.g., "sub-0" means
   the first sub-idea in your list must be completed first).
3. Each sub-idea should fit within a single LLM context window for implementation.
4. Every sub-idea must have a clear "done" condition.
5. The union of all sub-ideas must cover the original task completely — nothing lost.
6. Prefer vertical slices (end-to-end features) over horizontal slices (layers).
7. Estimated scope should be "small" or "medium" — if you'd estimate "large",
   the sub-idea itself may need further decomposition.

== OUTPUT FORMAT ==
When you are ready, output ONLY this JSON object (no markdown fences, no extra text):

{
  "sub_ideas": [
    {
      "title": "Short descriptive title",
      "description": "Full description of what this sub-idea entails",
      "prerequisites": ["sub-0"],
      "estimated_scope": "small" | "medium",
      "rationale": "Why this is a coherent, independent unit of work"
    }
  ],
  "decomposition_rationale": "Why you chose this particular decomposition strategy",
  "coverage_check": "How the sub-ideas together cover the entire original task",
  "confidence": 85
}

== RULES ==
- Be thorough but efficient. You have limited turns.
- Do NOT attempt to write or modify any files.
- Focus on evidence from the actual code, not assumptions.
- If you cannot confidently decompose the task, set confidence below 50 and explain why.
"""

_SUBDIVISION_RETRY_CONTEXT = """\

== RETRY CONTEXT ==
A previous decomposition attempt failed. Some sub-ideas were rejected by the
intake pipeline. You MUST try a DIFFERENT decomposition strategy.

Previous attempt #{attempt_number}:
{previous_decomposition}

Rejected sub-ideas and reasons:
{rejected_details}

Sub-ideas that passed (you may keep these if they still make sense):
{passed_details}

Guidance: {guidance}
"""


# ---------------------------------------------------------------------------
# SubdivisionAgent
# ---------------------------------------------------------------------------

class SubdivisionAgent:
    """
    Decomposes oversized ideas into smaller sub-ideas.

    Usage::

        agent = SubdivisionAgent(
            parent_task_id="task-42",
            parent_title="Add full OAuth2 flow",
            parent_description="...",
            scope_vote={...},
        )
        result = await agent.run()
    """

    def __init__(
        self,
        parent_task_id: str,
        parent_title: str,
        parent_description: str,
        scope_vote: dict | None = None,
        rejection_context: dict | None = None,
        max_context: int | None = None,
        max_turns: int = 25,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ) -> None:
        self.parent_task_id = parent_task_id
        self.parent_title = parent_title
        self.parent_description = parent_description
        self.scope_vote = scope_vote
        self.rejection_context = rejection_context
        self.max_context = max_context
        self.max_turns = max_turns
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.llm_id = llm_id
        self.budget_id = budget_id

        self._restricted_schemas = _build_restricted_schemas()
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    async def run(self) -> SubdivisionResult:
        """Execute the subdivision agent and return decomposed sub-ideas."""
        context = self._build_context()
        messages = [
            {"role": "system", "content": _SUBDIVISION_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]

        for turn in range(self.max_turns):
            try:
                response = await self._call_llm(messages)
            except Exception as exc:
                logger.error("Subdivision agent LLM call failed (turn %d): %s", turn + 1, exc)
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] LLM call failed: {exc}. Try a different approach or output your decomposition now.",
                })
                continue

            usage = response.get("usage", {})
            self._total_prompt_tokens += usage.get("prompt_tokens", 0)
            self._total_completion_tokens += usage.get("completion_tokens", 0)

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # Check for structured output
            result = self._extract_result(content)
            if result:
                return result

            # Dispatch tool calls
            if tool_calls:
                tool_results = self._handle_tool_calls(tool_calls)
                messages.extend(tool_results)
                continue

            # Nudge if neither tools nor result
            remaining = self.max_turns - (turn + 1)
            if remaining <= 3:
                messages.append({
                    "role": "user",
                    "content": f"[SYSTEM] You have {remaining} turns remaining. Output your JSON decomposition NOW.",
                })
            else:
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM] You did not call any tool and did not output a decomposition. "
                               "Either call a tool to investigate, or output your JSON decomposition.",
                })

        # Exhausted turns — return a low-confidence empty result
        logger.warning("Subdivision agent exhausted %d turns for task '%s'.", self.max_turns, self.parent_task_id)
        return SubdivisionResult(
            sub_ideas=[],
            decomposition_rationale="Agent exhausted turn limit without producing decomposition.",
            coverage_check="",
            confidence=0,
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
        )

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        parts = [
            f"## Task to Decompose",
            f"**ID:** {self.parent_task_id}",
            f"**Title:** {self.parent_title}",
            f"**Description:**\n{self.parent_description}",
        ]

        if self.max_context:
            parts.append(f"\n**LLM Context Window:** {self.max_context} tokens")
            parts.append("Each sub-idea must be implementable within this context budget.")

        if self.scope_vote:
            try:
                scope_str = json.dumps(self.scope_vote, indent=2, default=str)
                parts.append(f"\n## Scope Analysis (from intake pipeline)\n```json\n{scope_str}\n```")
            except (TypeError, ValueError):
                parts.append(f"\n## Scope Analysis\n{self.scope_vote}")

        if self.rejection_context:
            rc = self.rejection_context
            prev_decomp = json.dumps(rc.get("previous_decomposition", []), indent=2, default=str)
            rejected = json.dumps(rc.get("rejected_sub_ideas", []), indent=2, default=str)
            passed = json.dumps(rc.get("passed_sub_ideas", []), indent=2, default=str)
            guidance = rc.get("guidance", "Try a different decomposition strategy.")

            retry_text = _SUBDIVISION_RETRY_CONTEXT.format(
                attempt_number=rc.get("attempt_number", "?"),
                previous_decomposition=prev_decomp,
                rejected_details=rejected,
                passed_details=passed,
                guidance=guidance,
            )
            parts.append(retry_text)

        return "\n\n".join(parts)

    # ------------------------------------------------------------------
    # LLM call
    # ------------------------------------------------------------------

    async def _call_llm(self, messages: list[dict]) -> dict:
        return await call_llm(
            messages,
            base_url=self.llm_base_url,
            model=self.llm_model,
            temperature=SUBDIVISION_LLM_TEMPERATURE,
            tools=self._restricted_schemas,
            tool_choice="auto",
            task_id=self.parent_task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
        )

    # ------------------------------------------------------------------
    # Tool call handling
    # ------------------------------------------------------------------

    def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
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

            if name not in SUBDIVISION_AGENT_TOOLS:
                result_content = f"ERROR: Tool '{name}' is not available to the subdivision agent."
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
    # Result extraction
    # ------------------------------------------------------------------

    def _extract_result(self, content: str) -> SubdivisionResult | None:
        """Try to extract a subdivision result JSON from the assistant's content."""
        if not content:
            return None

        import re

        # Try fenced JSON block first
        fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
        if fenced:
            parsed = self._try_parse(fenced.group(1))
            if parsed:
                return parsed

        # Try bare JSON object
        start = content.find("{")
        end = content.rfind("}")
        if start != -1 and end > start:
            parsed = self._try_parse(content[start:end + 1])
            if parsed:
                return parsed

        return None

    def _try_parse(self, raw: str) -> SubdivisionResult | None:
        """Attempt to parse a JSON string into a SubdivisionResult."""
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None

        if "sub_ideas" not in data:
            return None

        sub_ideas = []
        for item in data["sub_ideas"]:
            if not isinstance(item, dict):
                continue
            sub_ideas.append(SubIdeaSpec(
                title=item.get("title", "Untitled"),
                description=item.get("description", ""),
                prerequisites=item.get("prerequisites", []),
                estimated_scope=item.get("estimated_scope", "medium"),
                rationale=item.get("rationale", ""),
            ))

        if not sub_ideas:
            return None

        confidence = data.get("confidence", 50)
        if not isinstance(confidence, (int, float)):
            confidence = 50
        confidence = max(0, min(100, int(confidence)))

        return SubdivisionResult(
            sub_ideas=sub_ideas,
            decomposition_rationale=data.get("decomposition_rationale", ""),
            coverage_check=data.get("coverage_check", ""),
            confidence=confidence,
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
            raw_output=data,
        )


# ---------------------------------------------------------------------------
# Convenience function
# ---------------------------------------------------------------------------

async def run_subdivision(
    parent_task_id: str,
    parent_title: str,
    parent_description: str,
    scope_vote: dict | None = None,
    rejection_context: dict | None = None,
    max_context: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
) -> SubdivisionResult:
    """Run a subdivision agent and return its result."""
    agent = SubdivisionAgent(
        parent_task_id=parent_task_id,
        parent_title=parent_title,
        parent_description=parent_description,
        scope_vote=scope_vote,
        rejection_context=rejection_context,
        max_context=max_context,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
    )
    return await agent.run()
