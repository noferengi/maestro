"""
app/agent/dev_orchestrator.py
-----------------------------
DevOrchestrator - batch execution of component agents.

Reads PlanningResult.implementation_steps, groups into dependency-resolved
batches, and runs ComponentLoops in parallel (up to LLM parallel_sessions).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.agent.config import (
    INDEV_COMPONENT_MAX_RETRIES,
    INDEV_TEST_FIX_MAX_RETRIES,
    INDEV_TEST_FIX_MAX_TURNS,
    PROJECT_ROOT,
)
from app.agent.component_loop import ComponentLoop, ComponentLoopResult
from app.agent.llm_client import ShutdownError

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DevOrchestratorResult:
    task_id: str
    status: str  # "ACCEPTED" | "REVERT_TO_DESIGN" | "ERROR"
    # REVERT_TO_DESIGN: agent explicitly signalled design is wrong → demote to planning
    # ERROR: transient infrastructure failure (loops, LLM errors, ctx saturation) → stay in indev
    batches_completed: int = 0
    total_batches: int = 0
    component_results: list[ComponentLoopResult] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
    test_output: str | None = None
    test_parsed: dict = field(default_factory=dict)
    error_detail: str | None = None


class DevOrchestrator:
    """Orchestrates batch execution of component implementation agents."""

    def __init__(
        self,
        task_id: str,
        planning_result: dict,
        *,
        max_parallel: int = 5,
        llm_base_url: str | None = None,
        llm_model: str | None = None,
        llm_id: int | None = None,
        budget_id: int | None = None,
        max_context: int = 0,
        review_feedback: str | None = None,
        project_path: str | None = None,
    ):
        self.task_id = task_id
        self.plan = planning_result
        self.max_parallel = max_parallel
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id
        self.max_context = max_context
        self.review_feedback = review_feedback
        self.project_path = project_path

    async def run(self) -> DevOrchestratorResult:
        """Execute all batches of components."""
        logger.info("[dev_orch] Starting for task '%s'", self.task_id)

        steps = self.plan.get("implementation_steps", [])
        if not steps:
            return DevOrchestratorResult(
                task_id=self.task_id,
                status="REVERT_TO_DESIGN",
                error_detail="Planning result has no implementation_steps — demoting to planning for re-run.",
            )

        # Bump run counter so this dispatch's results are isolated from prior runs.
        from app.database import get_latest_dev_run_number
        self._dev_run_number = get_latest_dev_run_number(self.task_id) + 1

        # Group steps into dependency-resolved batches
        batches = self._build_batches(steps)
        logger.info("[dev_orch] %d steps grouped into %d batches", len(steps), len(batches))

        all_results: list[ComponentLoopResult] = []
        all_files: set[str] = set()
        total_prompt = 0
        total_completion = 0

        planning_context = json.dumps(self.plan, indent=1)[:8000]

        for batch_idx, batch in enumerate(batches):
            logger.info(
                "[dev_orch] Executing batch %d/%d (%d components)",
                batch_idx + 1, len(batches), len(batch),
            )

            # Run components in parallel (limited by max_parallel)
            semaphore = asyncio.Semaphore(self.max_parallel)

            async def run_component(step: dict) -> ComponentLoopResult:
                async with semaphore:
                    return await self._run_single_component(
                        step, planning_context, batch_idx
                    )

            tasks = [run_component(step) for step in batch]
            results = await asyncio.gather(*tasks, return_exceptions=True)

            # Process results
            batch_failed = False
            for i, result in enumerate(results):
                if isinstance(result, Exception):
                    # Server shutdown mid-batch is infrastructure failure, not code failure.
                    # Re-raise so _run_indev_task's ShutdownError handler fires and the
                    # task stays in INDEV instead of being demoted to planning.
                    if isinstance(result, ShutdownError):
                        raise result
                    comp_result = ComponentLoopResult(
                        component_name=batch[i].get("component", f"batch{batch_idx}_step{i}"),
                        status="ERROR",
                        error_detail=str(result),
                    )
                    all_results.append(comp_result)
                    batch_failed = True
                else:
                    all_results.append(result)
                    total_prompt += result.prompt_tokens
                    total_completion += result.completion_tokens
                    all_files.update(result.files_changed)

                    if result.status not in ("ACCEPTED",):
                        batch_failed = True

                    # Store component result in DB
                    self._store_component_result(result, batch_idx, i, self._dev_run_number)

            if batch_failed:
                logger.warning("[dev_orch] Batch %d had failures, halting.", batch_idx + 1)
                failed_components = [r for r in all_results if r.status not in ("ACCEPTED",)]
                _lines = [f"Batch {batch_idx + 1}/{len(batches)} failed ({len(failed_components)} component(s)):"]
                for _r in failed_components[:3]:
                    _reason = _r.error_detail or _r.status
                    _lines.append(f"  • {_r.component_name}: {_reason[:300]}")
                # Only propagate REVERT_TO_DESIGN if the agent explicitly signalled it.
                # Transient failures (loops, LLM errors, context saturation) use ERROR so
                # the scheduler keeps the task in INDEV rather than demoting to planning.
                _is_design_signal = any(r.status == "REVERT_TO_DESIGN" for r in failed_components)
                return DevOrchestratorResult(
                    task_id=self.task_id,
                    status="REVERT_TO_DESIGN" if _is_design_signal else "ERROR",
                    batches_completed=batch_idx,
                    total_batches=len(batches),
                    component_results=all_results,
                    files_changed=sorted(all_files),
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                    error_detail="\n".join(_lines),
                )

            # Commit batch
            try:
                from app.agent.tools import git_commit
                git_commit(f"[Maestro] Batch {batch_idx + 1}/{len(batches)} for task {self.task_id}")
            except Exception as e:
                logger.warning("[dev_orch] Batch commit failed: %s", e)

        # Run full test suite
        test_passed, test_output, test_parsed = await self._run_full_tests()

        if not test_passed:
            # Before demoting to planning, give the agent targeted fix attempts.
            # This handles common cases like wrong base cases, off-by-one errors,
            # or import issues that don't require redesigning anything.
            for fix_attempt in range(1, INDEV_TEST_FIX_MAX_RETRIES + 1):
                logger.info(
                    "[dev_orch] Test suite failed — running test-fix loop %d/%d for task '%s'.",
                    fix_attempt, INDEV_TEST_FIX_MAX_RETRIES, self.task_id,
                )
                fix_tokens_p, fix_tokens_c, signalled_redesign = await self._run_test_fix_loop(
                    test_output, fix_attempt
                )
                total_prompt += fix_tokens_p
                total_completion += fix_tokens_c
                
                if signalled_redesign:
                    logger.warning("[dev_orch] Test-fix loop %d signalled NEEDS_REDESIGN.", fix_attempt)
                    return DevOrchestratorResult(
                        task_id=self.task_id,
                        status="REVERT_TO_DESIGN",
                        batches_completed=len(batches),
                        total_batches=len(batches),
                        component_results=all_results,
                        files_changed=sorted(all_files),
                        prompt_tokens=total_prompt,
                        completion_tokens=total_completion,
                        error_detail=f"Test-fix loop signalled redesign needed: {test_output[:500]}",
                    )

                test_passed, test_output, test_parsed = await self._run_full_tests()
                if test_passed:
                    logger.info(
                        "[dev_orch] Test-fix loop %d succeeded for task '%s'.",
                        fix_attempt, self.task_id,
                    )
                    break
                logger.warning(
                    "[dev_orch] Test-fix loop %d still failing for task '%s'.",
                    fix_attempt, self.task_id,
                )

        # Store test evidence as a special component result row
        self._store_test_evidence(test_output, test_parsed, self._dev_run_number)

        return DevOrchestratorResult(
            task_id=self.task_id,
            status="ACCEPTED" if test_passed else "REVERT_TO_DESIGN",
            batches_completed=len(batches),
            total_batches=len(batches),
            component_results=all_results,
            files_changed=sorted(all_files),
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            test_output=test_output,
            test_parsed=test_parsed,
            error_detail=None if test_passed else (
                f"Test suite failed after {INDEV_TEST_FIX_MAX_RETRIES} fix attempt(s).\n\n"
                f"Last test output:\n{test_output}"
            ),
        )

    def _build_batches(self, steps: list[dict]) -> list[list[dict]]:
        """Group implementation steps into dependency-resolved batches.

        After grouping by order, scans each batch for file-level conflicts.
        Any component that claims a file already claimed by an earlier component
        in the same batch is deferred to the next batch to prevent parallel writes.
        """
        order_map: dict[int, list[dict]] = {}
        for step in steps:
            order = step.get("order", 0)
            if order not in order_map:
                order_map[order] = []
            order_map[order].append(step)

        # Iteratively resolve file conflicts until no more exist
        changed = True
        while changed:
            changed = False
            for order in sorted(order_map.keys()):
                batch = order_map[order]
                seen_files: dict[str, str] = {}  # file -> first component name
                survivors: list[dict] = []
                deferred: list[dict] = []

                for step in batch:
                    comp = step.get("component", "unknown")
                    conflict_file: str | None = None
                    for f in step.get("files", []):
                        if f in seen_files:
                            conflict_file = f
                            break

                    if conflict_file is not None:
                        logger.warning(
                            "[dev_orch] Components '%s' and '%s' both claim '%s' "
                            "- serializing to avoid write conflict",
                            seen_files[conflict_file], comp, conflict_file,
                        )
                        deferred.append(step)
                        changed = True
                    else:
                        for f in step.get("files", []):
                            seen_files[f] = comp
                        survivors.append(step)

                if deferred:
                    order_map[order] = survivors
                    next_order = order + 1
                    if next_order not in order_map:
                        order_map[next_order] = []
                    order_map[next_order] = deferred + order_map[next_order]

        return [order_map[k] for k in sorted(order_map.keys()) if order_map[k]]

    async def _run_single_component(
        self, step: dict, planning_context: str, batch_idx: int
    ) -> ComponentLoopResult:
        """Run a single ComponentLoop with retries."""
        component_name = step.get("component", "unknown")

        # Build allowed write paths from the step's files
        from app.agent.tools import get_task_git_cwd
        _effective_root = get_task_git_cwd() or PROJECT_ROOT
        allowed_paths = []
        for fpath in step.get("files", []):
            abs_path = os.path.join(_effective_root, fpath)
            allowed_paths.append(abs_path)

        for retry in range(INDEV_COMPONENT_MAX_RETRIES + 1):
            loop = ComponentLoop(
                task_id=self.task_id,
                component_name=component_name,
                implementation_step=step,
                planning_context=planning_context,
                allowed_write_paths=allowed_paths,
                llm_base_url=self.llm_base_url,
                llm_model=self.llm_model,
                llm_id=self.llm_id,
                budget_id=self.budget_id,
                max_context=self.max_context,
                review_feedback=self.review_feedback,
            )
            result = await loop.run()

            if result.status == "ACCEPTED":
                return result

            if result.status == "TIMEOUT":
                logger.info("[dev_orch] Component '%s' timed out. Triggering inline research.", component_name)
                try:
                    from app.agent.research import run_research
                    research_question = (
                        f"The implementation of component '{component_name}' is timing out during tests. "
                        "Investigate the source code and the tests to identify any high-complexity algorithms "
                        "(like naive Fibonacci) or infinite loops that might be causing the hang."
                    )
                    research_result = await run_research(
                        question=research_question,
                        context={"component": component_name, "step": step},
                        task_id=self.task_id,
                        llm_id=self.llm_id,
                        budget_id=self.budget_id,
                        llm_base_url=self.llm_base_url,
                        llm_model=self.llm_model,
                    )
                    # Append findings to the review_feedback so the next retry (if any) sees it.
                    findings_note = (
                        f"\n\n[RESEARCH FINDINGS FOR TIMEOUT]\n"
                        f"Verdict: {research_result.vote.get('verdict', 'unknown')}\n"
                        f"Findings: {research_result.findings}"
                    )
                    if self.review_feedback:
                        self.review_feedback += findings_note
                    else:
                        self.review_feedback = findings_note
                except Exception as research_exc:
                    logger.warning("[dev_orch] Inline research failed: %s", research_exc)

            if retry < INDEV_COMPONENT_MAX_RETRIES:
                logger.info(
                    "[dev_orch] Component '%s' failed (retry %d/%d)",
                    component_name, retry + 1, INDEV_COMPONENT_MAX_RETRIES,
                )

        return result  # Last attempt result

    async def _run_full_tests(self) -> tuple[bool, str, dict]:
        """Run the full test suite and parse results.

        Returns:
            (passed, raw_output, parsed) where parsed is a dict from
            :py:func:`app.agent.test_parser.parse_pytest_output`.
        """
        import asyncio as _asyncio
        from app.agent.test_parser import parse_pytest_output
        from app.agent.tools import _venv_python
        if not self.project_path:
            return False, "Test runner error: no project_path configured", {}
        cwd = str(self.project_path)
        python_exe = _venv_python(cwd)
        try:
            proc = await _asyncio.create_subprocess_exec(
                python_exe, "-m", "pytest", "-x", "--tb=short", "-q",
                stdout=_asyncio.subprocess.PIPE,
                stderr=_asyncio.subprocess.STDOUT,
                cwd=cwd,
            )
            raw, _ = await _asyncio.wait_for(proc.communicate(), timeout=120)
            output = raw.decode("utf-8", errors="replace")
            parsed = parse_pytest_output(output)
            passed = parsed["all_passed"]
            return passed, output[:6000], parsed
        except Exception as e:
            msg = f"Test runner error: {e}"
            logger.warning("[dev_orch] %s", msg)
            return False, msg, {}

    async def _run_test_fix_loop(
        self, test_output: str, fix_attempt: int
    ) -> tuple[int, int, bool]:
        """Agentic loop that reads failure output and makes targeted fixes.

        Returns (prompt_tokens, completion_tokens, redesign_needed) consumed during the loop.
        Never raises — infrastructure errors are logged and the loop exits cleanly
        so the caller can re-run the test suite and decide whether to demote.
        """
        from app.agent.llm_client import call_llm, is_shutting_down, ShutdownError
        from app.agent.tools import dispatch_tool, TOOL_SCHEMAS, build_tool_schemas

        _FIX_TOOLS = {
            "read_file", "count_lines",
            "find_in_files", "find_files", "list_directory",
            "write_file", "append_file",
        }
        tool_schemas = build_tool_schemas(list(_FIX_TOOLS) + ["submit_work"])

        system_prompt = (
            "You are a software developer. All implementation batches completed but the "
            "test suite is still failing. Your job is to make the MINIMAL fix needed to "
            "pass the tests — do not rewrite the implementation.\n\n"
            "WORKFLOW:\n"
            "1. Read the test failure output carefully to identify the root cause.\n"
            "2. Read the relevant source file(s) to understand the current state.\n"
            "3. Apply the smallest possible fix (e.g., wrong base case, missing import, "
            "off-by-one error, wrong return value).\n"
            "4. When done, call the submit_work tool with signal='ACCEPTED'.\n"
            "5. If you determine the failure requires a design change beyond a code fix, "
            "call submit_work with signal='REVERT_TO_DESIGN'.\n\n"
            "Do NOT rewrite whole files. Target the specific failing assertion.\n"
            "No prose after calling submit_work."
        )

        messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": (
                f"Test-fix attempt {fix_attempt}/{INDEV_TEST_FIX_MAX_RETRIES}.\n\n"
                f"Failing test output:\n```\n{test_output}\n```\n\n"
                "Please identify the root cause and apply the minimal fix."
            )},
        ]

        total_prompt = 0
        total_completion = 0
        redesign_needed = False

        try:
            for turn in range(INDEV_TEST_FIX_MAX_TURNS):
                if is_shutting_down():
                    raise ShutdownError("shutting down")

                response = await call_llm(
                    messages,
                    base_url=self.llm_base_url,
                    model=self.llm_model,
                    tools=tool_schemas,
                    tool_choice="auto",
                    task_id=self.task_id,
                    llm_id=self.llm_id,
                    budget_id=self.budget_id,
                    agent_name=f"DevOrchestrator[test-fix-{fix_attempt}]",
                )
                usage = response.get("usage", {})
                total_prompt += usage.get("prompt_tokens", 0)
                total_completion += usage.get("completion_tokens", 0)

                choice = response.get("choices", [{}])[0]
                msg = choice.get("message", {})
                content = msg.get("content", "") or ""
                tool_calls = msg.get("tool_calls") or []

                messages.append(msg)

                if tool_calls:
                    terminal_found = False
                    for tc in tool_calls:
                        fn_name = tc["function"]["name"]
                        fn_args_raw = tc["function"]["arguments"]
                        fn_args = fn_args_raw if isinstance(fn_args_raw, dict) else json.loads(fn_args_raw)
                        try:
                            result_str = str(dispatch_tool(fn_name, fn_args))
                            if "__maestro_terminal__" in result_str:
                                terminal_found = True
                                data = json.loads(result_str)
                                if data.get("signal") == "REVERT_TO_DESIGN":
                                    redesign_needed = True
                        except Exception as exc:
                            result_str = f"Error: {exc}"
                        
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc["id"],
                            "content": result_str[:4000],
                        })
                    
                    if terminal_found:
                        break
                elif not content:
                    break  # empty response — stop gracefully
                else:
                    # Nudge if no tool calls
                    messages.append({
                        "role": "user",
                        "content": "[SYSTEM] You must call submit_work when your fix is applied or if a redesign is needed."
                    })

        except ShutdownError:
            logger.info("[dev_orch] test-fix-%d interrupted by shutdown.", fix_attempt)
        except Exception as exc:
            logger.warning("[dev_orch] test-fix-%d loop error: %s", fix_attempt, exc)

        return total_prompt, total_completion, redesign_needed

    def _store_component_result(
        self, result: ComponentLoopResult, batch_number: int, step_order: int,
        dev_run_number: int = 0,
    ) -> None:
        """Persist a component result to the database."""
        try:
            from app.database import create_component_result
            create_component_result(
                task_id=self.task_id,
                component_name=result.component_name,
                step_order=step_order,
                batch_number=batch_number,
                dev_run_number=dev_run_number,
                status=result.status,
                files_changed=json.dumps(result.files_changed),
                tests_passed=int(result.tests_passed),
                turns_used=result.turns,
                error_detail=result.error_detail,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                test_output=result.test_output,
                coverage_pct=result.coverage_pct,
                completed_at=datetime.now(timezone.utc) if result.status == "ACCEPTED" else None,
            )
        except Exception as e:
            logger.error("[dev_orch] Failed to store component result: %s", e)

    def _store_test_evidence(
        self, test_output: str | None, test_parsed: dict, dev_run_number: int,
    ) -> None:
        """Store the whole-task test run as a special component result row.

        Uses component_name='__tests__' so the frontend can identify it.
        """
        try:
            from app.database import create_component_result
            passed = test_parsed.get("passed")
            total = test_parsed.get("total")
            if passed is not None and total is not None:
                tests_passed_val = passed
            else:
                tests_passed_val = 1 if (test_output and "passed" in test_output.lower()) else 0
            create_component_result(
                task_id=self.task_id,
                component_name="__tests__",
                step_order=0,
                batch_number=0,
                dev_run_number=dev_run_number,
                status="ACCEPTED" if test_parsed.get("all_passed") else "FAILED",
                files_changed="[]",
                tests_passed=tests_passed_val,
                turns_used=0,
                error_detail=None,
                prompt_tokens=0,
                completion_tokens=0,
                test_output=test_output,
                coverage_pct=test_parsed.get("coverage_pct"),
                completed_at=datetime.now(timezone.utc),
            )
        except Exception as e:
            logger.error("[dev_orch] Failed to store test evidence: %s", e)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

async def run_dev_orchestrator(
    task_id: str,
    planning_result: dict,
    *,
    max_parallel: int = 5,
    llm_base_url: str | None = None,
    llm_model: str | None = None,
    llm_id: int | None = None,
    budget_id: int | None = None,
    project_path: str | None = None,
    review_feedback: str | None = None,
) -> dict:
    """Run the development orchestrator and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)

    _max_context = 0
    if llm_id is not None:
        from app.database import get_llm as _get_llm
        _llm_record = _get_llm(llm_id)
        if _llm_record is not None:
            _max_context = _llm_record.max_context or 0

    orch = DevOrchestrator(
        task_id=task_id,
        planning_result=planning_result,
        max_parallel=max_parallel,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
        max_context=_max_context,
        review_feedback=review_feedback,
        project_path=project_path,
    )
    result = await orch.run()
    return {
        "task_id": result.task_id,
        "status": result.status,
        "batches_completed": result.batches_completed,
        "total_batches": result.total_batches,
        "files_changed": result.files_changed,
        "prompt_tokens": result.prompt_tokens,
        "completion_tokens": result.completion_tokens,
        "error_detail": result.error_detail,
        "component_results": [
            {
                "component": r.component_name,
                "status": r.status,
                "turns": r.turns,
                "files_changed": r.files_changed,
            }
            for r in result.component_results
        ],
    }
