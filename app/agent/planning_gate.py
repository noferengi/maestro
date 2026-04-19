"""
app/agent/planning_gate.py
--------------------------
7-check due diligence gate for PLANNING -> IN DEV transition.

All checks are deterministic except #6 (LLM feasibility re-check).
Results stored in transition_results with transition="planning_to_indev".
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from app.agent.config import (
    PLANNING_GATE_FEASIBILITY_RECHECK,
    PLANNING_GATE_CONTEXT_SAFETY_MARGIN,
    PIPELINE_DONE_STATUSES,
    PROJECT_ROOT,
)
from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError

logger = logging.getLogger(__name__)
AGENT_NAME = "Planning Gate"


@dataclass(slots=True)
class GateCheck:
    name: str
    passed: bool
    hard_fail: bool  # True = blocks advancement; False = warning only
    detail: str = ""


@dataclass(slots=True)
class GateResult:
    task_id: str
    passed: bool
    checks: list[GateCheck] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_check_unavailable: bool = False


class PlanningGate:
    """7-check due diligence for PLANNING -> IN DEV transition."""

    def __init__(
        self,
        task_id: str,
        planning_result: dict,
        all_tasks: list[dict],
        *,
        max_context: int | None = None,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
    ):
        self.task_id = task_id
        self.plan = planning_result
        self.all_tasks = all_tasks
        self.max_context = max_context or 100000
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id

    async def run(self) -> GateResult:
        """Execute all 7 checks and return the gate result."""
        from app.agent.llm_client import set_llm_session_context
        set_llm_session_context(AGENT_NAME)
        checks: list[GateCheck] = []

        # Check 1: Interface completeness
        checks.append(self._check_interface_completeness())

        # Check 2: Circular dependency detection
        checks.append(self._check_circular_dependencies())

        # Check 3: Test strategy completeness
        checks.append(self._check_test_strategy())

        # Check 4: Prerequisites resolved
        checks.append(self._check_prerequisites())

        # Check 5: File manifest safety
        checks.append(self._check_file_safety())

        # Check 6: Implementation feasibility re-check (LLM)
        prompt_tokens = 0
        completion_tokens = 0
        llm_check_unavailable = False
        if PLANNING_GATE_FEASIBILITY_RECHECK:
            check6, pt, ct, llm_check_unavailable = await self._check_feasibility()
            checks.append(check6)
            prompt_tokens += pt
            completion_tokens += ct
        else:
            checks.append(GateCheck(
                name="feasibility_recheck",
                passed=True,
                hard_fail=False,
                detail="Skipped (disabled in config)",
            ))

        # Check 7: Context budget
        checks.append(self._check_context_budget())

        # Overall pass: no hard failures
        hard_failures = [c for c in checks if not c.passed and c.hard_fail]
        passed = len(hard_failures) == 0

        logger.info(
            "[planning_gate] Task '%s': %s (%d/%d checks passed, %d hard failures)",
            self.task_id,
            "PASSED" if passed else "FAILED",
            sum(1 for c in checks if c.passed),
            len(checks),
            len(hard_failures),
        )

        return GateResult(
            task_id=self.task_id,
            passed=passed,
            checks=checks,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            llm_check_unavailable=llm_check_unavailable,
        )

    # ------------------------------------------------------------------
    # Check 1: Interface completeness
    # ------------------------------------------------------------------

    def _check_interface_completeness(self) -> GateCheck:
        """Every consumes resolves to a provides. Empty contracts are fine (simple task)."""
        contracts = self.plan.get("interface_contracts", [])
        if not contracts:
            # Many simple tasks have no cross-component interfaces — not a hard failure.
            return GateCheck(
                name="interface_completeness",
                passed=True,
                hard_fail=False,
                detail="No interface contracts defined (simple task — skipping).",
            )

        all_provides: set[str] = set()
        all_consumes: set[str] = set()

        for contract in contracts:
            provides = contract.get("provides", [])
            consumes = contract.get("consumes", [])
            all_provides.update(provides)
            all_consumes.update(consumes)

        unresolved = all_consumes - all_provides
        if unresolved:
            return GateCheck(
                name="interface_completeness",
                passed=False,
                hard_fail=True,
                detail=f"Unresolved consumes: {', '.join(sorted(unresolved))}",
            )

        return GateCheck(
            name="interface_completeness",
            passed=True,
            hard_fail=True,
            detail=f"{len(contracts)} contracts validated, all consumes resolved.",
        )

    # ------------------------------------------------------------------
    # Check 2: Circular dependency detection
    # ------------------------------------------------------------------

    def _check_circular_dependencies(self) -> GateCheck:
        """Detect cycles in the proposed dependency graph."""
        dep_graph = self.plan.get("dependency_graph", {})
        if not dep_graph:
            return GateCheck(
                name="circular_dependency",
                passed=True,
                hard_fail=True,
                detail="No dependency graph to check.",
            )

        from app.agent.static_analysis import _detect_cycles
        cycles = _detect_cycles(dep_graph)
        if cycles:
            cycle_strs = [" -> ".join(c) for c in cycles[:3]]
            return GateCheck(
                name="circular_dependency",
                passed=False,
                hard_fail=True,
                detail=f"{len(cycles)} cycle(s): {'; '.join(cycle_strs)}",
            )

        return GateCheck(
            name="circular_dependency",
            passed=True,
            hard_fail=True,
            detail=f"No cycles in {len(dep_graph)} nodes.",
        )

    # ------------------------------------------------------------------
    # Check 3: Test strategy completeness
    # ------------------------------------------------------------------

    def _check_test_strategy(self) -> GateCheck:
        """A test strategy must exist. Coverage matching is advisory only.

        The LLM names test subjects by component name (e.g. "UserService"), not
        by filename (e.g. "app/services/user.py"), so strict filename-to-component
        matching produces false failures. We hard-fail only on a completely absent
        test_strategy; unmatched files are soft-reported for visibility.
        """
        test_strategy = self.plan.get("test_strategy", [])
        if not test_strategy:
            return GateCheck(
                name="test_strategy",
                passed=False,
                hard_fail=True,
                detail="No test strategy defined at all.",
            )

        # Advisory: report files that appear completely unmentioned (soft only)
        file_manifest = self.plan.get("file_manifest", [])
        components_needing_tests = set()
        for entry in file_manifest:
            action = entry.get("action", "")
            path = entry.get("path", "")
            if action in ("create", "modify") and path.endswith(".py") and "test" not in path:
                components_needing_tests.add(path)

        tested_components = set()
        for ts in test_strategy:
            component = ts.get("component", "")
            if component:
                tested_components.add(component)
            for f in ts.get("test_cases", []):
                tested_components.add(f)

        untested = components_needing_tests - tested_components
        if untested:
            # Soft advisory — doesn't block the gate
            logger.debug(
                "[planning_gate] Test strategy advisory: %d file(s) not explicitly named: %s",
                len(untested), ", ".join(sorted(untested)[:5]),
            )

        return GateCheck(
            name="test_strategy",
            passed=True,
            hard_fail=True,
            detail=f"{len(test_strategy)} test strategies defined.",
        )

    # ------------------------------------------------------------------
    # Check 4: Prerequisites resolved
    # ------------------------------------------------------------------

    def _check_prerequisites(self) -> GateCheck:
        """All task prerequisites are in COMPLETED/ACCEPTED state."""
        task_dict = None
        for t in self.all_tasks:
            if t.get("id") == self.task_id:
                task_dict = t
                break

        if not task_dict:
            return GateCheck(
                name="prerequisites_resolved",
                passed=True,
                hard_fail=True,
                detail="Task not found in task list (assuming no prereqs).",
            )

        prereqs = task_dict.get("prerequisites", [])
        if not prereqs:
            return GateCheck(
                name="prerequisites_resolved",
                passed=True,
                hard_fail=True,
                detail="No prerequisites.",
            )

        task_by_id = {t["id"]: t for t in self.all_tasks}
        unfinished = []
        for pid in prereqs:
            ptask = task_by_id.get(pid)
            if not ptask or ptask.get("type", "").lower() not in PIPELINE_DONE_STATUSES:
                unfinished.append(pid)

        if unfinished:
            return GateCheck(
                name="prerequisites_resolved",
                passed=False,
                hard_fail=True,
                detail=f"Unfinished prerequisites: {', '.join(unfinished)}",
            )

        return GateCheck(
            name="prerequisites_resolved",
            passed=True,
            hard_fail=True,
            detail=f"All {len(prereqs)} prerequisites completed.",
        )

    # ------------------------------------------------------------------
    # Check 5: File manifest safety
    # ------------------------------------------------------------------

    def _check_file_safety(self) -> GateCheck:
        """All paths pass _assert_safe_path(), no blocked commands.

        _assert_safe_path() uses the effective_root set by set_task_git_cwd()
        (called by run_planning_gate() before this method runs), which means
        paths are validated against the task's own project root — not
        TheMaestro's source tree.
        """
        import os
        from app.agent.tools import _assert_safe_path

        file_manifest = self.plan.get("file_manifest", [])
        issues = []

        for entry in file_manifest:
            path = entry.get("path", "")
            if path:
                try:
                    # Pass path directly — _assert_safe_path resolves relative
                    # paths against _task_git_cwd (the task's project root).
                    _assert_safe_path(path)
                except ValueError as e:
                    issues.append(str(e))

        if issues:
            return GateCheck(
                name="file_safety",
                passed=False,
                hard_fail=True,
                detail=f"{len(issues)} safety violation(s): {issues[0]}",
            )

        return GateCheck(
            name="file_safety",
            passed=True,
            hard_fail=True,
            detail=f"All {len(file_manifest)} paths validated.",
        )

    # ------------------------------------------------------------------
    # Check 6: Implementation feasibility re-check (LLM)
    # ------------------------------------------------------------------

    async def _check_feasibility(self) -> tuple[GateCheck, int, int, bool]:
        """LLM confirms plan is still viable. Retries up to 3 times with exponential backoff.

        Returns (GateCheck, prompt_tokens, completion_tokens, llm_check_unavailable).
        llm_check_unavailable=True means all retries failed and the check was skipped.
        """
        from app.agent.llm_client import call_llm, extract_text_response

        prompt = (
            "Review this implementation plan for feasibility. Is it still viable?\n\n"
            f"Plan summary:\n{json.dumps(self.plan, indent=1)[:4000]}\n\n"
            "Output JSON: {\"feasible\": true/false, \"concerns\": [\"...\"]}"
        )
        messages = [
            {"role": "system", "content": "You are a feasibility reviewer. Output only JSON."},
            {"role": "user", "content": prompt},
        ]

        max_attempts = 3
        last_error: Exception | None = None

        for attempt in range(1, max_attempts + 1):
            if is_shutting_down():
                raise ShutdownError("Server is shutting down")

            try:
                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    response_format={"type": "json_object"},
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=AGENT_NAME,
                )
                usage = response.get("usage", {})
                pt = usage.get("prompt_tokens", 0)
                ct = usage.get("completion_tokens", 0)

                content = extract_text_response(response)
                data, _ = json.JSONDecoder().raw_decode(content.lstrip())
                feasible = data.get("feasible", True)
                concerns = data.get("concerns", [])

                return GateCheck(
                    name="feasibility_recheck",
                    passed=feasible,
                    hard_fail=False,
                    detail=f"{'Feasible' if feasible else 'Concerns'}: {'; '.join(concerns) if concerns else 'none'}",
                ), pt, ct, False

            except Exception as e:
                last_error = e
                logger.warning(
                    "[planning_gate] Gate check 6: LLM call failed (attempt %d/%d): %s",
                    attempt, max_attempts, e,
                )
                if attempt < max_attempts:
                    await asyncio.sleep(2 ** attempt)  # 2s, 4s

        logger.warning(
            "[planning_gate] Task '%s': feasibility_recheck unavailable after %d attempts - "
            "proceeding with warning. Last error: %s",
            self.task_id, max_attempts, last_error,
        )
        return GateCheck(
            name="feasibility_recheck",
            passed=True,
            hard_fail=False,
            detail=f"LLM feasibility recheck unavailable after {max_attempts} attempts",
        ), 0, 0, True

    # ------------------------------------------------------------------
    # Check 7: Context budget
    # ------------------------------------------------------------------

    def _check_context_budget(self) -> GateCheck:
        """Each implementation step fits within max_context * (1 - safety_margin)."""
        budget = int(self.max_context * (1 - PLANNING_GATE_CONTEXT_SAFETY_MARGIN))
        steps = self.plan.get("implementation_steps", [])
        oversized = []

        for step in steps:
            est = step.get("estimated_context_tokens", 0)
            if est > budget:
                component = step.get("component", "unknown")
                oversized.append(f"{component} ({est} > {budget})")

        if oversized:
            return GateCheck(
                name="context_budget",
                passed=False,
                hard_fail=True,
                detail=f"Oversized steps: {', '.join(oversized[:3])}",
            )

        return GateCheck(
            name="context_budget",
            passed=True,
            hard_fail=True,
            detail=f"All {len(steps)} steps within budget ({budget} tokens).",
        )


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_planning_gate(
    task_id: str,
    planning_result: dict,
    all_tasks: list[dict],
    *,
    max_context: int | None = None,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_path: str | None = None,
) -> dict:
    """Run the planning gate and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)
    gate = PlanningGate(
        task_id=task_id,
        planning_result=planning_result,
        all_tasks=all_tasks,
        max_context=max_context,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
    )
    result = await gate.run()
    return {
        "task_id": result.task_id,
        "passed": result.passed,
        "checks": [
            {"name": c.name, "passed": c.passed, "hard_fail": c.hard_fail, "detail": c.detail}
            for c in result.checks
        ],
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "llm_check_unavailable": result.llm_check_unavailable,
    }
