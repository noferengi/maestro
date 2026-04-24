"""
CRUD operations for pipeline audit / result tables.

Each pipeline stage writes a result row on completion.  These tables are
write-once (create + optional update); callers never delete rows.

Tables covered:
  TransitionVote / TransitionResult  — intake pipeline voting (IDEA → PLANNING)
  SubdivisionRecord                  — subdivision attempt audit trail
  PlanningResult                     — planning pipeline output
  ComponentResult                    — per-component dev agent result
  OptimizationResult                 — optimization pipeline output
  SecurityReviewResult               — security review findings (has veto power)
  FullReviewResult                   — final review findings
  MergeRecord                        — merge-to-main operations

Note: update_*() functions for PlanningResult, OptimizationResult,
SecurityReviewResult, FullReviewResult, and MergeRecord accept an explicit
`db` session as their first argument (called from within an existing
transaction in the pipeline orchestrators).  All other functions open and
close their own sessions.
"""

import logging
from datetime import datetime, timezone

from .session import SessionLocal
from .models import (
    TransitionVote, TransitionResult, SubdivisionRecord,
    PlanningResult, ComponentResult, OptimizationResult,
    SecurityReviewResult, FullReviewResult, MergeRecord,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# TransitionVote / TransitionResult
# ---------------------------------------------------------------------------

def create_transition_vote(task_id, transition, stage, verdict, confidence, justification=None, raw_response=None, prompt_tokens=None, completion_tokens=None, model=None, budget_id=None):
    db = SessionLocal()
    try:
        vote = TransitionVote(
            task_id=task_id, transition=transition, stage=stage,
            verdict=verdict, confidence=confidence, justification=justification,
            raw_response=raw_response, prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens, model=model, budget_id=budget_id
        )
        db.add(vote)
        db.commit()
        db.refresh(vote)
        return vote
    except Exception as e:
        db.rollback()
        logger.error("Error creating transition vote: %s", e)
        return None
    finally:
        db.close()


def get_transition_votes(task_id, transition=None):
    db = SessionLocal()
    try:
        q = db.query(TransitionVote).filter(TransitionVote.task_id == task_id)
        if transition:
            q = q.filter(TransitionVote.transition == transition)
        return q.order_by(TransitionVote.created_at).all()
    finally:
        db.close()


def create_transition_result(task_id, transition, outcome, vote_summary=None, total_prompt_tokens=None, total_completion_tokens=None):
    db = SessionLocal()
    try:
        result = TransitionResult(
            task_id=task_id, transition=transition, outcome=outcome,
            vote_summary=vote_summary, total_prompt_tokens=total_prompt_tokens,
            total_completion_tokens=total_completion_tokens
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating transition result: %s", e)
        return None
    finally:
        db.close()


def get_transition_results(task_id, transition=None):
    db = SessionLocal()
    try:
        q = db.query(TransitionResult).filter(TransitionResult.task_id == task_id)
        if transition:
            q = q.filter(TransitionResult.transition == transition)
        return q.order_by(TransitionResult.created_at.desc()).all()
    finally:
        db.close()


def get_transition_votes_for_result(task_id, from_dt=None, to_dt=None):
    """Return votes for a task created in the window (from_dt, to_dt].

    Use to match votes to a specific transition result when there is no direct
    FK.  Pass the previous result's created_at as from_dt and the current
    result's created_at as to_dt.  Either bound may be None (open interval).
    """
    db = SessionLocal()
    try:
        q = (db.query(TransitionVote)
             .filter(TransitionVote.task_id == task_id)
             .filter(TransitionVote.transition == "idea_to_planning"))
        if from_dt is not None:
            q = q.filter(TransitionVote.created_at > from_dt)
        if to_dt is not None:
            q = q.filter(TransitionVote.created_at <= to_dt)
        return q.order_by(TransitionVote.created_at).all()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# SubdivisionRecord
# ---------------------------------------------------------------------------

def create_subdivision_record(parent_task_id, child_task_ids, generation=1,
                               attempt_number=1, rejection_context=None,
                               agent_vote=None, prompt_tokens=0,
                               completion_tokens=0, status='active',
                               interface_contracts=None):
    db = SessionLocal()
    try:
        record = SubdivisionRecord(
            parent_task_id=parent_task_id,
            attempt_number=attempt_number,
            generation=generation,
            child_task_ids=child_task_ids,
            rejection_context=rejection_context,
            agent_vote=agent_vote,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            interface_contracts=interface_contracts,
            status=status,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error creating subdivision record: %s", e)
        return None
    finally:
        db.close()


def get_subdivision_records(parent_task_id):
    """Get all subdivision records for a parent task, ordered by creation time."""
    db = SessionLocal()
    try:
        return (db.query(SubdivisionRecord)
                .filter(SubdivisionRecord.parent_task_id == parent_task_id)
                .order_by(SubdivisionRecord.created_at.desc())
                .all())
    finally:
        db.close()


def update_subdivision_record(record_id, **kwargs):
    """Update a subdivision record."""
    db = SessionLocal()
    try:
        record = db.query(SubdivisionRecord).filter(SubdivisionRecord.id == record_id).first()
        if not record:
            return None
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error updating subdivision record: %s", e)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# PlanningResult
# ---------------------------------------------------------------------------

def create_planning_result(task_id, **kwargs):
    db = SessionLocal()
    try:
        result = PlanningResult(task_id=task_id, **kwargs)
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating planning result: %s", e)
        return None
    finally:
        db.close()


def get_planning_result(task_id):
    """Get the latest active planning result for a task."""
    db = SessionLocal()
    try:
        return (db.query(PlanningResult)
                .filter(PlanningResult.task_id == task_id, PlanningResult.status == 'active')
                .order_by(PlanningResult.created_at.desc())
                .first())
    finally:
        db.close()


def supersede_planning_results(task_id: str) -> int:
    """Mark all active/in_progress planning results for a task as superseded.

    Called at the start of every planning run so the new run's row becomes
    the authoritative result regardless of what the old run produced.
    Returns the number of rows updated.
    """
    db = SessionLocal()
    try:
        rows = (db.query(PlanningResult)
                .filter(PlanningResult.task_id == task_id,
                        PlanningResult.status.in_(['active', 'in_progress']))
                .all())
        for row in rows:
            row.status = 'superseded'
        db.commit()
        return len(rows)
    except Exception as e:
        db.rollback()
        logger.error("Error superseding planning results for '%s': %s", task_id, e)
        return 0
    finally:
        db.close()


def get_latest_planning_result(task_id: str):
    """Return the most-recent planning result for a task, regardless of status.

    Used by the ``/planning-result`` API endpoint so the Stage Journal can
    display in_progress and failed states, not just completed ones.
    The existing ``get_planning_result()`` (status='active' filter) is
    unchanged — indev pipeline, conceptual review, and stage-summary
    continue to use it.
    """
    db = SessionLocal()
    try:
        return (db.query(PlanningResult)
                .filter(PlanningResult.task_id == task_id)
                .order_by(PlanningResult.created_at.desc())
                .first())
    finally:
        db.close()


def update_planning_result(db, result_id, **kwargs):
    """Update a planning result by ID (caller-supplied session)."""
    try:
        result = db.query(PlanningResult).filter(PlanningResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating planning result: %s", e)
        return None


# ---------------------------------------------------------------------------
# ComponentResult
# ---------------------------------------------------------------------------

def get_latest_dev_run_number(task_id: str) -> int:
    """Return the highest dev_run_number recorded for a task (0 if none)."""
    from sqlalchemy import func
    db = SessionLocal()
    try:
        result = (db.query(func.max(ComponentResult.dev_run_number))
                  .filter(ComponentResult.task_id == task_id)
                  .scalar())
        return result if result is not None else 0
    finally:
        db.close()


def create_component_result(task_id, component_name, step_order, batch_number, **kwargs):
    db = SessionLocal()
    try:
        result = ComponentResult(
            task_id=task_id, component_name=component_name,
            step_order=step_order, batch_number=batch_number, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating component result: %s", e)
        return None
    finally:
        db.close()


def get_component_results(task_id, *, latest_run_only: bool = True):
    """Return component results for a task.

    By default returns only the most recent dev_run_number so the UI shows
    the current run rather than a pile of accumulated historical rows.
    Pass latest_run_only=False to retrieve all runs (e.g. diagnostics).
    """
    db = SessionLocal()
    try:
        q = db.query(ComponentResult).filter(ComponentResult.task_id == task_id)
        if latest_run_only:
            from sqlalchemy import func
            max_run = (db.query(func.max(ComponentResult.dev_run_number))
                       .filter(ComponentResult.task_id == task_id)
                       .scalar())
            if max_run is not None:
                q = q.filter(ComponentResult.dev_run_number == max_run)
        return q.order_by(ComponentResult.batch_number, ComponentResult.step_order).all()
    finally:
        db.close()


def update_component_result(result_id, **kwargs):
    db = SessionLocal()
    try:
        result = db.query(ComponentResult).filter(ComponentResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating component result: %s", e)
        return None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# OptimizationResult
# ---------------------------------------------------------------------------

def create_optimization_result(task_id, outcome, **kwargs):
    db = SessionLocal()
    try:
        result = OptimizationResult(task_id=task_id, outcome=outcome, **kwargs)
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating optimization result: %s", e)
        return None
    finally:
        db.close()


def get_optimization_result(task_id):
    db = SessionLocal()
    try:
        return (db.query(OptimizationResult)
                .filter(OptimizationResult.task_id == task_id)
                .order_by(OptimizationResult.created_at.desc())
                .first())
    finally:
        db.close()


def update_optimization_result(db, result_id, **kwargs):
    """Update an optimization result by ID (caller-supplied session)."""
    try:
        result = db.query(OptimizationResult).filter(OptimizationResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating optimization result: %s", e)
        return None


# ---------------------------------------------------------------------------
# SecurityReviewResult
# ---------------------------------------------------------------------------

def create_security_review_result(task_id, reviewer_type, verdict, confidence, **kwargs):
    db = SessionLocal()
    try:
        result = SecurityReviewResult(
            task_id=task_id, reviewer_type=reviewer_type,
            verdict=verdict, confidence=confidence, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating security review result: %s", e)
        return None
    finally:
        db.close()


def get_security_review_results(task_id):
    db = SessionLocal()
    try:
        return (db.query(SecurityReviewResult)
                .filter(SecurityReviewResult.task_id == task_id)
                .order_by(SecurityReviewResult.created_at.desc())
                .all())
    finally:
        db.close()


def update_security_review_result(db, result_id, **kwargs):
    """Update a security review result by ID (caller-supplied session)."""
    try:
        result = db.query(SecurityReviewResult).filter(SecurityReviewResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating security review result: %s", e)
        return None


# ---------------------------------------------------------------------------
# FullReviewResult
# ---------------------------------------------------------------------------

def create_full_review_result(task_id, reviewer_type, verdict, confidence, **kwargs):
    db = SessionLocal()
    try:
        result = FullReviewResult(
            task_id=task_id, reviewer_type=reviewer_type,
            verdict=verdict, confidence=confidence, **kwargs
        )
        db.add(result)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error creating full review result: %s", e)
        return None
    finally:
        db.close()


def get_full_review_results(task_id):
    db = SessionLocal()
    try:
        return (db.query(FullReviewResult)
                .filter(FullReviewResult.task_id == task_id)
                .order_by(FullReviewResult.created_at.desc())
                .all())
    finally:
        db.close()


def update_full_review_result(db, result_id, **kwargs):
    """Update a full review result by ID (caller-supplied session)."""
    try:
        result = db.query(FullReviewResult).filter(FullReviewResult.id == result_id).first()
        if not result:
            return None
        for key, value in kwargs.items():
            if hasattr(result, key):
                setattr(result, key, value)
        db.commit()
        db.refresh(result)
        return result
    except Exception as e:
        db.rollback()
        logger.error("Error updating full review result: %s", e)
        return None


# ---------------------------------------------------------------------------
# MergeRecord
# ---------------------------------------------------------------------------

def create_merge_record(task_id, branch_name, status, **kwargs):
    db = SessionLocal()
    try:
        record = MergeRecord(
            task_id=task_id, branch_name=branch_name,
            status=status, **kwargs
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error creating merge record: %s", e)
        return None
    finally:
        db.close()


def get_merge_record(task_id):
    db = SessionLocal()
    try:
        return (db.query(MergeRecord)
                .filter(MergeRecord.task_id == task_id)
                .order_by(MergeRecord.created_at.desc())
                .first())
    finally:
        db.close()


def update_merge_record(db, record_id, **kwargs):
    """Update a merge record by ID (caller-supplied session)."""
    try:
        record = db.query(MergeRecord).filter(MergeRecord.id == record_id).first()
        if not record:
            return None
        for key, value in kwargs.items():
            if hasattr(record, key):
                setattr(record, key, value)
        db.commit()
        db.refresh(record)
        return record
    except Exception as e:
        db.rollback()
        logger.error("Error updating merge record: %s", e)
        return None
