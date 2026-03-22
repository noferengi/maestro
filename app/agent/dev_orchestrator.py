"""
app/agent/dev_orchestrator.py
-----------------------------
DevOrchestrator — batch execution of component agents.

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
    PROJECT_ROOT,
    GIT_SAFETY_BRANCH_PREFIX,
)
from app.agent.component_loop import ComponentLoop, ComponentLoopResult

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class DevOrchestratorResult:
    task_id: str
    status: str  # "ACCEPTED" | "REVERT_TO_DESIGN" | "ERROR"
    batches_completed: int = 0
    total_batches: int = 0
    component_results: list[ComponentLoopResult] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    prompt_tokens: int = 0
    completion_tokens: int = 0
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
    ):
        self.task_id = task_id
        self.plan = planning_result
        self.max_parallel = max_parallel
        self.llm_base_url = llm_base_url
        self.llm_model = llm_model
        self.llm_id = llm_id
        self.budget_id = budget_id

    async def run(self) -> DevOrchestratorResult:
        """Execute all batches of components."""
        logger.info("[dev_orch] Starting for task '%s'", self.task_id)

        steps = self.plan.get("implementation_steps", [])
        if not steps:
            return DevOrchestratorResult(
                task_id=self.task_id,
                status="ERROR",
                error_detail="No implementation steps in planning result.",
            )

        # Create branch
        try:
            from app.agent.tools import git_create_branch
            branch = f"{GIT_SAFETY_BRANCH_PREFIX}{self.task_id}"
            git_create_branch(branch)
        except Exception as e:
            logger.warning("[dev_orch] Branch creation: %s (may already exist)", e)

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
                    self._store_component_result(result, batch_idx, i)

            if batch_failed:
                logger.warning("[dev_orch] Batch %d had failures, halting.", batch_idx + 1)
                return DevOrchestratorResult(
                    task_id=self.task_id,
                    status="REVERT_TO_DESIGN",
                    batches_completed=batch_idx,
                    total_batches=len(batches),
                    component_results=all_results,
                    files_changed=sorted(all_files),
                    prompt_tokens=total_prompt,
                    completion_tokens=total_completion,
                    error_detail=f"Batch {batch_idx + 1} failed.",
                )

            # Commit batch
            try:
                from app.agent.tools import git_commit
                git_commit(f"[Maestro] Batch {batch_idx + 1}/{len(batches)} for task {self.task_id}")
            except Exception as e:
                logger.warning("[dev_orch] Batch commit failed: %s", e)

        # Run full test suite
        test_passed = await self._run_full_tests()

        return DevOrchestratorResult(
            task_id=self.task_id,
            status="ACCEPTED" if test_passed else "REVERT_TO_DESIGN",
            batches_completed=len(batches),
            total_batches=len(batches),
            component_results=all_results,
            files_changed=sorted(all_files),
            prompt_tokens=total_prompt,
            completion_tokens=total_completion,
            error_detail=None if test_passed else "Full test suite failed after all batches.",
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
                            "— serializing to avoid write conflict",
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
        allowed_paths = []
        for fpath in step.get("files", []):
            abs_path = os.path.join(PROJECT_ROOT, fpath)
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
            )
            result = await loop.run()

            if result.status == "ACCEPTED":
                return result

            if retry < INDEV_COMPONENT_MAX_RETRIES:
                logger.info(
                    "[dev_orch] Component '%s' failed (retry %d/%d)",
                    component_name, retry + 1, INDEV_COMPONENT_MAX_RETRIES,
                )

        return result  # Last attempt result

    async def _run_full_tests(self) -> bool:
        """Run the full test suite and return True if passed."""
        from app.agent.tools import run_shell
        try:
            output = run_shell("python -m pytest app/tests/ -x --tb=short -q")
            return "failed" not in output.lower() and "error" not in output.lower()
        except Exception as e:
            logger.warning("[dev_orch] Full test suite failed: %s", e)
            return False

    def _store_component_result(
        self, result: ComponentLoopResult, batch_number: int, step_order: int
    ) -> None:
        """Persist a component result to the database."""
        try:
            from app.database import create_component_result
            create_component_result(
                task_id=self.task_id,
                component_name=result.component_name,
                step_order=step_order,
                batch_number=batch_number,
                status=result.status,
                files_changed=json.dumps(result.files_changed),
                tests_passed=result.tests_passed,
                turns_used=result.turns,
                error_detail=result.error_detail,
                prompt_tokens=result.prompt_tokens,
                completion_tokens=result.completion_tokens,
                completed_at=datetime.now(timezone.utc) if result.status == "ACCEPTED" else None,
            )
        except Exception as e:
            logger.error("[dev_orch] Failed to store component result: %s", e)


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
) -> dict:
    """Run the development orchestrator and return a result dict."""
    if project_path is not None:
        from app.agent.tools import set_task_git_cwd
        set_task_git_cwd(project_path)
    orch = DevOrchestrator(
        task_id=task_id,
        planning_result=planning_result,
        max_parallel=max_parallel,
        llm_base_url=llm_base_url,
        llm_model=llm_model,
        llm_id=llm_id,
        budget_id=budget_id,
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
