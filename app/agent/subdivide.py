"""
app/agent/subdivide.py
----------------------
Subdivision Agent — decomposes oversized ideas into smaller sub-ideas.

Invoked by the intake pipeline when any stage votes SUBDIVIDE_IDEA.
Follows the ResearchAgent pattern: restricted tools, structured output,
configurable turns.

Now includes:
  - Context-aware tool selection (greenfield vs existing codebase)
  - Planning tools (architecture docs, mermaid diagrams, interface contracts)
  - Async tool dispatch (for spawn_research_agent)
  - Context window budget enforcement
  - Interface contract output parsing
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    PROJECT_ROOT,
    SUBDIVISION_AGENT_MAX_TURNS,
    SUBDIVISION_LLM_TEMPERATURE,
    SUBDIVISION_AGENT_TOOLS,
    SUBDIVISION_PLANNING_TOOLS,
    SUBDIVISION_CONTEXT_BUDGET_RATIO,
    SUBDIVISION_CONTEXT_AWARE_TOOLS,
    check_context_saturation,
)
from app.agent.llm_client import call_llm
from app.agent.tools import TOOL_SCHEMAS, async_dispatch_tool, LISTING_EXCLUDED_DIRS

logger = logging.getLogger(__name__)


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
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in LISTING_EXCLUDED_DIRS]
        for fname in files:
            ext = os.path.splitext(fname)[1].lower()
            if ext in _SOURCE_EXTENSIONS:
                return True
    return False


# ---------------------------------------------------------------------------
# Context-aware tool schema builder
# ---------------------------------------------------------------------------

def _build_context_aware_schemas(
    has_source: bool | None = None,
) -> tuple[list[dict], list[str]]:
    """Build tool schemas based on project context.

    Returns (schemas, allowed_tool_names).

    - Always includes planning tools + list_directory, find_files, get_task, list_tasks
    - If source files exist: also includes full codebase read tools
    - If greenfield: skips codebase read tools
    """
    if has_source is None:
        has_source = _has_meaningful_source_files()

    # Planning tools are always available
    allowed = set(SUBDIVISION_PLANNING_TOOLS)

    if has_source and SUBDIVISION_CONTEXT_AWARE_TOOLS:
        # Add all codebase read tools
        allowed.update(SUBDIVISION_AGENT_TOOLS)
    elif not SUBDIVISION_CONTEXT_AWARE_TOOLS:
        # Context-aware disabled — always include all tools
        allowed.update(SUBDIVISION_AGENT_TOOLS)

    allowed_list = sorted(allowed)

    schemas = [
        schema for schema in TOOL_SCHEMAS
        if schema.get("function", {}).get("name") in allowed
    ]
    return schemas, allowed_list


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class SubIdeaSpec:
    """Specification for a single sub-idea produced by decomposition."""
    title: str
    description: str
    prerequisites: list[str] = field(default_factory=list)
    estimated_scope: str = "medium"  # small | medium
    rationale: str = ""
    provides: list[dict] = field(default_factory=list)
    consumes: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class SubdivisionResult:
    """Final outcome of a subdivision agent run."""
    sub_ideas: list[SubIdeaSpec]
    decomposition_rationale: str
    coverage_check: str
    confidence: int
    interface_contracts: list[dict] = field(default_factory=list)
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
You have access to tools for investigating the project and planning the decomposition.

**Planning tools** (always available):
- `generate_architecture_doc`: Structure your thinking about components and relationships
- `generate_interface_contract`: Define API surfaces between sub-ideas (types, schemas, method signatures)
- `generate_mermaid_diagram`: Create visual diagrams (flowchart, sequence, class, ER)
- `spawn_research_agent`: Investigate unfamiliar technologies or domain questions

**Investigation tools** (always available):
- `list_directory`, `find_files`, `get_task`, `list_tasks`

{codebase_tools_section}

== PLANNING GUIDANCE ==
Use `generate_architecture_doc` to structure your thinking BEFORE decomposing.
Use `generate_interface_contract` to define the API surface between sub-ideas.
Use `spawn_research_agent` when you need domain knowledge about unfamiliar technologies.

== DECOMPOSITION RULES ==
1. Produce 2-7 sub-ideas. Each must be independently implementable and testable.
2. Specify prerequisites between sub-ideas using their index (e.g., "sub-0" means
   the first sub-idea in your list must be completed first).
3. Each sub-idea should fit within a single LLM context window for implementation.
4. Every sub-idea must have a clear "done" condition.
5. The union of all sub-ideas must cover the original task completely — nothing lost.
6. For greenfield projects, begin with foundational horizontal slices (project scaffolding,
   data model, SDK/API integration, build system), then decompose remaining work into
   vertical feature slices. For existing codebases, prefer vertical slices that cut
   across layers. Document your reasoning in decomposition_rationale.
7. Estimated scope should be "small" or "medium" — if you'd estimate "large",
   the sub-idea itself may need further decomposition.

== OUTPUT FORMAT ==
When you are ready, output ONLY this JSON object (no markdown fences, no extra text):

{{
  "sub_ideas": [
    {{
      "title": "Short descriptive title",
      "description": "Full description of what this sub-idea entails",
      "prerequisites": ["sub-0"],
      "estimated_scope": "small" | "medium",
      "rationale": "Why this is a coherent, independent unit of work",
      "provides": [{{"name": "...", "type": "...", "description": "..."}}],
      "consumes": [{{"name": "...", "type": "...", "source": "sub-N"}}]
    }}
  ],
  "interface_contracts": [
    {{
      "component": "sub-0 title",
      "provides": [{{"name": "...", "type": "..."}}],
      "consumes": [{{"name": "...", "type": "...", "source": "sub-N"}}]
    }}
  ],
  "decomposition_rationale": "Why you chose this particular decomposition strategy",
  "coverage_check": "How the sub-ideas together cover the entire original task",
  "confidence": 85
}}

== CONTEXT BUDGET ==
Your total token budget is ~{token_budget} tokens ({budget_pct}% of {max_context}).
Plan tool calls efficiently.

== RULES ==
- Be thorough but efficient. You have limited turns.
- Do NOT attempt to write or modify any files.
- Focus on evidence from the actual code (if available) and sound architectural reasoning.
- If you cannot confidently decompose the task, set confidence below 50 and explain why.
"""

