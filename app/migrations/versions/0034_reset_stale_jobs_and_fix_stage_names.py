"""
Migration 0034 — reset stale background jobs and fix legacy stage names.

Two independent data repairs:

1. Background job cleanup
   ----------------------
   file_summary_jobs and research_jobs that are stuck in 'running' (orphaned by
   a server crash) or 'failed' (will never be retried under the old scheduler)
   are reset to 'pending' so the scheduler's new _rescue_stale_jobs() phase and
   the existing dispatch logic can pick them up on the next tick.

   completed_at is cleared so the auto-set-on-terminal-status logic in
   update_file_summary_job / update_research_job fires correctly on the next
   completion.

2. Legacy stage-name normalisation
   ---------------------------------
   Before the pipeline was finalised, a small number of seed/test tasks were
   created with stage names that pre-date the current pipeline columns:

     'development'  →  'indev'        (SCHEDULER_DISPATCHABLE_TYPES uses 'indev')
     'review'       →  'full_review'  (SCHEDULER_DISPATCHABLE_TYPES uses 'full_review')

   Tasks in these old stages are invisible to the scheduler and will never
   advance.  Renaming them to the canonical names lets the scheduler pick them
   up immediately.
"""

description = "reset stale background jobs; rename legacy stage names development->indev, review->full_review"

import json
from datetime import datetime, timezone


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def up(conn):
    # ------------------------------------------------------------------
    # 1. Reset stuck file_summary_jobs
    # ------------------------------------------------------------------
    cur = conn.execute(
        "SELECT COUNT(*) AS cnt FROM file_summary_jobs WHERE status IN ('running', 'failed')"
    )
    row = cur.fetchone()
    fsj_count = row["cnt"] if row else 0

    conn.execute(
        """
        UPDATE file_summary_jobs
           SET status      = 'pending',
               completed_at = NULL
         WHERE status IN ('running', 'failed')
        """
    )
    print(f"[0034] Reset {fsj_count} file_summary_jobs to 'pending'.")

    # ------------------------------------------------------------------
    # 2. Reset stuck research_jobs
    # ------------------------------------------------------------------
    cur = conn.execute(
        "SELECT COUNT(*) AS cnt FROM research_jobs WHERE status IN ('running', 'failed')"
    )
    row = cur.fetchone()
    rj_count = row["cnt"] if row else 0

    conn.execute(
        """
        UPDATE research_jobs
           SET status      = 'pending',
               completed_at = NULL
         WHERE status IN ('running', 'failed')
        """
    )
    print(f"[0034] Reset {rj_count} research_jobs to 'pending'.")

    # ------------------------------------------------------------------
    # 3. Rename legacy stage names
    # ------------------------------------------------------------------
    renames = [
        ("development", "indev"),
        ("review",      "full_review"),
    ]
    total_renamed = 0
    for old_type, new_type in renames:
        cur = conn.execute(
            "SELECT id, history FROM tasks WHERE type = ? AND is_active = 1",
            (old_type,),
        )
        rows = cur.fetchall()
        for row in rows:
            task_id, history_raw = row
            # Append a migration note to task history
            try:
                history = json.loads(history_raw) if history_raw else []
            except Exception:
                history = []
            history.append({
                "event":     "migration_0034_stage_rename",
                "old_type":  old_type,
                "new_type":  new_type,
                "timestamp": _now_iso(),
                "note":      f"Stage name normalised: '{old_type}' → '{new_type}' so scheduler can dispatch.",
            })
            conn.execute(
                "UPDATE tasks SET type = ?, history = ? WHERE id = ?",
                (new_type, json.dumps(history), task_id),
            )
            total_renamed += 1
            print(f"[0034]   Renamed task '{task_id}': {old_type} -> {new_type}")

    print(f"[0034] Renamed {total_renamed} tasks to canonical stage names.")
    conn.commit()


def down(conn):
    # ------------------------------------------------------------------
    # 1. Reverse stage-name renames (best-effort — history entries are kept)
    # ------------------------------------------------------------------
    renames = [
        ("indev",       "development"),
        ("full_review", "review"),
    ]
    # Only reverse tasks that have a migration_0034_stage_rename history entry
    # to avoid touching tasks that were legitimately in indev/full_review.
    total_reversed = 0
    for new_type, old_type in renames:
        cur = conn.execute(
            "SELECT id, history FROM tasks WHERE type = ? AND is_active = 1",
            (new_type,),
        )
        for row in cur.fetchall():
            task_id, history_raw = row
            try:
                history = json.loads(history_raw) if history_raw else []
            except Exception:
                history = []
            was_renamed = any(
                e.get("event") == "migration_0034_stage_rename" and e.get("old_type") == old_type
                for e in history
            )
            if was_renamed:
                conn.execute(
                    "UPDATE tasks SET type = ? WHERE id = ?",
                    (old_type, task_id),
                )
                total_reversed += 1
    print(f"[0034] down: Reversed {total_reversed} stage renames.")

    # ------------------------------------------------------------------
    # 2. Background jobs: no safe down — we cannot distinguish jobs that
    #    were reset by this migration from ones that were genuinely pending.
    #    Leave them as 'pending'; the scheduler will handle them.
    # ------------------------------------------------------------------
    print("[0034] down: background job resets are not reversible (left as 'pending').")
    conn.commit()
