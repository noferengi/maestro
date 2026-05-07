"""
app/agent/clarify.py
---------------------
Clarification Agent — rewrites an IDEA card into a design-document-quality
specification before it enters the planning pipeline.

Runs automatically when a new IDEA card is created.  Uses the InvestigationAgent
infrastructure (multi-life, read-only tool set) but with a clarification-focused
system prompt and a structured JSON output schema.

Output fields:
  rewritten_description   — full rewrite in Goal/AC/Out-of-Scope/Constraints form
  design_rationale        — why the description was restructured this way
  acceptance_criteria     — list of specific, testable criteria
  out_of_scope            — explicit scope boundary
  open_questions          — questions the agent could not resolve from context
  suggested_prerequisites — [{task_id, title, reason}, ...] — existing tasks to depend on
  suggested_subtasks      — [{title, description, order}, ...] — optional decomposition
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass

from app.agent.config import (
    LLM_BASE_URL,
    LLM_MODEL,
    RESEARCH_AGENT_MAX_LIVES,
    RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    RESEARCH_CONTEXT_BUDGET_RATIO,
    check_context_saturation,
)
from app.agent.json_utils import extract_json_block
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError, ContextTooLargeError, TaskDeactivatedError
from app.agent.research import _build_restricted_schemas, _has_meaningful_source_files
from app.agent.tools import dispatch_tool

logger = logging.getLogger(__name__)

_CLARIFICATION_AGENT_NAME = "Clarification Agent"

_CLARIFICATION_SYSTEM_PROMPT = """You are a **Clarification Agent** inside the Maestro Orchestrator.

Your job is to analyze a new IDEA card and rewrite it into a design-document-quality
specification before it enters the automated development pipeline.

A well-formed spec enables the planning pipeline to produce an accurate implementation plan,
pass the planning gate on the first attempt, and avoid mid-pipeline demotions.

== WHAT YOU RECEIVE ==
- The task's original title and description
- The project's architecture constraints
- A list of other tasks already in the project (for prerequisite suggestion)
- Access to the codebase (read-only tools) so you can verify context and find relevant files

== WHAT YOU PRODUCE ==
A single JSON object at the end of your investigation. No prose after the JSON.

{
  "rewritten_description": "Full rewrite. Must follow this exact structure:\\n**Goal:** [one sentence]\\n\\n**Acceptance Criteria:**\\n- [specific, testable]\\n- ...\\n\\n**Out of Scope:** [explicit boundary]\\n\\n**Constraints:** [technical constraints or 'None']",
  "design_rationale": "Brief explanation of why you restructured the description this way.",
  "acceptance_criteria": ["criterion 1", "criterion 2", "..."],
  "out_of_scope": "What this card explicitly does NOT do.",
  "open_questions": ["Question the user should answer before planning begins.", "..."],
  "suggested_prerequisites": [
    {"task_id": "T-xx", "title": "...", "reason": "This task modifies shared files."}
  ],
  "suggested_subtasks": [
    {"title": "...", "description": "...", "order": 1}
  ]
}

== RULES ==
- Preserve the original title verbatim. You may append [clarification] after it but never remove the original.
- Do NOT invent requirements. Only make explicit what is implicit in the description.
- If the description is already well-formed and complete, return it essentially unchanged (with minimal restructuring).
- Keep acceptance_criteria specific and testable. Avoid vague criteria like "works correctly."
- suggested_prerequisites: only include tasks that clearly must complete before this one.
  Look at the provided task list. Do not invent task IDs.
- suggested_subtasks: only suggest decomposition for large, multi-component tasks.
  For focused single-component work, leave this array empty.
- open_questions: only list genuine blockers (missing technical decision, ambiguous scope).
  Do not pad with obvious questions.
- Output the JSON inside a markdown code block: ```json ... ```

