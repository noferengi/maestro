"""
app/agent/dreamer.py
--------------------
Dreamer — autonomous project resurrection agent.

Fires when the scheduler detects a project has had no pipeline activity for
DREAMER_STALL_TICKS consecutive ticks.  The Dreamer:

  1. Surveys failing / stalled tasks (pure DB — no LLM).
  2. Makes a single LLM call to generate a mutation plan (JSON).
  3. Acts: clones / mutates failing cards, creates new idea cards, queues
     research jobs for NEEDS_RESEARCH tasks.

One DreamerAgent instance is created per project per stall event.  It runs
in its own daemon thread (spawned by the scheduler) with its own event loop.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent.llm_client import is_shutting_down, sanitize_user_content, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "Dreamer"


# ---------------------------------------------------------------------------
# Mutation strategies
# ---------------------------------------------------------------------------

MUTATION_STRATEGIES = [
    ("simplify",    "Reduce scope, remove complexity, or break into smaller pieces."),
    ("refocus",     "Change the primary implementation approach or data structure."),
    ("expand",      "Add more context, detail, and constraints to the description."),
    ("restructure", "Reorganise into cleaner subtasks with explicit interfaces."),
    ("innovate",    "Try a completely different technical approach."),
]


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class FailedTask:
    task_id: str
    title: str
    description: str
    stage: str
    failure_reason: str


@dataclass
class ProjectState:
    project_name: str
    needs_research: list[FailedTask] = field(default_factory=list)
    failed: list[FailedTask]        = field(default_factory=list)
    alive_count: int = 0
    arch_context: str = ""
    # Survey-mode fields (populated even when no failures exist)
    has_files: bool = False                              # project path contains source files
    deleted_tasks: list[dict] = field(default_factory=list)  # recently soft-deleted task summaries


@dataclass
class DreamerPlan:
    tasks_to_resurrect: list[dict]   # [{task_id, new_title, new_description, reentry_stage}]
    tasks_to_research:  list[str]    # [task_id]
    new_cards:          list[dict]   # [{title, description, rationale}]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class DreamerAgent:
    """Survey → Decide → Act agent for stalled projects."""

    def __init__(
        self,
        project_name: str,
        project_path: "str | None",
        llm_id: int,
        budget_id: int,
        llm_base_url: str,
        llm_model: str,
    ):
        self.project      = project_name
        self.project_path = project_path
        self.llm_id       = llm_id
        self.budget_id    = budget_id
        self.llm_base_url = llm_base_url
        self.llm_model    = llm_model
        
        from app.agent.survey_orchestrator import SurveyOrchestrator
        self.orchestrator = SurveyOrchestrator()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    async def run(self) -> dict:
        """Full Dreamer lifecycle: survey → decide → act."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        from app.database import create_dreamer_run, update_dreamer_run

        run = create_dreamer_run(self.project, self.llm_id, self.budget_id)
        if not run:
            logger.error("[Dreamer] Failed to create run record for '%s'.", self.project)
            return {"status": "error", "reason": "db_error"}

        try:
            # Phase 1: Survey (DB only)
            state = self._survey_project()
            logger.info(
                "[Dreamer] '%s': %d failed, %d needs_research, %d alive, "
                "%d deleted, has_files=%s.",
                self.project, len(state.failed), len(state.needs_research),
                state.alive_count, len(state.deleted_tasks), state.has_files,
            )

            if not state.failed and not state.needs_research:
                # No failures to resurrect.  Enter survey mode if the project has
                # substantive content (files or prior history) to reason about.
                # Otherwise there is nothing to latch onto and we skip this run.
                has_signal = state.has_files or bool(state.arch_context)
                if not has_signal:
                    logger.info(
                        "[Dreamer] '%s': no failures and no project signal — skipping.",
                        self.project,
                    )
                    update_dreamer_run(run.id, status="completed",
                                       stall_reason="no_signal",
                                       actions_taken=[], new_task_ids=[])
                    return {"status": "no_action", "reason": "no signal"}

                logger.info(
                    "[Dreamer] '%s': no failures — survey mode "
                    "(has_files=%s, arch=%s, deleted=%d).",
                    self.project, state.has_files,
                    bool(state.arch_context), len(state.deleted_tasks),
                )
                plan = await self._decide_survey(state)
                stall_reason = "survey"
            else:
                # Phase 1.5: Research — schedule one research job per NEEDS_RESEARCH task
                # and wait for all to complete before deciding.  Findings are injected into
                # the Decide prompt so the LLM knows root causes, not just symptoms.
                research_findings: dict[str, str] = {}
                if state.needs_research:
                    research_findings = await self._run_research_phase(state)

                # Pick a mutation strategy (seeded per-project per-hour for variety)
                rng = random.Random(hash(self.project) ^ int(time.time() / 3600))
                strategy_name, strategy_desc = rng.choice(MUTATION_STRATEGIES)

                # Phase 2: Decide (1 LLM call, enriched with research findings)
                plan = await self._decide(state, strategy_name, strategy_desc,
                                          research_findings=research_findings)
                stall_reason = f"strategy={strategy_name}"

            # Phase 3: Act (DB operations only)
            actions, new_task_ids = self._act(plan, state)

            update_dreamer_run(
                run.id,
                status="completed",
                stall_reason=stall_reason,
                actions_taken=actions,
                new_task_ids=new_task_ids,
            )
            logger.info(
                "[Dreamer] '%s' completed: %d actions, %d new tasks.",
                self.project, len(actions), len(new_task_ids),
            )
            return {"status": "completed", "actions": actions, "new_task_ids": new_task_ids}

        except Exception as exc:
            logger.exception("[Dreamer] '%s' run failed: %s", self.project, exc)
            try:
                from app.database import update_dreamer_run as _upd
                _upd(run.id, status="failed", stall_reason=str(exc)[:500])
            except Exception:
                pass
            raise

    # ------------------------------------------------------------------
    # Phase 1: Survey
    # ------------------------------------------------------------------
    # Phase 1.5: Research
    # ------------------------------------------------------------------

    async def _run_research_phase(self, state: ProjectState) -> "dict[str, str]":
        """Schedule one research job per NEEDS_RESEARCH task and await all completions.

        Returns a mapping of task_id → findings string.  Tasks whose research
        job fails or times out are omitted — Decide still runs; it just won't
        have that task's findings.

        Dreamer threads are not tracked in the scheduler's _active_sessions, so
        no park/unpark is needed — the scheduler sees the LLM as free and will
        dispatch research jobs normally.
        """
        import asyncio as _asyncio
        from app.database import create_research_job as _create_rj, get_research_job as _get_rj
        from app.agent.scheduler import get_or_create_completion_event as _get_event, is_shutting_down

        if not state.needs_research:
            return {}

        jobs: list[tuple[str, int]] = []   # [(task_id, job_id)]
        for ft in state.needs_research:
            question = (
                f"Task '{sanitize_user_content(ft.title)}' (stage={ft.stage!r}) is stalled in the Maestro "
                f"pipeline.  Most recent failure: {sanitize_user_content(ft.failure_reason)}\n\n"
                f"Task description: {sanitize_user_content(ft.description[:300])}\n\n"
                "What is the root cause of this failure, and what concrete changes "
                "to the task description or implementation approach would allow it "
                "to pass the pipeline?"
            )
            job = _create_rj(
                task_id=ft.task_id,
                question=question,
                context=json.dumps({"project": self.project, "stage": ft.stage}),
                priority=0.0,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
            )
            if job:
                jobs.append((ft.task_id, job.id))
                logger.info(
                    "[Dreamer] Scheduled research job %d for task '%s'.",
                    job.id, ft.task_id,
                )

        if not jobs:
            return {}

        # Wait for all research jobs concurrently (poll every 30s, timeout 2h).
        MAX_WAIT   = 7200.0
        POLL       = 30.0
        loop       = _asyncio.get_event_loop()

        async def _wait_one(task_id: str, job_id: int) -> "tuple[str, str]":
            key = f"research_job_{job_id}"
            event, _ = _get_event(key)
            elapsed = 0.0
            while not event.is_set() and elapsed < MAX_WAIT:
                if is_shutting_down():
                    return task_id, ""
                remaining = min(POLL, MAX_WAIT - elapsed)
                done = await loop.run_in_executor(None, event.wait, remaining)
                if done:
                    break
                elapsed += remaining
            if not event.is_set():
                logger.warning("[Dreamer] Research job %d timed out.", job_id)
                return task_id, ""
            result = _get_rj(job_id)
            if result and result.findings:
                return task_id, result.findings
            return task_id, ""

        pairs = await _asyncio.gather(*[_wait_one(tid, jid) for tid, jid in jobs])
        return {tid: findings for tid, findings in pairs if findings}

    # ------------------------------------------------------------------
    # Phase 1: Survey
    # ------------------------------------------------------------------

    def _survey_project(self) -> ProjectState:
        """Pure DB survey — no LLM calls."""
        import os
        from app.database import get_tasks_by_project, get_transition_results
        from app.database import get_deleted_tasks_by_project
        from app.agent.project_snapshot import build_architecture_context

        tasks = get_tasks_by_project(self.project)
        state = ProjectState(project_name=self.project)

        try:
            state.arch_context = build_architecture_context(self.project, agent_type=None)
        except Exception:
            state.arch_context = ""

        # Detect whether the project path has any source files.
        if self.project_path and os.path.isdir(self.project_path):
            from app.agent.path_filter import walk_safe
            for _root, dirs, files in walk_safe(self.project_path):
                if files:
                    state.has_files = True
                    break

        # Collect soft-deleted tasks so the Dreamer can reference prior intent.
        deleted = get_deleted_tasks_by_project(self.project, limit=15)
        for dt in deleted:
            results = get_transition_results(dt.id)
            last_outcome = "none"
            if results:
                latest = sorted(
                    results,
                    key=lambda r: r.created_at or datetime.min,
                    reverse=True,
                )[0]
                last_outcome = (latest.outcome or "none").lower()
            state.deleted_tasks.append({
                "id":          dt.id,
                "title":       dt.title,
                "description": (dt.description or "")[:200],
                "stage":       dt.type,
                "outcome":     last_outcome,
            })

        skip_types = {"completed", "architecture"}

        for task in tasks:
            if not task.is_active:
                continue
            if task.type in skip_types:
                continue

            results = get_transition_results(task.id)
            if not results:
                # Never processed — leave as-is; the scheduler will handle it
                continue

            # Most recent result (sort descending by created_at)
            latest = sorted(
                results,
                key=lambda r: r.created_at or datetime.min,
                reverse=True,
            )[0]

            outcome = (latest.outcome or "").lower()

            if outcome in ("passed", "accepted", "running", "aborted_infra"):
                state.alive_count += 1
                continue

            reason = self._format_failure_reason(latest)
            ft = FailedTask(
                task_id=task.id,
                title=task.title,
                description=(task.description or "")[:400],
                stage=task.type,
                failure_reason=reason,
            )

            if "needs_research" in outcome:
                state.needs_research.append(ft)
            else:
                state.failed.append(ft)

        return state

    def _format_failure_reason(self, result: Any) -> str:
        """Extract a concise failure reason from a TransitionResult."""
        outcome = result.outcome or "unknown"
        summary = result.vote_summary or {}
        if isinstance(summary, str):
            try:
                summary = json.loads(summary)
            except Exception:
                pass

        justification = ""
        if isinstance(summary, dict):
            for key in ("justification", "detail", "error", "reason"):
                val = summary.get(key)
                if val:
                    justification = str(val)[:200]
                    break

        return f"{outcome}: {justification}" if justification else outcome

    # ------------------------------------------------------------------
    # Phase 2: Decide
    # ------------------------------------------------------------------

    async def _decide(
        self,
        state: ProjectState,
        strategy_name: str,
        strategy_desc: str,
        research_findings: "dict[str, str] | None" = None,
    ) -> DreamerPlan:
        """Single LLM call to produce a mutation plan.

        ``research_findings`` maps task_id → findings string for tasks that
        completed the Phase 1.5 research step.  When present they are injected
        into the failed-tasks block so the LLM has root-cause detail, not just
        symptoms.
        """
        from app.agent.llm_client import call_llm, extract_text_response
        from app.agent.config import DREAMER_DECIDE_MAX_TOKENS
        from app.agent.tools import build_tool_schemas, dispatch_tool

        rf = research_findings or {}

        # Build the failed-tasks block (cap at 10 entries)
        actionable = (state.failed + state.needs_research)[:10]
        failed_block_lines = []
        for ft in actionable:
            tag = "NEEDS_RESEARCH" if ft in state.needs_research else "FAILED"
            lines = [
                f"[{tag}] id={ft.task_id!r} stage={ft.stage!r}",
                f"  Title: {sanitize_user_content(ft.title)}",
                f"  Description: {sanitize_user_content(ft.description[:200])}",
                f"  Failure: {sanitize_user_content(ft.failure_reason)}",
            ]
            if ft.task_id in rf:
                # Cap findings at 600 chars to keep the prompt manageable
                lines.append(f"  Research findings: {sanitize_user_content(rf[ft.task_id][:600])}")
            failed_block_lines.append("\n".join(lines))
        failed_block = "\n\n".join(failed_block_lines)

        arch_block = (
            f"\nProject Architecture Context:\n{sanitize_user_content(state.arch_context)}\n"
            if state.arch_context else ""
        )

        # Adjust the system prompt when research findings are present
        research_guidance = (
            "\nResearch findings are provided for NEEDS_RESEARCH tasks above. "
            "Use them to write a concrete new_description that addresses the "
            "identified root cause rather than repeating the original wording."
            if rf else ""
        )

        system_prompt = (
            "You are the Dreamer — an autonomous project resurrection agent.\n"
            "Your job is to analyse stalled and failed tasks in a software project "
            "and propose concrete actions to unblock them.\n\n"
            "To submit your plan, call the submit_work tool with:\n"
            "payload={\n"
            "  \"tasks_to_resurrect\": [{\"task_id\": \"...\", \"new_title\": \"...\", \"new_description\": \"...\", \"reentry_stage\": \"idea|planning|indev\"}],\n"
            "  \"tasks_to_research\": [\"task_id\", ...],\n"
            "  \"new_cards\": [{\"title\": \"...\", \"description\": \"...\", \"rationale\": \"...\"}]\n"
            "}\n\n"
            "Limits: max 5 resurrections, max 3 new_cards.\n"
            "new_description must be focused and concrete — not a copy of the original."
            f"{research_guidance}\n"
            "No prose after calling submit_work."
        )

        user_msg = (
            f"Project: {self.project}\n"
            f"Mutation strategy: {strategy_name} — {strategy_desc}\n"
            f"{arch_block}\n"
            "Stalled / failed tasks:\n\n"
            f"{failed_block}\n"
            "Generate the DreamerPlan now."
        )

        tool_schemas = build_tool_schemas(["submit_work"])

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": system_prompt},
                    {"role": "user",   "content": user_msg},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                max_tokens=DREAMER_DECIDE_MAX_TOKENS,
                agent_name="Dreamer",
                tools=tool_schemas,
                tool_choice="auto",
            )
            
            assistant_msg = response.get("choices", [{}])[0].get("message", {})
            tool_calls = assistant_msg.get("tool_calls") or []
            
            data = None
            if tool_calls:
                for tc in tool_calls:
                    tc_result = dispatch_tool(
                        tc["function"]["name"],
                        json.loads(tc["function"]["arguments"]) if isinstance(tc["function"]["arguments"], str) else tc["function"]["arguments"],
                    )
                    if isinstance(tc_result, str) and "__maestro_terminal__" in tc_result:
                        data = json.loads(tc_result).get("payload")
                        break
            
            if data is None:
                raw = assistant_msg.get("content", "{}")
                from app.agent.json_utils import extract_json_block as _ejb
                candidate = _ejb(raw) if raw.strip() else None
                if candidate:
                    data = json.loads(candidate)
                elif raw.strip():
                    data, _ = json.JSONDecoder().raw_decode(raw.lstrip())
                else:
                    data = {}
        except Exception as exc:
            logger.warning("[Dreamer] LLM call failed: %s — using empty plan.", exc)
            data = {}

        return DreamerPlan(
            tasks_to_resurrect=data.get("tasks_to_resurrect") or [],
            tasks_to_research=data.get("tasks_to_research") or [],
            new_cards=data.get("new_cards") or [],
        )

    # ------------------------------------------------------------------
    # Phase 2b: Decide (survey mode — no failures, generate from codebase)
    # ------------------------------------------------------------------

    async def _decide_survey(self, state: ProjectState) -> DreamerPlan:
        """Generate new idea cards by surveying what the project does and could do.

        Called when there are no failed tasks to resurrect.  The LLM receives:
          - arch context cards (if any)
          - a summary of recently deleted tasks (prior human/AI intent)
          - access to survey tools (get_project_summary, list_scope_summaries, etc.)

        The model is asked to propose up to 3 concrete new idea cards.
        """
        from app.agent.llm_client import call_llm, extract_text_response
        from app.agent.json_utils import extract_json_block
        from app.agent.config import DREAMER_DECIDE_MAX_TOKENS, DREAMER_SURVEY_TOOLS
        from app.agent.survey_orchestrator import SurveyOrchestrator
        from app.agent.tools import build_tool_schemas, async_dispatch_tool, _task_project_name, _task_git_cwd

        # 1. Ensure project is being surveyed (enqueues jobs if needed)
        orchestrator = SurveyOrchestrator()
        if self.project_path:
            orchestrator.ensure_project_surveyed(
                self.project, self.project_path, self.llm_id, self.budget_id
            )

        arch_block = (
            f"\nArchitecture context:\n{sanitize_user_content(state.arch_context)}\n"
            if state.arch_context else ""
        )

        deleted_block = ""
        if state.deleted_tasks:
            lines = []
            for t in state.deleted_tasks:
                lines.append(
                    f"  - [{t['stage']}] {sanitize_user_content(t['title'])}: "
                    f"{sanitize_user_content(t['description'][:150])} (last outcome: {t['outcome']})"
                )
            deleted_block = (
                "\nRecently deleted/archived tasks (prior intent context):\n"
                + "\n".join(lines) + "\n"
            )

        system_prompt = (
            "You are the Dreamer — an autonomous project discovery agent.\n"
            "A software project has no active work items.  Your job is to survey "
            "its codebase and history, then propose new concrete idea cards for "
            "work that would be valuable, feasible, and not yet tracked.\n\n"
            "You have access to survey tools to explore the project's health and organization. "
            "Use them to understand the project before making your final proposal.\n\n"
            "To submit your new cards, call the submit_work tool with:\n"
            "payload={\"new_cards\": [{\"title\": \"...\", \"description\": \"...\", \"rationale\": \"...\"}]}\n\n"
            "Focus on:\n"
            "  1. Features or improvements clearly present in the codebase but untracked\n"
            "  2. Obvious gaps or natural next steps given the project's current state\n"
            "  3. High-value technical debt, observability, or quality work\n\n"
            "Rules:\n"
            "  - If the project is too sparse to determine intent, output new_cards: []\n"
            "  - new_cards descriptions must be concrete and actionable\n"
            "  - Max 3 cards\n\n"
            "No prose after calling submit_work."
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user",   "content": f"Project: {sanitize_user_content(self.project)}\n{arch_block}{deleted_block}\nSurvey this project and propose up to 3 new idea cards."}
        ]
        
        # Include submit_work in survey tools
        tool_schemas = build_tool_schemas(DREAMER_SURVEY_TOOLS + ["submit_work"])
        
        # Set context for tools
        _task_project_name.set(self.project)
        _task_git_cwd.set(self.project_path)

        # Multi-turn loop (max 100 turns)
        max_turns = 100
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

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    max_tokens=DREAMER_DECIDE_MAX_TOKENS,
                    agent_name="Dreamer",
                    tools=tool_schemas,
                    tool_choice="auto",
                )
            except Exception as exc:
                logger.warning("[Dreamer] Survey LLM call failed on turn %d: %s", turn, exc)
                break

            msg = response.get("choices", [{}])[0].get("message", {})
            messages.append(msg)
            
            tool_calls = msg.get("tool_calls") or []
            if tool_calls:
                for tc in tool_calls:
                    t_id = tc["id"]
                    t_name = tc["function"]["name"]
                    t_args = json.loads(tc["function"]["arguments"])
                    
                    result = await async_dispatch_tool(
                        t_name, t_args,
                        llm_id=self.llm_id, budget_id=self.budget_id,
                        llm_base_url=self.llm_base_url, llm_model=self.llm_model
                    )
                    messages.append({"role": "tool", "tool_call_id": t_id, "name": t_name, "content": result})

                    # Check for terminal signal from submit_work
                    if isinstance(result, str) and "__maestro_terminal__" in result:
                        try:
                            data = json.loads(result)
                            payload = data.get("payload", {})
                            return DreamerPlan(
                                tasks_to_resurrect=[],
                                tasks_to_research=[],
                                new_cards=payload.get("new_cards") or [],
                            )
                        except (json.JSONDecodeError, ValueError):
                            pass
                continue

            # Fallback for content
            raw = msg.get("content") or "{}"
            try:
                from app.agent.json_utils import extract_json_block as _ejb
                candidate = _ejb(raw) if raw.strip() else None
                if candidate:
                    data = json.loads(candidate)
                elif raw.strip():
                    # Try simple cleaning for stubborn LLMs
                    cleaned = raw.strip()
                    if cleaned.startswith("```"):
                        cleaned = cleaned.split("\n", 1)[1].rsplit("\n", 1)[0]
                    data = json.loads(cleaned)
                else:
                    data = {}
                
                if data and "new_cards" in data:
                    return DreamerPlan(
                        tasks_to_resurrect=[],
                        tasks_to_research=[],
                        new_cards=data.get("new_cards") or [],
                    )
            except Exception:
                pass

            # Nudge if no tool calls and no clear JSON in content
            messages.append({
                "role": "user",
                "content": "[SYSTEM] You must call submit_work to output your discovered cards when ready."
            })

        return DreamerPlan(
            tasks_to_resurrect=[],
            tasks_to_research=[],
            new_cards=[],
        )

    # ------------------------------------------------------------------
    # Phase 3: Act
    # ------------------------------------------------------------------

    def _act(
        self,
        plan: DreamerPlan,
        state: ProjectState,
    ) -> "tuple[list[dict], list[str]]":
        """Execute the plan — all DB operations, no LLM calls."""
        from app.database import get_task, create_task, create_research_job
        from app.agent.config import DREAMER_MAX_RESURRECTIONS, DREAMER_MAX_NEW_CARDS

        actions: list[dict] = []
        new_task_ids: list[str] = []

        # -- Resurrections ------------------------------------------------
        for item in plan.tasks_to_resurrect[:DREAMER_MAX_RESURRECTIONS]:
            task_id    = str(item.get("task_id", "")).strip()
            original   = get_task(task_id)
            if not original:
                logger.warning("[Dreamer] Resurrection: task '%s' not found.", task_id)
                continue

            new_title = (item.get("new_title") or "").strip() or f"[Dreamer] {original.title}"
            new_desc  = (item.get("new_description") or "").strip() or original.description or ""
            reentry   = str(item.get("reentry_stage", "idea")).strip().lower()
            if reentry not in ("idea", "planning", "indev"):
                reentry = "idea"

            clone = create_task(
                title=new_title,
                task_type=reentry,
                description=new_desc,
                owner="dreamer",
                tags=list(original.tags or []) + ["dreamer-resurrected"],
                llm_id=original.llm_id or self.llm_id,
                budget_id=original.budget_id or self.budget_id,
                project=self.project,
            )
            if clone:
                new_task_ids.append(clone.id)
                actions.append({
                    "action":           "resurrected",
                    "original_task_id": task_id,
                    "new_task_id":      clone.id,
                    "new_title":        new_title,
                    "reentry_stage":    reentry,
                })
                logger.info(
                    "[Dreamer] Resurrected '%s' → '%s' (stage=%s).",
                    task_id, clone.id, reentry,
                )

        # -- Research queuing ---------------------------------------------
        all_ft: dict[str, FailedTask] = {
            ft.task_id: ft for ft in (state.needs_research + state.failed)
        }

        for task_id in plan.tasks_to_research:
            task_id = str(task_id).strip()
            t = get_task(task_id)
            if not t:
                continue
            ft = all_ft.get(task_id)
            failure_text = ft.failure_reason if ft else "unknown"
            question = (
                f"Task '{t.title}' (stage={t.type}) is stalled. "
                f"Most recent pipeline failure: {failure_text}. "
                "What is the root cause and what concrete changes to the task "
                "description or implementation approach would allow it to pass "
                "the pipeline?"
            )
            job = create_research_job(
                task_id=task_id,
                question=question,
                context=json.dumps({"project": self.project, "stage": t.type}),
                priority=2.0,           # lower priority than normal (0.0) research
                llm_id=t.llm_id or self.llm_id,
                budget_id=t.budget_id or self.budget_id,
            )
            if job:
                actions.append({
                    "action":   "queued_research",
                    "task_id":  task_id,
                    "question": question[:120],
                })
                logger.info("[Dreamer] Queued research job for task '%s'.", task_id)

        # -- New cards ----------------------------------------------------
        for item in plan.new_cards[:DREAMER_MAX_NEW_CARDS]:
            title = (item.get("title") or "").strip()
            desc  = (item.get("description") or "").strip()
            if not title:
                continue

            new_task = create_task(
                title=title,
                task_type="idea",
                description=desc,
                owner="dreamer",
                tags=["dreamer-generated"],
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                project=self.project,
            )
            if new_task:
                new_task_ids.append(new_task.id)
                actions.append({
                    "action":    "created",
                    "task_id":   new_task.id,
                    "title":     title,
                    "rationale": (item.get("rationale") or "")[:120],
                })
                logger.info("[Dreamer] Created new idea card '%s'.", title)

        return actions, new_task_ids
