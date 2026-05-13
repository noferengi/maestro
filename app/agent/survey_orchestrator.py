"""
app/agent/survey_orchestrator.py
-------------------------------
Coordinates multi-level project summarization (Survey mode).

Uses a context-aware "paging" strategy to handle large projects:

  Files (level 0)  → short_summary            [file_summaries table]
  Directories (L1) → directory scope_summary  [deterministic grouping]
  Modules (L2)     → module scope_summary     [LLM-driven grouping]
  Project (top)    → project scope_summary    [synthesis]

Freshness model: before enqueuing any job, check whether a fresh
scope_summary already exists with a matching content_hash.  If so, skip.
This prevents the Maestro from spinning up duplicate work on every tick.

Content hash: SHA1 of sorted "mtime:size" tuples for each file in the scope.
Fast (stat-only, no file reads) and sufficient for change detection.
"""

from __future__ import annotations

import os
import json
import hashlib
import logging
from datetime import datetime

from app.agent.config import (
    SUMMARY_CONTEXT_RATIO,
    SURVEY_STALENESS_ENABLED,
    SURVEY_DIRECTORY_MAX_FILES,
)
from app.database import (
    get_file_summaries_for_project_root,
    get_scope_summary,
    upsert_scope_summary,
    enqueue_scope_survey_job,
    list_scope_summaries,
)

logger = logging.getLogger(__name__)