== INVESTIGATION WORKFLOW ==
1. Read the task description carefully.
2. Use tools to explore relevant parts of the codebase (look for related modules, existing patterns).
3. Review the existing task list for prerequisite candidates.
4. Produce the JSON report.
"""


@dataclass(slots=True)
class ClarificationResult:
    """Output from the clarification agent."""
    rewritten_description: str
    design_rationale: str
    acceptance_criteria: list[str]
    out_of_scope: str
    open_questions: list[str]
    suggested_prerequisites: list[dict]
    suggested_subtasks: list[dict]
    prompt_tokens: int = 0
    completion_tokens: int = 0
    lives_used: int = 0


class ClarificationAgent:
    """
    Multi-life read-only agent that rewrites an IDEA card into a structured spec.

    Follows the same pattern as InvestigationAgent but uses a clarification-focused
    system prompt and produces a ClarificationResult instead of an InvestigationResult.
    """

    def __init__(
        self,
        task_id: str,
        title: str,
        description: str,
        project_name: str,
        project_root: str,
        existing_tasks: list[dict],  # [{id, title, type}] of other project tasks
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        max_context: int = 0,
        max_lives: int | None = None,
        max_turns_per_life: int = RESEARCH_AGENT_MAX_TURNS_PER_LIFE,
    ) -> None:
        self.task_id = task_id
        self.title = title
        self.description = description
        self.project_name = project_name
        self.project_root = project_root
        self.existing_tasks = existing_tasks
        self.llm_base_url = llm_base_url or LLM_BASE_URL
        self.llm_model = llm_model or LLM_MODEL
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_context = max_context
        self.max_lives = max_lives or RESEARCH_AGENT_MAX_LIVES
        self.max_turns_per_life = max_turns_per_life

        self._has_source = _has_meaningful_source_files(project_root)
        self._restricted_schemas = _build_restricted_schemas(self._has_source)
        self._allowed_tools = {s["function"]["name"] for s in self._restricted_schemas}

        self._accumulated_findings: list[str] = []
        self._total_prompt_tokens = 0
        self._total_completion_tokens = 0

    async def run(self) -> ClarificationResult:
        """Run the clarification agent. Returns a ClarificationResult."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(_CLARIFICATION_AGENT_NAME)

        total_turns = 0
        final_output: dict | None = None

        for life_num in range(1, self.max_lives + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            logger.info(
                "Clarification agent life %d/%d for task '%s': %s",
                life_num, self.max_lives, self.task_id, self.title[:60],
            )

            life_context = self._build_life_context(life_num)
            output, turns, pt, ct, findings = await self._run_life(life_context, life_num)

            total_turns += turns
            self._total_prompt_tokens += pt
            self._total_completion_tokens += ct

            if findings:
                self._accumulated_findings.append(f"[Life {life_num}] {findings}")

            if output:
                final_output = output
                break

        if not final_output:
            # Fallback: return original description wrapped in the expected structure
            logger.warning("Clarification agent exhausted without producing output for task '%s'", self.task_id)
            final_output = {
                "rewritten_description": (
                    f"**Goal:** {self.description[:200]}\n\n"
                    f"**Acceptance Criteria:**\n- (To be determined)\n\n"
                    f"**Out of Scope:** Not specified\n\n"
                    f"**Constraints:** None"
                ),
                "design_rationale": "Agent exhausted without producing a full rewrite.",
                "acceptance_criteria": [],
                "out_of_scope": "Not specified",
                "open_questions": ["The clarification agent could not complete its analysis. Please review and edit this description manually."],
                "suggested_prerequisites": [],
                "suggested_subtasks": [],
            }

        return ClarificationResult(
            rewritten_description=final_output.get("rewritten_description", self.description),
            design_rationale=final_output.get("design_rationale", ""),
            acceptance_criteria=final_output.get("acceptance_criteria", []),
            out_of_scope=final_output.get("out_of_scope", ""),
            open_questions=final_output.get("open_questions", []),
            suggested_prerequisites=final_output.get("suggested_prerequisites", []),
            suggested_subtasks=final_output.get("suggested_subtasks", []),
            prompt_tokens=self._total_prompt_tokens,
            completion_tokens=self._total_completion_tokens,
            lives_used=life_num,
        )

    def _build_life_context(self, life_num: int) -> str:
        from app.agent.project_snapshot import build_architecture_context
        parts: list[str] = []

        if life_num == 1:
            arch_ctx = build_architecture_context(self.project_name, agent_type="intake")
            if arch_ctx:
                parts.append(arch_ctx)

            # Existing tasks list for prerequisite detection
            if self.existing_tasks:
                task_lines = []
                for t in self.existing_tasks[:60]:  # cap to avoid context bloat
                    task_lines.append(f"  [{t['id']}] ({t['type']}) {t['title']}")
                parts.append(
                    "== EXISTING PROJECT TASKS (for prerequisite suggestions) ==\n"
                    + "\n".join(task_lines)
                )

        parts.append(
            f"== TASK TO CLARIFY ==\n"
            f"ID: {self.task_id}\n"
            f"Title: {self.title}\n"
            f"Description:\n{self.description}"
        )

        if life_num > 1 and self._accumulated_findings:
            prev = "\n\n".join(self._accumulated_findings)
            parts.append(
                f"== FINDINGS FROM PREVIOUS LIVES ==\n{prev}\n\n"
                f"Continue your analysis and produce the final JSON report."
            )

        return "\n\n".join(parts)

    async def _run_life(
        self, context: str, life_num: int
    ) -> tuple[dict | None, int, int, int, str]:
        """Run one clarification life. Returns (output_dict, turns, pt, ct, findings_text)."""
        messages = [
            {"role": "system", "content": _CLARIFICATION_SYSTEM_PROMPT},
            {"role": "user", "content": context},
        ]

        turns_used = 0
        life_prompt_tokens = 0
        life_completion_tokens = 0
        _ctx_warned: set[float] = set()
        _turn_warned: set[int] = set()
        _last_prompt_tokens = 0
        findings_text = ""

        for _turn in range(self.max_turns_per_life):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            turns_used += 1
            budget_tokens = int(self.max_context * RESEARCH_CONTEXT_BUDGET_RATIO) if self.max_context else 0
            budget_exceeded = budget_tokens > 0 and _last_prompt_tokens >= budget_tokens

            if budget_exceeded:
                messages.append({
                    "role": "user",
                    "content": "[SYSTEM] TOKEN BUDGET EXCEEDED. Output your JSON report now.",
                })

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    tools=None if budget_exceeded else self._restricted_schemas,
                    tool_choice=None if budget_exceeded else "auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=_CLARIFICATION_AGENT_NAME,
                )
            except ContextTooLargeError:
                logger.warning("Clarification agent context too large on life %d", life_num)
                break
            except Exception as exc:
                if is_shutting_down():
                    raise
                turns_used -= 1
                logger.error("Clarification agent LLM call failed (life %d): %s", life_num, exc)
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

            output = _extract_clarification_output(content)
            if output:
                findings_text = content
                return output, turns_used, life_prompt_tokens, life_completion_tokens, findings_text

            if tool_calls:
                if budget_exceeded:
                    messages.extend([{
                        "role": "tool",
                        "tool_call_id": tc.get("id", "unknown"),
                        "name": tc.get("function", {}).get("name", "unknown"),
                        "content": "ERROR: Token budget exceeded. Output your JSON report now.",
                    } for tc in tool_calls])
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
                            result_content = f"ERROR: Tool '{name}' not available. Available: {sorted(self._allowed_tools)}"
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

            if check_context_saturation(_last_prompt_tokens, self.max_context, _ctx_warned, messages):
                logger.warning("Clarification agent context saturated (life %d)", life_num)
                break

            from app.agent.config import check_turn_saturation
            check_turn_saturation(turns_used, self.max_turns_per_life, _turn_warned, messages)

            if not tool_calls and not output:
                if content:
                    findings_text = content
                else:
                    messages.append({
                        "role": "user",
                        "content": (
                            "[SYSTEM] You did not call any tool and did not produce the JSON report. "
                            "Either use a tool to investigate further, or output your JSON report now."
                        ),
                    })

        return None, turns_used, life_prompt_tokens, life_completion_tokens, findings_text


