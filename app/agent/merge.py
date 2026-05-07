"""
app/agent/merge.py
------------------
Deterministic git merge workflow.

Virtual merge (dry_run=True):
  Uses a temporary isolated worktree so concurrent virtual merges never
  interfere with the main checkout. Steps:
  1. Verify branch maestro/task-{id} exists
  2. Create temp worktree at {project_path}/.maestro-mergetest-{task_id}/
  3. Merge --no-ff in the temp worktree
  4. If conflict and llm_id/budget_id provided: attempt LLM resolution
  5. Run full test suite in the temp worktree
  6. Tear down the temp worktree (always, via try/finally)
  7. Return virtual_passed / conflict / test_failure / error

Real merge (dry_run=False):
  Traditional checkout-main approach (human-triggered, sequential).
  On conflict → abort merge, record conflict status
  On test failure → reset HEAD~1, record test_failure
  On success → push / tag / update task status / create MergeRecord

Conflict statuses returned:
  "virtual_passed"  — dry run passed, advance to human_review
  "merged"          — real merge done
  "conflict"        — unresolvable conflict, demote task to indev
  "test_failure"    — tests failed post-merge, demote task to indev
  "error"           — infrastructure failure (missing branch, git error)
  "push_failure"    — real merge succeeded but push failed
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
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
    status: str  # "merged" | "conflict" | "test_failure" | "error" | "virtual_passed" | "push_failure"
    merge_commit_sha: str | None = None
    test_output: str | None = None
    error_detail: str | None = None
    branch_name: str = ""
    conflict_files: list[str] | None = None  # which files conflicted


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


def _get_base_branch(cwd: str) -> str | None:
    """Return 'main' or 'master', whichever exists as a local branch."""
    for candidate in ("main", "master"):
        rc, out = _git(["branch", "--list", candidate], cwd=cwd)
        if out.strip():
            return candidate
    return None


def _ensure_mergetest_gitignore(project_path: str) -> None:
    """Add .maestro-mergetest-*/ to .gitignore if not already present."""
    gitignore = os.path.join(project_path, ".gitignore")
    entry = ".maestro-mergetest-*/"
    try:
        if os.path.exists(gitignore):
            with open(gitignore, "r", encoding="utf-8", errors="replace") as f:
                if entry in f.read():
                    return
        with open(gitignore, "a", encoding="utf-8") as f:
            f.write(f"\n{entry}\n")
    except Exception as e:
        logger.warning("[merge] Could not update .gitignore for mergetest entry: %s", e)


def _run_tests(project_path: str, test_cwd: str) -> tuple[bool, str]:
    """Run the project test suite in test_cwd. Returns (passed, output)."""
    try:
        test_result = subprocess.run(
            ["python", "-m", "pytest", "app/tests/", "-x", "--tb=short", "-q"],
            capture_output=True,
            text=True,
            timeout=MERGE_TEST_TIMEOUT,
            cwd=test_cwd,
        )
        output = (test_result.stdout + test_result.stderr)[:4000]
        return test_result.returncode == 0, output
    except subprocess.TimeoutExpired:
        return False, f"Test suite timed out after {MERGE_TEST_TIMEOUT}s"
    except Exception as e:
        return False, f"Test execution error: {e}"


def _get_conflicting_files(cwd: str) -> list[str]:
    """Return list of files with unresolved merge conflicts."""
    rc, out = _git(["diff", "--name-only", "--diff-filter=U"], cwd=cwd)
    if rc != 0 or not out.strip():
        return []
    return [f.strip() for f in out.strip().splitlines() if f.strip()]


def _build_conflict_report(cwd: str, branch: str, conflict_files: list[str]) -> str:
    """Build a concise conflict report for task history injection."""
    lines = [f"Merge conflict between {branch} and base branch."]
    lines.append(f"Conflicting files ({len(conflict_files)}): {', '.join(conflict_files)}")
    for filepath in conflict_files[:2]:
        rc, diff_out = _git(["diff", filepath], cwd=cwd)
        if rc == 0 and diff_out:
            lines.append(f"\n--- {filepath} (diff excerpt) ---\n{diff_out[:600]}")
    return "\n".join(lines)


def _try_resolve_conflicts(
    task_id: str,
    branch: str,
    cwd: str,
    llm_id: int,
    budget_id: int,
) -> bool:
    """
    Attempt LLM-based resolution of all merge conflicts in cwd.
    Returns True if all conflicts resolved and merge committed, False otherwise.
    The caller must call `git merge --abort` if this returns False.
    """
    conflict_files = _get_conflicting_files(cwd)
    if not conflict_files:
        return False

    try:
        from app.database import get_llm
        llm = get_llm(llm_id)
        if not llm:
            logger.warning("[merge] Cannot resolve conflicts: llm_id=%s not found", llm_id)
            return False
        base_url = f"http://{llm.address}:{llm.port}/v1"
        model = llm.model
    except Exception as e:
        logger.warning("[merge] Cannot load LLM for conflict resolution: %s", e)
        return False

    for filepath in conflict_files:
        full_path = os.path.join(cwd, filepath)
        try:
            with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                conflicted_content = f.read()
        except Exception as e:
            logger.warning("[merge] Cannot read conflicted file %s: %s", filepath, e)
            return False

        rc, head_version = _git(["show", f"HEAD:{filepath}"], cwd=cwd)
        head_version = head_version if rc == 0 else ""

        rc, merge_version = _git(["show", f"MERGE_HEAD:{filepath}"], cwd=cwd)
        merge_version = merge_version if rc == 0 else ""

        resolved = _call_llm_for_resolution(
            filepath, conflicted_content, head_version, merge_version,
            task_id, base_url, model, llm_id, budget_id,
        )
        if resolved is None:
            logger.warning("[merge] LLM could not resolve conflict in %s", filepath)
            return False

        try:
            with open(full_path, "w", encoding="utf-8") as f:
                f.write(resolved)
        except Exception as e:
            logger.warning("[merge] Cannot write resolved file %s: %s", filepath, e)
            return False

        rc, out = _git(["add", filepath], cwd=cwd)
        if rc != 0:
            logger.warning("[merge] git add failed for %s: %s", filepath, out)
            return False

    # All files resolved — commit the merge
    rc, out = _git(["commit", "--no-edit"], cwd=cwd)
    if rc != 0:
        logger.warning("[merge] git commit after resolution failed: %s", out)
        return False

    logger.info("[merge] Conflict resolution succeeded for task '%s' (%d files)", task_id, len(conflict_files))
    return True


def _call_llm_for_resolution(
    filepath: str,
    conflicted: str,
    head_version: str,
    merge_version: str,
    task_id: str,
    base_url: str,
    model: str,
    llm_id: int,
    budget_id: int,
) -> str | None:
    """
    Ask the LLM to resolve a single file's merge conflict.
    Returns resolved file content, or None if resolution failed.
    """
    from app.agent.llm_client import call_llm

    MAX_CHARS = 6000
    messages = [
        {
            "role": "system",
            "content": (
                "You are a precise merge conflict resolver. You will receive a file that contains "
                "git merge conflict markers (<<<<<<< HEAD, =======, >>>>>>> branch), along with "
                "the original base version and the incoming branch version. "
                "Your task: synthesize both changes into a single coherent file. "
                "Preserve all functionality from both sides. Remove all conflict markers. "
                "Return ONLY the complete resolved file content — no commentary, no code fences, "
                "no explanation. The output must be valid source code."
            ),
        },
        {
            "role": "user",
            "content": (
                f"File: `{filepath}`\n\n"
                f"=== FILE WITH CONFLICT MARKERS ===\n{conflicted[:MAX_CHARS]}\n\n"
                f"=== BASE (main branch) VERSION ===\n{head_version[:MAX_CHARS]}\n\n"
                f"=== INCOMING (task branch) VERSION ===\n{merge_version[:MAX_CHARS]}\n\n"
                "Resolve the conflict. Return ONLY the complete merged file content."
            ),
        },
    ]

    loop = asyncio.new_event_loop()
    try:
        response = loop.run_until_complete(
            call_llm(
                messages,
                base_url=base_url,
                model=model,
                max_tokens=4096,
                task_id=task_id,
                llm_id=llm_id,
                budget_id=budget_id,
                agent_name="Conflict Resolution",
                total_timeout_secs=120.0,
            )
        )
    except Exception as e:
        logger.warning("[merge] LLM call for conflict resolution failed (%s): %s", filepath, e)
        return None
    finally:
        loop.close()

    try:
        content = response["choices"][0]["message"]["content"] or ""
        # Reject if conflict markers survived
        if "<<<<<<<" in content or "=======" in content or ">>>>>>>" in content:
            logger.warning("[merge] LLM resolution for %s still contains conflict markers", filepath)
            return None
        return content if content.strip() else None
    except Exception as e:
        logger.warning("[merge] Could not extract LLM resolution for %s: %s", filepath, e)
        return None


def _virtual_merge(
    task_id: str,
    branch: str,
    effective_cwd: str,
    base_branch: str,
    llm_id: int | None,
    budget_id: int | None,
    attempt_resolution: bool,
) -> MergeResult:
    """
    Perform a virtual (dry-run) merge in a temporary isolated worktree.
    The main project checkout is never touched.
    """
    mergetest_path = os.path.join(effective_cwd, f".maestro-mergetest-{task_id}")
    _ensure_mergetest_gitignore(effective_cwd)

    # Clean up any stale mergetest worktree from a previous crashed run
    if os.path.exists(mergetest_path):
        logger.info("[merge] Cleaning up stale mergetest worktree at %s", mergetest_path)
        _git(["worktree", "remove", "--force", mergetest_path], cwd=effective_cwd)
    _git(["worktree", "prune"], cwd=effective_cwd)

    rc, out = _git(["worktree", "add", "--detach", mergetest_path, base_branch], cwd=effective_cwd)
    if rc != 0:
        _store_merge_record(task_id, branch, None, "error", None,
                            error_detail=f"Cannot create merge-test worktree: {out[:300]}")
        return MergeResult(
            task_id=task_id, status="error",
            error_detail=f"Cannot create merge-test worktree: {out[:300]}",
            branch_name=branch,
        )

    try:
        rc, out = _git(
            ["merge", "--no-ff", branch, "-m", f"[mergetest] {branch} into {base_branch}"],
            cwd=mergetest_path,
        )

        if rc != 0:
            conflict_files = _get_conflicting_files(mergetest_path)

            resolved = False
            if attempt_resolution and llm_id and budget_id:
                logger.info("[merge] Attempting LLM conflict resolution for task '%s'", task_id)
                resolved = _try_resolve_conflicts(task_id, branch, mergetest_path, llm_id, budget_id)

            if not resolved:
                conflict_detail = _build_conflict_report(mergetest_path, branch, conflict_files)
                _git(["merge", "--abort"], cwd=mergetest_path)
                _store_merge_record(task_id, branch, None, "conflict", None,
                                    error_detail=conflict_detail[:500])
                return MergeResult(
                    task_id=task_id, status="conflict",
                    error_detail=conflict_detail,
                    branch_name=branch,
                    conflict_files=conflict_files,
                )

        rc, sha = _git(["rev-parse", "HEAD"], cwd=mergetest_path)
        merge_sha = sha.strip() if rc == 0 else None

        test_passed, test_output = _run_tests(effective_cwd, mergetest_path)
        if not test_passed:
            _store_merge_record(task_id, branch, merge_sha, "test_failure", test_output,
                                error_detail="Tests failed after merge.")
            return MergeResult(
                task_id=task_id, status="test_failure",
                merge_commit_sha=merge_sha, test_output=test_output,
                error_detail="Tests failed after merge.",
                branch_name=branch,
            )

        _store_merge_record(task_id, branch, merge_sha, "virtual_passed", test_output)
        return MergeResult(
            task_id=task_id, status="virtual_passed",
            merge_commit_sha=merge_sha, test_output=test_output,
            branch_name=branch,
        )

    finally:
        _git(["worktree", "remove", "--force", mergetest_path], cwd=effective_cwd)
        _git(["worktree", "prune"], cwd=effective_cwd)


def execute_merge(
    task_id: str,
    project_path: str | None = None,
    dry_run: bool = False,
    llm_id: int | None = None,
    budget_id: int | None = None,
    attempt_resolution: bool = True,
) -> MergeResult:
    """
    Execute the merge workflow for a task branch.

    dry_run=True  — virtual merge test. Uses an isolated temporary worktree.
                    Concurrent-safe. Returns virtual_passed/conflict/test_failure/error.
                    Attempts LLM conflict resolution when llm_id+budget_id are provided.
    dry_run=False — real merge into main/master. Traditional checkout approach.
                    Pushes if MERGE_AUTO_PUSH=True. Updates task to 'completed'.
    """
    effective_cwd = project_path or PROJECT_ROOT
    logger.info("[merge] project_dir=%s dry_run=%s task=%s", effective_cwd, dry_run, task_id)
    ensure_git_repo(effective_cwd)

    branch = f"{GIT_SAFETY_BRANCH_PREFIX}{task_id}"

    # Step 1: Verify branch exists
    rc, out = _git(["branch", "--list", branch], cwd=effective_cwd)
    if not out.strip():
        rc, out = _git(["ls-remote", "--heads", "origin", branch], cwd=effective_cwd)
        if not out.strip():
            _store_merge_record(task_id, branch, None, "error", None,
                                error_detail=f"Branch '{branch}' not found locally or on remote.")
            return MergeResult(
                task_id=task_id, status="error",
                error_detail=f"Branch '{branch}' not found locally or on remote.",
                branch_name=branch,
            )

    # Step 2: Determine base branch
    base_branch = _get_base_branch(effective_cwd)
    if not base_branch:
        _store_merge_record(task_id, branch, None, "error", None,
                            error_detail="No main or master branch found in project.")
        return MergeResult(
            task_id=task_id, status="error",
            error_detail="No main or master branch found in project.",
            branch_name=branch,
        )

    # --- Virtual merge path: isolated temp worktree ---
    if dry_run:
        return _virtual_merge(task_id, branch, effective_cwd, base_branch,
                              llm_id, budget_id, attempt_resolution)

    # --- Real merge path: traditional checkout approach ---
    rc, out = _git(["checkout", base_branch], cwd=effective_cwd)
    if rc != 0:
        _store_merge_record(task_id, branch, None, "error", None,
                            error_detail=f"Cannot checkout {base_branch}: {out}")
        return MergeResult(
            task_id=task_id, status="error",
            error_detail=f"Cannot checkout {base_branch}: {out}",
            branch_name=branch,
        )

    rc, out = _git(["pull", "--ff-only"], cwd=effective_cwd)
    if rc != 0:
        logger.warning("[merge] Pull failed (non-fatal): %s", out)

    rc, out = _git(
        ["merge", "--no-ff", branch, "-m", f"Merge {branch} into {base_branch} (Maestro task {task_id})"],
        cwd=effective_cwd,
    )
    if rc != 0:
        conflict_files = _get_conflicting_files(effective_cwd)
        conflict_detail = _build_conflict_report(effective_cwd, branch, conflict_files)
        _git(["merge", "--abort"], cwd=effective_cwd)
        _store_merge_record(task_id, branch, None, "conflict", None,
                            error_detail=conflict_detail[:500])
        return MergeResult(
            task_id=task_id, status="conflict",
            error_detail=conflict_detail,
            branch_name=branch,
            conflict_files=conflict_files,
        )

    rc, sha = _git(["rev-parse", "HEAD"], cwd=effective_cwd)
    merge_sha = sha.strip() if rc == 0 else None

    test_passed, test_output = _run_tests(effective_cwd, effective_cwd)
    if not test_passed:
        _git(["reset", "--hard", "HEAD~1"], cwd=effective_cwd)
        _store_merge_record(task_id, branch, merge_sha, "test_failure", test_output,
                            error_detail="Tests failed after merge. Merge reverted.")
        return MergeResult(
            task_id=task_id, status="test_failure",
            merge_commit_sha=merge_sha, test_output=test_output,
            error_detail="Tests failed after merge. Merge reverted.",
            branch_name=branch,
        )

    # Push (if configured)
    if MERGE_AUTO_PUSH:
        import time as _time
        push_success = False
        last_push_err = ""
        for attempt in range(MERGE_PUSH_RETRIES):
            rc, out = _git(["push"], cwd=effective_cwd)
            if rc == 0:
                push_success = True
                break
            last_push_err = out
            logger.warning("[merge] Push attempt %d/%d failed: %s", attempt + 1, MERGE_PUSH_RETRIES, out)
            if attempt < MERGE_PUSH_RETRIES - 1:
                _time.sleep(2 ** (attempt + 1))
        if not push_success:
            _store_merge_record(task_id, branch, merge_sha, "push_failure", test_output,
                                error_detail=last_push_err[:500])
            return MergeResult(
                task_id=task_id, status="push_failure",
                merge_commit_sha=merge_sha, test_output=test_output,
                error_detail=f"Push failed after {MERGE_PUSH_RETRIES} attempts: {last_push_err[:500]}",
                branch_name=branch,
            )

    try:
        from app.database import update_task
        update_task(task_id, type="completed")
        logger.info("[merge] Task '%s' marked as completed.", task_id)
    except Exception as e:
        logger.error("[merge] Failed to update task: %s", e)

    if MERGE_TAG_BRANCHES:
        tag_name = f"merged/task-{task_id}"
        rc, out = _git(["tag", tag_name, branch], cwd=effective_cwd)
        if rc != 0:
            logger.warning("[merge] Failed to tag branch: %s", out)

    _store_merge_record(task_id, branch, merge_sha, "merged", test_output)
    logger.info("[merge] Task '%s' successfully merged to %s.", task_id, base_branch)

    return MergeResult(
        task_id=task_id, status="merged",
        merge_commit_sha=merge_sha, test_output=test_output,
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