class SurveyOrchestrator:
    """
    Coordinates multi-level project summarization.

    Entry point: ensure_project_surveyed(project_name, project_root, llm_id, budget_id)

    All deterministic work (file counting, content hashing, strategy selection,
    job creation) happens synchronously.  LLM calls happen asynchronously in the
    scheduler's job workers.  The orchestrator can be called from:
      - MaestroAgent (survey mode)
      - Project prewarm endpoint
      - Future "Re-survey" button in the SummaryBrowser UI
    """

    # -----------------------------------------------------------------------
    # Public entry point
    # -----------------------------------------------------------------------

    def ensure_project_surveyed(
        self,
        project_name: str,
        project_root: str,
        llm_id: int,
        budget_id: int,
    ) -> dict:
        """
        Synchronous pre-flight.  Returns a survey_status dict.
        Enqueues ScopeSurveyJob rows as needed; does NOT block on them.

        Freshness policy:
          - File summaries: enqueued via prewarm (enqueue_file_summary already
            checks sha1+size cache — no re-read if file unchanged).
          - Directory summaries: compared by content_hash (mtime+size of files);
            if hash matches the stored scope_summary, skip.
          - Module/project summaries: enqueued only when not already fresh.
        """
        from app.agent.path_filter import walk_safe

        project_root = os.path.normpath(os.path.abspath(project_root))
        logger.info(
            "[Survey] ensure_project_surveyed: project='%s' root='%s'",
            project_name, project_root,
        )

        # --- Tier 0: File Summaries ---
        from app.agent.project_snapshot import prewarm_project_summaries
        files_enqueued = prewarm_project_summaries(
            project_root, llm_id=llm_id, budget_id=budget_id
        )

        # --- Tier 1: Build directory map (rel_dir → list of abs file paths) ---
        dir_map: dict[str, list[str]] = {}
        for dirpath, dirs, files in walk_safe(project_root):
            rel_dir = os.path.relpath(dirpath, project_root).replace("\\", "/")
            if rel_dir == ".":
                rel_dir = ""
            for f in files:
                abs_file = os.path.join(dirpath, f)
                dir_map.setdefault(rel_dir, []).append(abs_file)

        # --- Tier 1: Enqueue directory jobs (skip if already fresh) ---
        dir_jobs_enqueued = self._enqueue_directory_jobs(
            project_name, project_root, dir_map, llm_id, budget_id
        )

        # --- Tier 2: Module clustering (skip if already fresh) ---
        mod_enqueued = self._enqueue_module_clustering_job(
            project_name, llm_id, budget_id
        )

        # --- Tier 3: Project summary (skip if already fresh) ---
        proj_enqueued = self._enqueue_project_summary_job(
            project_name, llm_id, budget_id
        )

        result = {
            "status": "initiated",
            "files_prewarmed": files_enqueued,
            "directory_jobs": dir_jobs_enqueued,
            "module_jobs_enqueued": mod_enqueued,
            "project_job_enqueued": proj_enqueued,
        }
        logger.info("[Survey] ensure_project_surveyed result: %s", result)
        return result

    # -----------------------------------------------------------------------
    # Read accessors (used by Maestro tools and SummaryBrowser)
    # -----------------------------------------------------------------------

    def get_project_summary(self, project_name: str) -> str | None:
        """Returns the top-level '__ROOT__' scope_summary text if fresh, else None."""
        summary = get_scope_summary(project_name, "project", "__ROOT__")
        if summary and summary.staleness_state == "fresh":
            return summary.summary
        return None

    def get_directory_summary(self, project_name: str, rel_dir: str) -> str | None:
        """Returns the directory-level summary for a relative directory path if fresh."""
        rel_dir = rel_dir.replace("\\", "/").strip("/")
        if not rel_dir:
            rel_dir = ""
        summary = get_scope_summary(project_name, "directory", rel_dir)
        if summary and summary.staleness_state == "fresh":
            return summary.summary
        return None

    def get_module_summary(self, project_name: str, module_name: str) -> str | None:
        """Returns the LLM-assigned module summary by name if fresh."""
        summary = get_scope_summary(project_name, "module", module_name)
        if summary and summary.staleness_state == "fresh":
            return summary.summary
        return None

    def list_scopes(
        self, project_name: str, scope_type: str | None = None
    ) -> list:
        """Returns all scope summaries for a project, optionally filtered by type."""
        return list_scope_summaries(project_name, scope_type)

    # -----------------------------------------------------------------------
    # Staleness trigger (called when files change)
    # -----------------------------------------------------------------------

    def _enqueue_staleness_check_jobs(
        self,
        project_name: str,
        changed_files: list[str],
        llm_id: int,
        budget_id: int,
    ) -> None:
        """Enqueue staleness-check jobs for scopes affected by changed_files."""
        if not SURVEY_STALENESS_ENABLED:
            return
        # Always mark the project-level summary as needing a staleness check
        enqueue_scope_survey_job(
            project_name, "project", "__ROOT__",
            action="staleness_check", priority=0.5,
            llm_id=llm_id, budget_id=budget_id,
        )
        # Mark each changed directory as stale too
        for path in changed_files:
            rel_dir = os.path.dirname(path).replace("\\", "/").strip("/")
            if rel_dir:
                enqueue_scope_survey_job(
                    project_name, "directory", rel_dir,
                    action="staleness_check", priority=0.5,
                    llm_id=llm_id, budget_id=budget_id,
                )

    # -----------------------------------------------------------------------
    # Strategy selection
    # -----------------------------------------------------------------------

    def _strategy(self, project_root: str, max_context: int) -> str:
        """
        Adaptive strategy based on file count and branching factor.

        Returns one of:
          'one_shot'        — entire project fits in a single LLM call
          'directory'       — one call per top-level directory
          'directory_module' — directories + LLM module clustering
          'recursive'       — nested paging for very large repos
        """
        from app.agent.path_filter import walk_safe

        total_files = 0
        top_dirs: set[str] = set()
        for dirpath, dirs, files in walk_safe(project_root):
            total_files += len(files)
            rel = os.path.relpath(dirpath, project_root)
            if rel != ".":
                top = rel.replace("\\", "/").split("/")[0]
                top_dirs.add(top)

        branching = self.get_branching_factor(max_context)
        if total_files <= branching:
            return "one_shot"
        if len(top_dirs) <= branching:
            return "directory"
        if total_files <= branching * branching:
            return "directory_module"
        return "recursive"

    # -----------------------------------------------------------------------
    # Internal job enqueuers (all with freshness checks)
    # -----------------------------------------------------------------------

    def _compute_dir_hash(self, file_paths: list[str]) -> str:
        """Fast content hash: SHA1 of sorted 'mtime:size' tuples for each file.

        Uses filesystem metadata only (no file reads) — O(n) stat calls.
        """
        parts: list[str] = []
        for p in file_paths:
            try:
                st = os.stat(p)
                parts.append(f"{st.st_mtime:.0f}:{st.st_size}")
            except OSError:
                parts.append(f"missing:{p}")
        combined = "|".join(sorted(parts))
        return hashlib.sha1(combined.encode("utf-8")).hexdigest()

    def _compute_content_hash(self, sha1_list: list[str]) -> str:
        """SHA1 of sorted file content hashes (for use when sha1s are available)."""
        combined = "".join(sorted(sha1_list))
        return hashlib.sha1(combined.encode("utf-8")).hexdigest()

    def _is_scope_fresh(
        self,
        project_name: str,
        scope_type: str,
        scope_key: str,
        content_hash: str | None = None,
    ) -> bool:
        """Return True if the stored scope_summary is fresh (and hash matches if given)."""
        record = get_scope_summary(project_name, scope_type, scope_key)
        if not record:
            return False
        if record.staleness_state != "fresh":
            return False
        if content_hash is not None and record.content_hash != content_hash:
            return False
        return True

    def _enqueue_directory_jobs(
        self,
        project_name: str,
        project_root: str,
        dir_map: dict[str, list[str]],
        llm_id: int,
        budget_id: int,
    ) -> int:
        """Enqueue directory summary jobs, skipping any that are already fresh.

        Splits directories with too many files into sub-directory pages.
        Returns number of jobs actually enqueued.
        """
        enqueued = 0
        for rel_dir, file_paths in dir_map.items():
            content_hash = self._compute_dir_hash(file_paths)

            if self._is_scope_fresh(project_name, "directory", rel_dir, content_hash):
                logger.debug(
                    "[Survey] dir '%s' already fresh (hash=%s) — skipping.",
                    rel_dir, content_hash[:8],
                )
                continue

            # Split oversized directories into multiple page jobs
            if len(file_paths) > SURVEY_DIRECTORY_MAX_FILES:
                pages = [
                    file_paths[i:i + SURVEY_DIRECTORY_MAX_FILES]
                    for i in range(0, len(file_paths), SURVEY_DIRECTORY_MAX_FILES)
                ]
                for page_idx, page_files in enumerate(pages):
                    page_key = f"{rel_dir}__page{page_idx}"
                    page_hash = self._compute_dir_hash(page_files)
                    if not self._is_scope_fresh(project_name, "directory", page_key, page_hash):
                        enqueue_scope_survey_job(
                            project_name, "directory", page_key,
                            action="generate", priority=1.0,
                            llm_id=llm_id, budget_id=budget_id,
                        )
                        enqueued += 1
            else:
                enqueue_scope_survey_job(
                    project_name, "directory", rel_dir,
                    action="generate", priority=1.0,
                    llm_id=llm_id, budget_id=budget_id,
                )
                enqueued += 1

        return enqueued

    def _enqueue_module_clustering_job(
        self, project_name: str, llm_id: int, budget_id: int
    ) -> bool:
        """Enqueue module clustering job if not already fresh. Returns True if enqueued."""
        if self._is_scope_fresh(project_name, "module_clustering", "__ROOT__"):
            logger.debug("[Survey] module clustering already fresh — skipping.")
            return False
        enqueue_scope_survey_job(
            project_name, "module_clustering", "__ROOT__",
            action="generate", priority=1.5,
            llm_id=llm_id, budget_id=budget_id,
        )
        return True

    def _enqueue_project_summary_job(
        self, project_name: str, llm_id: int, budget_id: int
    ) -> bool:
        """Enqueue top-level project summary job if not already fresh. Returns True if enqueued."""
        if self._is_scope_fresh(project_name, "project", "__ROOT__"):
            logger.debug("[Survey] project summary already fresh — skipping.")
            return False
        enqueue_scope_survey_job(
            project_name, "project", "__ROOT__",
            action="generate", priority=2.0,
            llm_id=llm_id, budget_id=budget_id,
        )
        return True

    # -----------------------------------------------------------------------
    # Utility
    # -----------------------------------------------------------------------

    def get_summary_context_limit(self, max_context: int) -> int:
        """Char limit for a single summary input block (3 chars/token, 10% of window)."""
        return int(max_context * SUMMARY_CONTEXT_RATIO) * 3

    def get_branching_factor(self, max_context: int) -> int:
        """How many child summaries fit in one LLM call at the 10% ratio."""
        # 1.0 / 0.10 = 10 slots; use 8 to leave 20% for response/instruction overhead.
        return max(3, int(1.0 / SUMMARY_CONTEXT_RATIO) - 2)