def _extract_clarification_output(content: str) -> dict | None:
    """Extract and validate the clarification JSON from the assistant's response."""
    raw = extract_json_block(content)
    if raw is None:
        return None
    try:
        parsed = json.loads(raw.strip())
        if isinstance(parsed, dict) and "rewritten_description" in parsed:
            return parsed
    except (json.JSONDecodeError, ValueError):
        pass
    return None


_JUDGE_SYSTEM_PROMPT = """You are a **Specification Judge** inside the Maestro Orchestrator.

An automated Enrichment Agent has rewritten an auto-generated task card (created by a
subdivision or dreamer agent).  Your job is to decide whether the rewrite is a net
improvement over the original.

Approve if the rewrite adds at least ONE of:
- Specific, testable acceptance criteria that the original lacked
- Accurate file / module / function references grounded in the codebase
- A clearer, narrower scope boundary (Out of Scope section)
- Technical constraints or edge cases the original omitted

Reject if:
- The rewrite is essentially the same content, just reformatted
- The rewrite introduces vague or unverifiable criteria ("works correctly", "is clean")
- The rewrite appears to contradict or misrepresent the original intent
- The original is already complete and specific enough to proceed directly to planning

Output ONLY this JSON (no prose, no code block):
{"verdict": "approve", "reason": "...one sentence..."}
or
{"verdict": "reject", "reason": "...one sentence..."}
"""


