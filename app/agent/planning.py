"""
app/agent/planning.py
---------------------
Planning pipeline for the PLANNING stage.

Sub-stages:
  1. Codebase Survey - agentic loop (read-only tools, max N turns)
  2. Best-of-N Design Generation - N parallel structured LLM calls
  3. Design Review Panel - 3 parallel LLM reviewers
  4. Pitfall Detection - deterministic + 1 LLM call
  5. Plan Consolidation - 1 LLM call to merge winner + pitfall mitigations

Output: PlanningResult stored in planning_results table.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import asdict, dataclass, field
from typing import Any

from app.agent.config import (
    PLANNING_BEST_OF_N,
    PLANNING_TEMPERATURE_SPREAD,
    PLANNING_JUDGE_TEMPERATURE,
    PLANNING_MAX_DESIGN_RETRIES,
    PLANNING_SURVEY_MAX_TURNS,
    PLANNING_LLM_TEMPERATURE,
    PROJECT_ROOT,
    check_context_saturation,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
from app.agent.verdicts import Vote, Verdict, tally_votes

logger = logging.getLogger(__name__)
AGENT_NAME = "Planning Pipeline"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(slots=True)
class FileManifestEntry:
    path: str
    action: str  # create | modify | delete
    purpose: str
    estimated_lines: int = 0
    depends_on: list[str] = field(default_factory=list)


@dataclass(slots=True)
class InterfaceContract:
    component: str
    provides: list[str] = field(default_factory=list)
    consumes: list[str] = field(default_factory=list)
    invariants: list[str] = field(default_factory=list)


@dataclass(slots=True)
class TestStrategyEntry:
    component: str
    test_file: str
    test_cases: list[str] = field(default_factory=list)
    fixtures: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ImplementationStep:
    order: int
    component: str
    files: list[str] = field(default_factory=list)
    description: str = ""
    depends_on: list[str] = field(default_factory=list)
    estimated_context_tokens: int = 0


@dataclass(slots=True)
class PlanningResult:
    task_id: str
    design_rationale: str = ""
    file_manifest: list[FileManifestEntry] = field(default_factory=list)
    dependency_graph: dict[str, list[str]] = field(default_factory=dict)
    interface_contracts: list[InterfaceContract] = field(default_factory=list)
    test_strategy: list[TestStrategyEntry] = field(default_factory=list)
    implementation_steps: list[ImplementationStep] = field(default_factory=list)
    pitfalls_identified: list[dict] = field(default_factory=list)
    review_votes: list[Vote] = field(default_factory=list)
    best_of_n_designs: list[dict] = field(default_factory=list)
    selected_design_index: int = 0
    confidence: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0


# ---------------------------------------------------------------------------
# Read-only tools for codebase survey
# ---------------------------------------------------------------------------

SURVEY_TOOLS = [
    "read_file", "read_file_harder", "count_lines",
    "search_files", "find_files", "list_directory",
    "git_log", "git_blame",
    "get_task", "list_tasks",
]


def _get_survey_tool_schemas() -> list[dict]:
    """Return OpenAI-format tool schemas for the survey agent."""
    from app.agent.tools import TOOL_SCHEMAS
    return [s for s in TOOL_SCHEMAS if s["function"]["name"] in SURVEY_TOOLS]


# ---------------------------------------------------------------------------
# Pipeline class
# ---------------------------------------------------------------------------

class PlanningPipeline:
    """Orchestrates the planning pipeline for a single task."""

    def __init__(
        self,
        task_id: str,
        task_title: str,
        task_description: str,
        all_tasks: list[dict],
        *,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        max_context: int | None = None,
        run_row_id: int | None = None,
    ):
        self.task_id = task_id
        self.task_title = task_title
        self.task_description = task_description
        self.all_tasks = all_tasks
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_context = max_context
        self.run_row_id = run_row_id
        self._total_prompt = 0
        self._total_completion = 0

    async def run(self) -> PlanningResult:
        """Execute all planning sub-stages and return the result."""
        logger.info(f"[{AGENT_NAME}] Starting pipeline for task '%s'", self.task_id)

        # Stage 1: Codebase survey
        survey_summary = await self._stage_codebase_survey()

        # Retry loop for stages 2-3
        best_designs: list[dict] = []
        selected_index = 0
        review_votes: list[Vote] = []
        winning_design: dict = {}

        for attempt in range(PLANNING_MAX_DESIGN_RETRIES):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            # Stage 2: Best-of-N design generation
            designs = await self._stage_design_generation(survey_summary)
            best_designs = designs

            # Judge selects winner
            selected_index, winning_design = await self._stage_judge_designs(
                designs, survey_summary
            )

            # Stage 3: Design review panel
            votes = await self._stage_design_review(winning_design, survey_summary)
            review_votes = votes

            tally = tally_votes(votes)
            if tally.outcome in ("passed", "conditional_pass"):
                logger.info(f"[{AGENT_NAME}] Design accepted on attempt %d", attempt + 1)
                break
            else:
                logger.info(
                    f"[{AGENT_NAME}] Design rejected (attempt %d/%d): %s",
                    attempt + 1, PLANNING_MAX_DESIGN_RETRIES, tally.summary,
                )
                if attempt < PLANNING_MAX_DESIGN_RETRIES - 1:
                    # Feed rejection reasons back into next iteration via survey
                    rejection_feedback = "; ".join(tally.rejection_reasons)
                    survey_summary += f"\n\n[REVIEWER FEEDBACK from attempt {attempt+1}]: {rejection_feedback}"

        # Stage 4: Pitfall detection
        pitfalls = await self._stage_pitfall_detection(winning_design, survey_summary)

        # Stage 5: Plan consolidation
        consolidated = await self._stage_consolidation(
            winning_design, pitfalls, survey_summary
        )

        # Build result
        result = self._build_result(
            consolidated, best_designs, selected_index,
            review_votes, pitfalls, survey_summary
        )

        # Persist to database
        self._store_result(result)

        return result

    # ------------------------------------------------------------------
    # Stage 1: Codebase Survey
    # ------------------------------------------------------------------

    async def _stage_codebase_survey(self) -> str:
        """Run an agentic loop with read-only tools to survey the codebase."""
        from app.agent.tools import dispatch_tool

        system_prompt = (
            "You are a codebase surveyor. Your job is to understand the existing code "
            "structure relevant to the following task. Use the provided tools to read files, "
            "search for patterns, and understand the architecture. When done, output a "
            "comprehensive summary of your findings in a single message starting with "
            "'SURVEY_COMPLETE:' followed by your summary.\n\n"
            f"Task: {self.task_title}\n"
            f"Description: {self.task_description}"
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                "Survey the codebase to understand what exists and what needs to change "
                "for this task. Focus on: existing file structure, relevant modules, "
                "interfaces that will be affected, and test patterns."
            )},
        ]

        tool_schemas = _get_survey_tool_schemas()
        survey_result = ""
        _ctx_warned: set[float] = set()

        for turn in range(PLANNING_SURVEY_MAX_TURNS):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            response = await call_llm(
                messages,
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=PLANNING_LLM_TEMPERATURE,
                tools=tool_schemas,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=AGENT_NAME,
            )

            self._track_tokens(response)

            # Context saturation check
            if check_context_saturation(
                response.get("usage", {}).get("prompt_tokens", 0),
                self.max_context or 0,
                _ctx_warned,
                messages,
            ):
                logger.warning(f"[{AGENT_NAME}] Survey context saturation (turn %d) - terminating", turn + 1)
                break
            choice = response.get("choices", [{}])[0]
            msg = choice.get("message", {})
            content = msg.get("content", "")
            tool_calls = msg.get("tool_calls", [])

            messages.append(msg)

            # Check for completion signal
            if content and "SURVEY_COMPLETE:" in content:
                survey_result = content.split("SURVEY_COMPLETE:", 1)[1].strip()
                break

            # Dispatch tool calls
            if tool_calls:
                for tc in tool_calls:
                    fn_name = tc["function"]["name"]
                    fn_args = tc["function"]["arguments"] if isinstance(tc["function"]["arguments"], dict) else json.loads(tc["function"]["arguments"])
                    try:
                        result = dispatch_tool(fn_name, fn_args)
                    except Exception as e:
                        result = f"Error: {e}"
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": str(result)[:4000],
                    })
            elif not content:
                break

        if not survey_result:
            survey_result = content or "Survey completed without explicit summary."

        logger.info(f"[{AGENT_NAME}] Codebase survey completed (%d chars)", len(survey_result))
        return survey_result

    # ------------------------------------------------------------------
    # Stage 2: Best-of-N Design Generation
    # ------------------------------------------------------------------

    async def _stage_design_generation(self, survey: str) -> list[dict]:
        """Generate N design proposals in parallel with temperature spread."""
        system_prompt = (
            "You are a software architect. Based on the codebase survey and task description, "
            "produce a detailed implementation design. Output valid JSON with these keys:\n"
            "- design_rationale: string explaining the approach\n"
            "- file_manifest: list of {path, action, purpose, estimated_lines, depends_on}\n"
            "- dependency_graph: dict mapping component -> [dependencies]\n"
            "- interface_contracts: list of {component, provides, consumes, invariants}\n"
            "- test_strategy: list of {component, test_file, test_cases, fixtures}\n"
            "- implementation_steps: list of {order, component, files, description, depends_on, estimated_context_tokens}\n"
            "\nOutput ONLY the JSON object, no markdown fences."
        )

        user_msg = (
            f"Task: {self.task_title}\n"
            f"Description: {self.task_description}\n\n"
            f"Codebase Survey:\n{survey[:8000]}"
        )

        temperatures = PLANNING_TEMPERATURE_SPREAD[:PLANNING_BEST_OF_N]
        # Pad if needed
        while len(temperatures) < PLANNING_BEST_OF_N:
            temperatures.append(0.5)

        tasks = []
        for temp in temperatures:
            tasks.append(
                call_llm(
                    [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_msg},
                    ],
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=temp,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            )

        if is_shutting_down():
            raise ShutdownError("Server is shutting down")

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        designs = []
        for i, resp in enumerate(responses):
            if isinstance(resp, Exception):
                logger.warning(f"[{AGENT_NAME}] Design %d failed: %s", i, resp)
                designs.append({"error": str(resp)})
                continue
            self._track_tokens(resp)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            try:
                design = json.loads(content)
            except json.JSONDecodeError:
                design = {"raw": content, "parse_error": True}
            designs.append(design)

        logger.info(f"[{AGENT_NAME}] Generated %d designs", len(designs))
        return designs

    async def _stage_judge_designs(
        self, designs: list[dict], survey: str
    ) -> tuple[int, dict]:
        """Use a judge LLM call to select the best design."""
        valid = [(i, d) for i, d in enumerate(designs) if "error" not in d and "parse_error" not in d]
        if not valid:
            logger.warning(f"[{AGENT_NAME}] No valid designs, using first")
            return 0, designs[0] if designs else {}
        if len(valid) == 1:
            return valid[0]

        judge_prompt = (
            "You are a design judge. Compare these design proposals and select the best one. "
            "Output JSON: {\"selected_index\": <0-based index>, \"justification\": \"...\"}\n\n"
        )
        for orig_idx, design in valid:
            # Only send the rationale + file list - keeps the prompt small
            summary = {
                "rationale": str(design.get("design_rationale", ""))[:400],
                "files": [f.get("path", "") for f in design.get("file_manifest", [])[:8]],
            }
            judge_prompt += f"\n--- Design {orig_idx} ---\n{json.dumps(summary)}\n"

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": "You are a design evaluator. Output only JSON. Be concise."},
                    {"role": "user", "content": judge_prompt},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=PLANNING_JUDGE_TEMPERATURE,
                max_tokens=256,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=AGENT_NAME,
            )
            self._track_tokens(response)

            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            result = json.loads(content)
            idx = int(result.get("selected_index", valid[0][0]))
            if idx < 0 or idx >= len(designs):
                idx = valid[0][0]
        except Exception as exc:
            logger.warning(f"[{AGENT_NAME}] Judge call failed (%s), using first valid design %d", exc, valid[0][0])
            idx = valid[0][0]

        return idx, designs[idx]

    # ------------------------------------------------------------------
    # Stage 3: Design Review Panel
    # ------------------------------------------------------------------

    async def _stage_design_review(
        self, design: dict, survey: str
    ) -> list[Vote]:
        """Five parallel reviewers evaluate the winning design.

        Reviewers: coupling, interface, testability, security design, performance.
        Majority (>=3/5) required to pass.
        """
        reviewers = [
            {
                "name": "coupling_reviewer",
                "focus": (
                    "Review for coupling & dependencies: circular deps, over-coupling, "
                    "god objects, missing abstractions. Check dependency graph for issues."
                ),
            },
            {
                "name": "interface_reviewer",
                "focus": (
                    "Review for interface completeness: contract coverage, API documentation, "
                    "data flow explicitness. Every consumes must resolve to a provides."
                ),
            },
            {
                "name": "testability_reviewer",
                "focus": (
                    "Review for testability & safety: test strategy adequacy, destructive "
                    "operation risks, safety rule compliance (no hard deletes, branch isolation)."
                ),
            },
            {
                "name": "security_design_reviewer",
                "focus": (
                    "Review for security concerns IN THE DESIGN before any code is written. "
                    "Look for: authentication/authorization gaps, data flows exposing sensitive "
                    "information, API endpoints lacking security controls, missing encryption for "
                    "sensitive data at rest/transit, injection vulnerabilities in the proposed "
                    "architecture. If the design has fundamental security flaws, vote REJECTED "
                    "and include 'demotion_target: planning' in your justification."
                ),
            },
            {
                "name": "performance_reviewer",
                "focus": (
                    "Review for performance and scalability concerns. Look for: N+1 query "
                    "patterns in the proposed data model, missing caching strategy for expensive "
                    "operations, synchronous blocking in async code paths, unbounded data growth "
                    "(missing pagination/archival), proposed algorithms with poor time/space "
                    "complexity. If the design has fundamental scalability flaws, vote REJECTED "
                    "and include 'demotion_target: planning' in your justification."
                ),
            },
        ]

        design_summary = json.dumps(design, indent=1)[:6000]

        tasks = []
        for reviewer in reviewers:
            prompt = (
                f"You are reviewing a software design from the perspective of: {reviewer['focus']}\n\n"
                f"Design:\n{design_summary}\n\n"
                "Output JSON with: {\"verdict\": \"LIKELY|POSSIBLE|NEEDS_RESEARCH|NOT_SUITABLE|REJECTED\", "
                "\"confidence\": <0-100>, \"justification\": \"...\"}"
            )
            tasks.append(
                call_llm(
                    [
                        {"role": "system", "content": "You are a design reviewer. Output only JSON."},
                        {"role": "user", "content": prompt},
                    ],
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    temperature=PLANNING_LLM_TEMPERATURE,
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
            )

        responses = await asyncio.gather(*tasks, return_exceptions=True)

        votes: list[Vote] = []
        for i, resp in enumerate(responses):
            reviewer_name = reviewers[i]["name"]
            if isinstance(resp, Exception):
                if isinstance(resp, ShutdownError):
                    raise resp  # don't bake transient shutdown into a permanent vote record
                logger.warning(f"[{AGENT_NAME}] Reviewer '%s' failed: %s", reviewer_name, resp)
                votes.append(Vote(
                    stage=reviewer_name,
                    verdict=Verdict.NEEDS_RESEARCH,
                    confidence=65,
                    justification=f"Reviewer failed: {resp}",
                ))
                continue

            self._track_tokens(resp)
            content = resp.get("choices", [{}])[0].get("message", {}).get("content", "")
            try:
                data = json.loads(content)
                verdict_str = data.get("verdict", "POSSIBLE").upper()
                verdict = Verdict(verdict_str)
                confidence = int(data.get("confidence", 80))
                # Clamp confidence to verdict range
                lo, hi = verdict.confidence_range
                confidence = max(lo, min(hi, confidence))
                justification = data.get("justification", "")
            except (json.JSONDecodeError, ValueError, KeyError):
                verdict = Verdict.POSSIBLE
                confidence = 80
                justification = content[:500]

            votes.append(Vote(
                stage=reviewer_name,
                verdict=verdict,
                confidence=confidence,
                justification=justification,
                raw_response=data if 'data' in dir() else None,
                model=self.llm_model or "",
            ))

        return votes

    # ------------------------------------------------------------------
    # Stage 4: Pitfall Detection
    # ------------------------------------------------------------------

    async def _stage_pitfall_detection(
        self, design: dict, survey: str
    ) -> list[dict]:
        """Deterministic checks + LLM edge case detection."""
        pitfalls: list[dict] = []

        # Deterministic: check dependency graph for cycles
        dep_graph = design.get("dependency_graph", {})
        if dep_graph:
            from app.agent.static_analysis import _detect_cycles
            cycles = _detect_cycles(dep_graph)
            if cycles:
                for cycle in cycles:
                    pitfalls.append({
                        "type": "circular_dependency",
                        "severity": "high",
                        "detail": f"Circular dependency: {' -> '.join(cycle)}",
                    })

        # Deterministic: file path safety
        from app.agent.tools import _assert_safe_path
        for entry in design.get("file_manifest", []):
            path = entry.get("path", "")
            if path:
                try:
                    _assert_safe_path(os.path.join(PROJECT_ROOT, path))
                except ValueError as e:
                    pitfalls.append({
                        "type": "unsafe_path",
                        "severity": "critical",
                        "detail": str(e),
                    })

        # LLM: edge case detection
        prompt = (
            "Analyze this design for potential pitfalls:\n"
            f"{json.dumps(design, indent=1)[:4000]}\n\n"
            "Look for: edge cases, implicit dependencies, race conditions, "
            "state management issues, migration risks.\n"
            "Output JSON: {\"pitfalls\": [{\"type\": \"...\", \"severity\": \"low|medium|high|critical\", \"detail\": \"...\"}]}"
        )

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": "You are a software quality analyst. Output only JSON."},
                    {"role": "user", "content": prompt},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=PLANNING_LLM_TEMPERATURE,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=AGENT_NAME,
            )
            self._track_tokens(response)
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            data = json.loads(content)
            pitfalls.extend(data.get("pitfalls", []))
        except Exception as e:
            logger.warning(f"[{AGENT_NAME}] Pitfall LLM check failed: %s", e)

        return pitfalls

    # ------------------------------------------------------------------
    # Stage 5: Plan Consolidation
    # ------------------------------------------------------------------

    async def _stage_consolidation(
        self, design: dict, pitfalls: list[dict], survey: str
    ) -> dict:
        """Merge the winning design with pitfall mitigations."""
        if not pitfalls:
            return design

        prompt = (
            "You have a winning design and identified pitfalls. "
            "Produce a consolidated final design that incorporates mitigations "
            "for the identified pitfalls. Output the same JSON structure as the original design.\n\n"
            f"Original design:\n{json.dumps(design, indent=1)[:4000]}\n\n"
            f"Pitfalls:\n{json.dumps(pitfalls, indent=1)[:2000]}"
        )

        try:
            response = await call_llm(
                [
                    {"role": "system", "content": "You are a software architect. Output only JSON."},
                    {"role": "user", "content": prompt},
                ],
                base_url=self.llm_base_url,
                model=self.llm_model,
                temperature=PLANNING_LLM_TEMPERATURE,
                task_id=self.task_id,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                agent_name=AGENT_NAME,
            )
            self._track_tokens(response)
            content = response.get("choices", [{}])[0].get("message", {}).get("content", "")
            return json.loads(content)
        except Exception as e:
            logger.warning(f"[{AGENT_NAME}] Consolidation failed: %s. Using original design.", e)
            return design

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _track_tokens(self, response: dict) -> None:
        usage = response.get("usage", {})
        self._total_prompt += usage.get("prompt_tokens", 0)
        self._total_completion += usage.get("completion_tokens", 0)

    def _build_result(
        self, consolidated: dict, all_designs: list[dict],
        selected_index: int, review_votes: list[Vote],
        pitfalls: list[dict], survey: str,
    ) -> PlanningResult:
        # Parse file manifest
        file_manifest = []
        for entry in consolidated.get("file_manifest", []):
            file_manifest.append(FileManifestEntry(
                path=entry.get("path", ""),
                action=entry.get("action", "create"),
                purpose=entry.get("purpose", ""),
                estimated_lines=entry.get("estimated_lines", 0),
                depends_on=entry.get("depends_on", []),
            ))

        # Parse interface contracts
        contracts = []
        for c in consolidated.get("interface_contracts", []):
            contracts.append(InterfaceContract(
                component=c.get("component", ""),
                provides=c.get("provides", []),
                consumes=c.get("consumes", []),
                invariants=c.get("invariants", []),
            ))

        # Parse test strategy
        test_strategy = []
        for t in consolidated.get("test_strategy", []):
            test_strategy.append(TestStrategyEntry(
                component=t.get("component", ""),
                test_file=t.get("test_file", ""),
                test_cases=t.get("test_cases", []),
                fixtures=t.get("fixtures", []),
            ))

        # Parse implementation steps
        steps = []
        for s in consolidated.get("implementation_steps", []):
            steps.append(ImplementationStep(
                order=s.get("order", 0),
                component=s.get("component", ""),
                files=s.get("files", []),
                description=s.get("description", ""),
                depends_on=s.get("depends_on", []),
                estimated_context_tokens=s.get("estimated_context_tokens", 0),
            ))

        return PlanningResult(
            task_id=self.task_id,
            design_rationale=consolidated.get("design_rationale", ""),
            file_manifest=file_manifest,
            dependency_graph=consolidated.get("dependency_graph", {}),
            interface_contracts=contracts,
            test_strategy=test_strategy,
            implementation_steps=sorted(steps, key=lambda s: s.order),
            pitfalls_identified=pitfalls,
            review_votes=review_votes,
            best_of_n_designs=all_designs,
            selected_design_index=selected_index,
            confidence=80,
            prompt_tokens=self._total_prompt,
            completion_tokens=self._total_completion,
        )

    def _store_result(self, result: PlanningResult) -> None:
        """Persist PlanningResult to the database.

        If ``self.run_row_id`` is set (i.e. the caller pre-created an
        ``in_progress`` row), update that row in-place so the row ID stays
        stable.  Otherwise fall back to creating a new row (scheduler path).
        """
        kwargs = dict(
            file_manifest=json.dumps([asdict(f) for f in result.file_manifest]),
            dependency_graph=json.dumps(result.dependency_graph),
            interface_contracts=json.dumps([asdict(c) for c in result.interface_contracts]),
            test_strategy=json.dumps([asdict(t) for t in result.test_strategy]),
            implementation_steps=json.dumps([asdict(s) for s in result.implementation_steps]),
            pitfalls_identified=json.dumps(result.pitfalls_identified),
            review_votes=json.dumps([
                {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
                 "justification": v.justification}
                for v in result.review_votes
            ]),
            best_of_n_designs=json.dumps(result.best_of_n_designs),
            selected_design_index=result.selected_design_index,
            confidence=result.confidence,
            prompt_tokens=result.prompt_tokens,
            completion_tokens=result.completion_tokens,
            status='active',
        )
        try:
            if self.run_row_id is not None:
                from app.database import update_planning_result
                from app.database.session import SessionLocal as _SL
                _db = _SL()
                try:
                    update_planning_result(_db, self.run_row_id, **kwargs)
                finally:
                    _db.close()
            else:
                from app.database import create_planning_result
                create_planning_result(task_id=result.task_id, **kwargs)
        except Exception as e:
            logger.error(f"[{AGENT_NAME}] Failed to store result: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_planning_pipeline(
    task_id: str,
    task_title: str,
    task_description: str,
    all_tasks: list[dict],
    *,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    max_context: int | None = None,
    project_path: str | None = None,
    run_row_id: int | None = None,
) -> dict:
    """Run the full planning pipeline and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)
    pipeline = PlanningPipeline(
        task_id=task_id,
        task_title=task_title,
        task_description=task_description,
        all_tasks=all_tasks,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=max_context,
        run_row_id=run_row_id,
    )
    result = await pipeline.run()
    return {
        "task_id": result.task_id,
        "outcome": "passed" if result.confidence >= 60 else "rejected",
        "design_rationale": result.design_rationale,
        "file_manifest": [asdict(f) for f in result.file_manifest],
        "dependency_graph": result.dependency_graph,
        "interface_contracts": [asdict(c) for c in result.interface_contracts],
        "test_strategy": [asdict(t) for t in result.test_strategy],
        "implementation_steps": [asdict(s) for s in result.implementation_steps],
        "pitfalls_identified": result.pitfalls_identified,
        "selected_design_index": result.selected_design_index,
        "confidence": result.confidence,
        "total_prompt_tokens": result.prompt_tokens,
        "total_completion_tokens": result.completion_tokens,
        "votes": [
            {"stage": v.stage, "verdict": v.verdict.value, "confidence": v.confidence,
             "justification": v.justification}
            for v in result.review_votes
        ],
    }