_CODEBASE_TOOLS_AVAILABLE = """\
**Codebase tools** (project has existing source code):
- `read_file`, `read_file_lines`, `count_lines`, `search_files`
- `git_status`, `git_diff`, `git_log`, `git_blame`, `git_show`
Use these to understand the current code structure before deciding how to split the task."""

_CODEBASE_TOOLS_UNAVAILABLE = """\
**Note**: This appears to be a greenfield project (no existing source code detected).
Codebase read tools are not available. Focus on architectural planning and
use `spawn_research_agent` for domain knowledge about unfamiliar technologies."""

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
        max_turns: int = SUBDIVISION_AGENT_MAX_TURNS,
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

        # Context-aware tool selection
        self._has_source = _has_meaningful_source_files()
        self._tool_schemas, self._allowed_tools = _build_context_aware_schemas(self._has_source)

        # Token budget enforcement
        self.token_budget = int((max_context or 100_000) * SUBDIVISION_CONTEXT_BUDGET_RATIO)
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0
        self._budget_exceeded = False

    async def run(self) -> SubdivisionResult:
        """Execute the subdivision agent and return decomposed sub-ideas."""
        system_prompt = self._build_system_prompt()
        context = self._build_context()
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context},
        ]

        _ctx_warned: set[float] = set()

        for turn in range(self.max_turns):
            # Hard budget enforcement
            if self._budget_exceeded:
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM] TOKEN BUDGET EXCEEDED. Output your JSON decomposition NOW. No more tool calls.",
                })

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
            prompt_tokens_this_call = usage.get("prompt_tokens", 0)
            self._total_prompt_tokens += prompt_tokens_this_call
            self._total_completion_tokens += usage.get("completion_tokens", 0)

            # Check budget after each call
            total_tokens = self._total_prompt_tokens + self._total_completion_tokens
            if total_tokens >= self.token_budget:
                self._budget_exceeded = True

            # Context saturation check
            if check_context_saturation(
                prompt_tokens_this_call, self.max_context or 0, _ctx_warned, messages
            ):
                logger.warning(
                    "SubdivisionAgent context saturation (turn %d) — terminating", turn + 1
                )
                break

            assistant_message = response.get("choices", [{}])[0].get("message", {})
            messages.append(assistant_message)

            tool_calls = assistant_message.get("tool_calls") or []
            content = assistant_message.get("content") or ""

            # Check for structured output
            result = self._extract_result(content)
            if result:
                return result

            # Dispatch tool calls (skip if budget exceeded)
            if tool_calls and not self._budget_exceeded:
                tool_results = await self._handle_tool_calls(tool_calls)
                messages.extend(tool_results)
                continue

            if tool_calls and self._budget_exceeded:
                # Return error for all tool calls when budget exceeded
                for tc in tool_calls:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.get("id", "unknown"),
                        "name": tc.get("function", {}).get("name", ""),
                        "content": "ERROR: Token budget exceeded. Output your decomposition NOW.",
                    })
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
    # System prompt building
    # ------------------------------------------------------------------

    def _build_system_prompt(self) -> str:
        """Build the system prompt with context-aware tool section."""
        codebase_section = (
            _CODEBASE_TOOLS_AVAILABLE if self._has_source
            else _CODEBASE_TOOLS_UNAVAILABLE
        )
        max_ctx_display = self.max_context or 100_000
        budget_pct = int(SUBDIVISION_CONTEXT_BUDGET_RATIO * 100)

        return _SUBDIVISION_SYSTEM_PROMPT.format(
            codebase_tools_section=codebase_section,
            token_budget=self.token_budget,
            budget_pct=budget_pct,
            max_context=max_ctx_display,
        )

    # ------------------------------------------------------------------
    # Context building
    # ------------------------------------------------------------------

    def _build_context(self) -> str:
        try:
            from app.agent.project_snapshot import build_snapshot_with_summaries
            snapshot = build_snapshot_with_summaries()
            parts = [snapshot, f"## Task to Decompose"]
        except Exception:
            parts = [f"## Task to Decompose"]
        parts += [
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
            tools=self._tool_schemas,
            tool_choice="auto",
            task_id=self.parent_task_id,
            llm_id=self.llm_id,
            budget_id=self.budget_id,
        )

    # ------------------------------------------------------------------
    # Tool call handling (async)
    # ------------------------------------------------------------------

    async def _handle_tool_calls(self, tool_calls: list) -> list[dict]:
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

            if name not in self._allowed_tools:
                result_content = f"ERROR: Tool '{name}' is not available to the subdivision agent."
            else:
                result_content = await async_dispatch_tool(
                    name, arguments,
                    task_id=self.parent_task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    llm_base_url=self.llm_base_url,
                    llm_model=self.llm_model,
                )

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
                provides=item.get("provides", []),
                consumes=item.get("consumes", []),
            ))

        if not sub_ideas:
            return None

        confidence = data.get("confidence", 50)
        if not isinstance(confidence, (int, float)):
            confidence = 50
        confidence = max(0, min(100, int(confidence)))

        interface_contracts = data.get("interface_contracts", [])
        if not isinstance(interface_contracts, list):
            interface_contracts = []

        return SubdivisionResult(
            sub_ideas=sub_ideas,
            decomposition_rationale=data.get("decomposition_rationale", ""),
            coverage_check=data.get("coverage_check", ""),
            confidence=confidence,
            interface_contracts=interface_contracts,
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