async def _judge_enrichment(
    original_description: str,
    result: "ClarificationResult",
    llm_base_url: str,
    llm_model: str,
    task_id: str,
    llm_id: int | None,
    budget_id: int | None,
) -> tuple[bool, str]:
    """Single-turn judge that decides if an enrichment is worth applying.
    Returns (approve, reason).
    """
    acs = "\n".join(f"- {c}" for c in result.acceptance_criteria) if result.acceptance_criteria else "(none)"
    oq = "\n".join(f"- {q}" for q in result.open_questions) if result.open_questions else "(none)"
    prompt = (
        f"== ORIGINAL DESCRIPTION ==\n{original_description}\n\n"
        f"== ENRICHED DESCRIPTION ==\n{result.rewritten_description}\n\n"
        f"== ACCEPTANCE CRITERIA ADDED ==\n{acs}\n\n"
        f"== OPEN QUESTIONS FLAGGED ==\n{oq}\n\n"
        f"== DESIGN RATIONALE ==\n{result.design_rationale or '(none)'}\n\n"
        "Verdict?"
    )
    messages = [
        {"role": "system", "content": _JUDGE_SYSTEM_PROMPT},
        {"role": "user", "content": prompt},
    ]
    try:
        response = await call_llm(
            messages,
            base_url=llm_base_url,
            model=llm_model,
            tools=None,
            tool_choice=None,
            task_id=task_id,
            llm_id=llm_id,
            budget_id=budget_id,
            agent_name="Enrichment Judge",
        )
        content = (response.get("choices", [{}])[0].get("message", {}).get("content") or "").strip()
        # Strip optional markdown code fence
        if content.startswith("```"):
            content = content.split("```")[1].lstrip("json").strip()
            if "```" in content:
                content = content[: content.index("```")]
        parsed = json.loads(content)
        approve = str(parsed.get("verdict", "")).lower() == "approve"
        reason = parsed.get("reason", "")
        return approve, reason
    except Exception as exc:
        logger.warning("Enrichment Judge failed for task '%s': %s — defaulting to approve", task_id, exc)
        return True, f"judge error ({exc}); defaulting to approve"


# Tasks created by automated agents that bypass the human approval gate.
# The enrichment judge auto-approves or auto-skips instead.
_AUTO_OWNERS = frozenset({"system", "dreamer"})


async def run_clarification(
    task_id: str,
    title: str,
    description: str,
    project_name: str,
    project_root: str,
    existing_tasks: list[dict],
    llm_id: int | None = None,
    budget_id: int | None = None,
    max_context: int = 0,
) -> ClarificationResult:
    """Module-level entry point for running the clarification agent."""
    from app.database import get_llm
    llm = get_llm(llm_id) if llm_id else None
    agent = ClarificationAgent(
        task_id=task_id,
        title=title,
        description=description,
        project_name=project_name,
        project_root=project_root,
        existing_tasks=existing_tasks,
        llm_base_url=f"http://{llm.address}:{llm.port}/v1" if llm else None,
        llm_model=llm.model if llm else None,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=max_context or (llm.max_context if llm else 0),
    )
    return await agent.run()


