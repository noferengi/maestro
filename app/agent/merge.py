"""
app/agent/merge.py
------------------
Deterministic git merge workflow — NO LLM.

Steps:
  1. Verify branch maestro/task-{id} exists
  2. Checkout main, pull latest
  3. Merge --no-ff (preserve branch history)
  4. Run full test suite (pytest, configurable timeout)
  5. Push to origin (if configured)
  6. Update task type to "completed"
  7. Tag branch: merged/task-{id} (if configured)
  8. Create MergeRecord audit trail

On conflict → abort merge, demote to development
On test failure → reset HEAD~1, demote to development
"""

from __future__ import annotations

import logging
import subprocess
import os
from dataclasses import dataclass
from typing import Any

from app.agent.config import (
    MERGE_TEST_TIMEOUT,
    MERGE_AUTO_PUSH,
    MERGE_TAG_BRANCHES,
    MERGE_DELETE_BRANCHES,
    MERGE_PUSH_RETRIES,
    PROJECT_ROOT,
    GIT_SAFETY_BRANCH_PREFIX,
)
from app.agent.tools import ensure_git_repo

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class MergeResult:
    task_id: str
    status: str  # "merged" | "conflict" | "test_failure" | "error"
    merge_commit_sha: str | None = None
    test_output: str | None = None
    error_detail: str | None = None
    branch_name: str = ""


def _git(args: list[str], timeout: int = 60, cwd: str | None = None) -> tuple[int, str]:
    """Run a git command and return (returncode, combined output)."""
    result = subprocess.run(
        ["git"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd or PROJECT_ROOT,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def execute_merge(task_id: str, project_path: str | None = None) -> MergeResult:
    """Execute the full deterministic merge workflow.

    This function is synchronous — no LLM calls, just git + pytest.
    """
    effective_cwd = project_path or PROJECT_ROOT
    logger.info("[merge] Using project directory: %s", effective_cwd)
    ensure_git_repo(effective_cwd)

    branch = f"{GIT_SAFETY_BRANCH_PREFIX}{task_id}"
    logger.info("[merge] Starting merge for task '%s' (branch: %s)", task_id, branch)

    # Step 1: Verify branch exists
    rc, out = _git(["branch", "--list", branch], cwd=effective_cwd)
    if not out.strip():
        # Also check remote
        rc, out = _git(["ls-remote", "--heads", "origin", branch], cwd=effective_cwd)
        if not out.strip():
            return MergeResult(
                task_id=task_id,
                status="error",
                error_detail=f"Branch '{branch}' not found locally or on remote.",
                branch_name=branch,
            )

    # Step 2: Checkout main and pull
    rc, out = _git(["checkout", "main"], cwd=effective_cwd)
    if rc != 0:
        # Try master
        rc, out = _git(["checkout", "master"], cwd=effective_cwd)
        if rc != 0:
            return MergeResult(
                task_id=task_id,
                status="error",
                error_detail=f"Cannot checkout main/master: {out}",
                branch_name=branch,
            )

    rc, out = _git(["pull", "--ff-only"], cwd=effective_cwd)
    if rc != 0:
        logger.warning("[merge] Pull failed (non-fatal): %s", out)

    # Step 3: Merge --no-ff
    rc, out = _git(["merge", "--no-ff", branch, "-m",
                     f"Merge {branch} into main (Maestro task {task_id})"],
                    cwd=effective_cwd)
    if rc != 0:
        # Conflict — abort
        logger.warning("[merge] Merge conflict: %s", out)
        _git(["merge", "--abort"], cwd=effective_cwd)
        return MergeResult(
            task_id=task_id,
            status="conflict",
            error_detail=f"Merge conflict: {out[:500]}",
            branch_name=branch,
        )

    # Get merge commit SHA
    rc, sha = _git(["rev-parse", "HEAD"], cwd=effective_cwd)
    merge_sha = sha.strip() if rc == 0 else None

    # Step 4: Run full test suite
    logger.info("[merge] Running test suite (timeout: %ds)...", MERGE_TEST_TIMEOUT)
    try:
        test_result = subprocess.run(
            ["python", "-m", "pytest", "app/tests/", "-x", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=MERGE_TEST_TIMEOUT,
            cwd=effective_cwd,
        )
        test_output = (test_result.stdout + test_result.stderr)[:4000]
        test_passed = test_result.returncode == 0
    except subprocess.TimeoutExpired:
        test_output = f"Test suite timed out after {MERGE_TEST_TIMEOUT}s"
        test_passed = False
    except Exception as e:
        test_output = f"Test execution error: {e}"
        test_passed = False

    if not test_passed:
        # Revert: reset HEAD~1
        logger.warning("[merge] Tests failed, reverting merge.")
        _git(["reset", "--hard", "HEAD~1"], cwd=effective_cwd)
        return MergeResult(
            task_id=task_id,
            status="test_failure",
            merge_commit_sha=merge_sha,
            test_output=test_output,
            error_detail="Tests failed after merge. Merge reverted.",
            branch_name=branch,
        )

    # Step 5: Push (if configured) — with retry and exponential backoff
    if MERGE_AUTO_PUSH:
        import time as _time
        push_success = False
        last_push_err = ""
        for _attempt in range(MERGE_PUSH_RETRIES):
            rc, out = _git(["push"], cwd=effective_cwd)
            if rc == 0:
                push_success = True
                break
            last_push_err = out
            logger.warning(
                "[merge] Push attempt %d/%d failed: %s",
                _attempt + 1, MERGE_PUSH_RETRIES, out,
            )
            if _attempt < MERGE_PUSH_RETRIES - 1:
                _time.sleep(2 ** (_attempt + 1))  # 2s, 4s, ...
        if not push_success:
            _store_merge_record(task_id, branch, merge_sha, "push_failure",
                                test_output, error_detail=last_push_err[:500])
            return MergeResult(
                task_id=task_id,
                status="push_failure",
                merge_commit_sha=merge_sha,
                test_output=test_output,
                error_detail=f"Push failed after {MERGE_PUSH_RETRIES} attempts: {last_push_err[:500]}",
                branch_name=branch,
            )

    # Step 6: Update task
    try:
        from app.database import update_task
        update_task(task_id, type="completed")
        logger.info("[merge] Task '%s' marked as completed.", task_id)
    except Exception as e:
        logger.error("[merge] Failed to update task: %s", e)

    # Step 7: Tag branch (if configured)
    if MERGE_TAG_BRANCHES:
        tag_name = f"merged/task-{task_id}"
        rc, out = _git(["tag", tag_name, branch], cwd=effective_cwd)
        if rc != 0:
            logger.warning("[merge] Failed to tag branch: %s", out)

    # Step 8: Store audit trail
    _store_merge_record(task_id, branch, merge_sha, "merged", test_output)

    logger.info("[merge] Task '%s' successfully merged to main.", task_id)

    return MergeResult(
        task_id=task_id,
        status="merged",
        merge_commit_sha=merge_sha,
        test_output=test_output,
        branch_name=branch,
    )


def _store_merge_record(
    task_id: str,
    branch: str,
    sha: str | None,
    status: str,
    test_output: str | None,
    error_detail: str | None = None,
) -> None:
    """Persist merge record to database."""
    try:
        from app.database import create_merge_record
        create_merge_record(
            task_id=task_id,
            branch_name=branch,
            merge_commit_sha=sha,
            status=status,
            test_output=test_output,
            error_detail=error_detail,
        )
    except Exception as e:
        logger.error("[merge] Failed to store merge record: %s", e)