def run_clarification_for_task(task_id: str) -> None:
    """Synchronous wrapper for background thread dispatch from main.py.

    Human-created cards (owner not in _AUTO_OWNERS) go to awaiting_user so the
    human can review the draft on the board before intake runs.

    Auto-created cards (owner in _AUTO_OWNERS) run the enrichment agent then a
    Judge LLM.  If the judge approves, the rewrite is applied and the card is
    auto-approved.  If the judge rejects, the original description is kept and
    the card is skipped (proceeds directly to intake without a rewrite).
    """
    import asyncio as _asyncio
    from app.database import get_task, get_tasks_by_project, update_task, get_project_path
    from app.database import create_intake_draft, update_intake_draft

    task = get_task(task_id)
    if not task:
        logger.error("run_clarification_for_task: task '%s' not found", task_id)
        return

    is_auto = (getattr(task, "owner", None) or "user") in _AUTO_OWNERS

    project_name = task.project or "TheMaestro"
    project_root = get_project_path(project_name) or ""

    # Gather sibling tasks for prerequisite suggestions (exclude self)
    sibling_tasks = [
        {"id": t.id, "title": t.title, "type": t.type}
        for t in get_tasks_by_project(project_name)
        if t.id != task_id and t.type != "architecture"
    ]

    # Ensure draft row exists (idempotent — retrigger resets the row but doesn't delete it)
    from app.database import get_intake_draft as _get_draft
    if not _get_draft(task_id):
        create_intake_draft(task_id)

    original_description = task.description or ""

    try:
        result = _asyncio.run(run_clarification(
            task_id=task_id,
            title=task.title,
            description=original_description,
            project_name=project_name,
            project_root=project_root,
            existing_tasks=sibling_tasks,
            llm_id=task.llm_id,
            budget_id=task.budget_id,
        ))
    except TaskDeactivatedError as exc:
        logger.info("Clarification agent for '%s' halted: %s", task_id, exc)
        return
    except ShutdownError:
        logger.info("Clarification agent for '%s' aborted: server shutting down", task_id)
        update_task(task_id, clarification_status="skipped")
        return
    except Exception as exc:
        logger.exception("Clarification agent failed for task '%s': %s", task_id, exc)
        update_task(task_id, clarification_status="skipped")
        return

    if is_auto:
        # Run the judge to decide whether to apply the enrichment.
        from app.database import get_llm as _get_llm
        _llm = _get_llm(task.llm_id) if task.llm_id else None
        _llm_url = f"http://{_llm.address}:{_llm.port}/v1" if _llm else ""
        _llm_model = _llm.model if _llm else ""
        try:
            approve, reason = _asyncio.run(_judge_enrichment(
                original_description=original_description,
                result=result,
                llm_base_url=_llm_url,
                llm_model=_llm_model,
                task_id=task_id,
                llm_id=task.llm_id,
                budget_id=task.budget_id,
            ))
        except Exception as exc:
            logger.warning("Judge failed for auto task '%s': %s — defaulting to approve", task_id, exc)
            approve, reason = True, f"judge error: {exc}"

        if approve:
            update_intake_draft(
                task_id,
                rewritten_description=result.rewritten_description,
                design_rationale=result.design_rationale,
                acceptance_criteria=result.acceptance_criteria,
                out_of_scope=result.out_of_scope,
                open_questions=result.open_questions,
                suggested_prerequisites=result.suggested_prerequisites,
                suggested_subtasks=result.suggested_subtasks,
                agent_token_cost=(result.prompt_tokens + result.completion_tokens),
            )
            update_task(
                task_id,
                clarification_status="approved",
                description=result.rewritten_description,
            )
            logger.info(
                "Auto-enrichment APPROVED for task '%s' (owner=%s): %s",
                task_id, task.owner, reason,
            )
        else:
            update_task(task_id, clarification_status="skipped")
            logger.info(
                "Auto-enrichment REJECTED for task '%s' (owner=%s): %s — keeping original",
                task_id, task.owner, reason,
            )
        return

    # Human card: store draft and wait for board approval.
    update_intake_draft(
        task_id,
        rewritten_description=result.rewritten_description,
        design_rationale=result.design_rationale,
        acceptance_criteria=result.acceptance_criteria,
        out_of_scope=result.out_of_scope,
        open_questions=result.open_questions,
        suggested_prerequisites=result.suggested_prerequisites,
        suggested_subtasks=result.suggested_subtasks,
        agent_token_cost=(result.prompt_tokens + result.completion_tokens),
    )
    update_task(task_id, clarification_status="awaiting_user")
    logger.info("Clarification agent completed for task '%s'", task_id)
