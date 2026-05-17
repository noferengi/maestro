"""
CRUD operations for BudgetEntry and Expense, plus budget math helpers.

BudgetEntry — one row per LLM call (delta prompt messages + full response JSON stored).
Expense     — one row per LLM call with microcent (µ¢) cost breakdown.
              1 µ¢ = 1/1,000,000 of a US cent.  dollar_amount == -1 → infinite.

Key helpers:
  get_budget_spent_microcents      — SUM(expenses) for a budget
  get_budget_remaining_microcents  — remaining capacity (None if infinite)
  budget_has_capacity              — pre-flight check before dispatching a job
  get_budget_summary               — aggregate totals from BudgetEntry rows
  reconstruct_messages_for_entry   — accumulate session deltas → full message list
"""

import json
import logging

from sqlalchemy import func

from datetime import datetime, timezone, timedelta

from .session import SessionLocal
from .models import BudgetEntry, Expense, Task, AgentSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# BudgetEntry CRUD
# ---------------------------------------------------------------------------

def create_budget_entry(llm_id=None, budget_id=None, task_id=None,
                        prompt_cost=0, generation_cost=0, tool_calls=0,
                        prompt_data=None, response_data=None,
                        session_id=None, agent_name=None,
                        prompt_message_count=None):
    db = SessionLocal()
    try:
        entry = BudgetEntry(
            llm_id=llm_id, budget_id=budget_id, task_id=task_id,
            prompt_cost=prompt_cost, generation_cost=generation_cost,
            tool_calls=tool_calls, prompt_data=prompt_data, response_data=response_data,
            session_id=session_id, agent_name=agent_name,
            prompt_message_count=prompt_message_count,
        )
        db.add(entry)
        
        now_iso = datetime.now(timezone.utc).isoformat()

        if task_id:
            db.query(Task).filter(Task.id == task_id).update(
                {"last_progress_at": datetime.utcnow()}, synchronize_session=False
            )
            # Robust fallback: if task_id is present, update the currently open session for that task.
            # This covers cases where session_id is a UUID or missing but task_id is known.
            db.query(AgentSession).filter(
                AgentSession.task_id == task_id,
                AgentSession.ended_at.is_(None)
            ).update({"last_activity_at": now_iso}, synchronize_session=False)
        
        if session_id and session_id.isdigit():
            db.query(AgentSession).filter(AgentSession.id == int(session_id)).update(
                {"last_activity_at": now_iso}, synchronize_session=False
            )
        
        db.commit()
        db.refresh(entry)
        return entry
    except Exception as e:
        db.rollback()
        logger.error("Error creating budget entry: %s", e)
        return None
    finally:
        db.close()


def get_budget_entries(budget_id=None, llm_id=None, task_id=None, limit=100, offset=0):
    db = SessionLocal()
    try:
        q = db.query(BudgetEntry)
        if budget_id is not None:
            q = q.filter(BudgetEntry.budget_id == budget_id)
        if llm_id is not None:
            q = q.filter(BudgetEntry.llm_id == llm_id)
        if task_id is not None:
            q = q.filter(BudgetEntry.task_id == task_id)
        return q.order_by(BudgetEntry.created_at.desc()).offset(offset).limit(limit).all()
    finally:
        db.close()


def get_budget_entry(entry_id):
    """Get a single budget entry by ID."""
    db = SessionLocal()
    try:
        return db.query(BudgetEntry).filter(BudgetEntry.id == entry_id).first()
    finally:
        db.close()


def delete_budget_entry(entry_id: int) -> bool:
    """
    Delete a budget entry by ID.

    This will cascade delete the associated Expense record(s) due to
    the ON DELETE CASCADE foreign key constraint on budget_entry_id.

    Args:
        entry_id: The ID of the BudgetEntry to delete.

    Returns:
        True if deletion was successful, False otherwise.
    """
    db = SessionLocal()
    try:
        entry = db.query(BudgetEntry).filter(BudgetEntry.id == entry_id).first()
        if entry is None:
            logger.warning("BudgetEntry not found: %s", entry_id)
            return False

        db.delete(entry)
        db.commit()
        logger.info("Deleted BudgetEntry %s (and cascaded Expense)", entry_id)
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting budget entry %s: %s", entry_id, e)
        return False
    finally:
        db.close()


def reconstruct_messages_for_entry(entry_id: int, db=None) -> list[dict]:
    """Rebuild the full message list for a budget entry by accumulating deltas.

    For legacy rows (prompt_message_count IS NULL), prompt_data is the full
    history and is returned as-is.  For delta rows, all prior deltas in the
    session are concatenated in order.
    """
    _own_db = db is None
    if _own_db:
        db = SessionLocal()
    try:
        entry = db.query(BudgetEntry).filter(BudgetEntry.id == entry_id).first()
        if not entry:
            return []
        if entry.prompt_message_count is None:
            return json.loads(entry.prompt_data) if entry.prompt_data else []
        if not entry.session_id:
            return json.loads(entry.prompt_data) if entry.prompt_data else []
        prior = (
            db.query(BudgetEntry)
            .filter(
                BudgetEntry.session_id == entry.session_id,
                BudgetEntry.id <= entry_id,
                BudgetEntry.prompt_data.isnot(None),
            )
            .order_by(BudgetEntry.id.asc())
            .all()
        )
        full: list[dict] = []
        for e in prior:
            full.extend(json.loads(e.prompt_data))
        return full
    finally:
        if _own_db:
            db.close()


# ---------------------------------------------------------------------------
# Expense CRUD
# ---------------------------------------------------------------------------

def create_expense(budget_entry_id, budget_id, llm_id, task_id,
                   prompt_tokens, completion_tokens,
                   prompt_cost_microcents, completion_cost_microcents,
                   remote_call_id=None):
    db = SessionLocal()
    try:
        e = Expense(
            budget_entry_id=budget_entry_id, budget_id=budget_id,
            llm_id=llm_id, task_id=task_id,
            remote_call_id=remote_call_id,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            prompt_cost_microcents=prompt_cost_microcents,
            completion_cost_microcents=completion_cost_microcents,
            total_cost_microcents=prompt_cost_microcents + completion_cost_microcents,
        )
        db.add(e)
        db.commit()
        db.refresh(e)
        return e
    except Exception as ex:
        db.rollback()
        logger.error("Error creating expense: %s", ex)
        return None
    finally:
        db.close()


def get_expense(expense_id: int):
    """
    Get a single expense record by ID.

    Args:
        expense_id: The ID of the Expense to retrieve.

    Returns:
        The Expense record if found, None otherwise.
    """
    db = SessionLocal()
    try:
        return db.query(Expense).filter(Expense.id == expense_id).first()
    finally:
        db.close()


def delete_expense(expense_id: int) -> bool:
    """
    Delete an expense record by ID.

    Note: This operation is independent of BudgetEntry deletion.
    Deleting an Expense does not affect its associated BudgetEntry.

    Args:
        expense_id: The ID of the Expense to delete.

    Returns:
        True if deletion was successful, False otherwise.
    """
    db = SessionLocal()
    try:
        expense = db.query(Expense).filter(Expense.id == expense_id).first()
        if expense is None:
            logger.warning("Expense not found: %s", expense_id)
            return False

        db.delete(expense)
        db.commit()
        logger.info("Deleted Expense %s", expense_id)
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting expense %s: %s", expense_id, e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Budget math helpers
# ---------------------------------------------------------------------------

def get_budget_spent_microcents(budget_id: int) -> int:
    db = SessionLocal()
    try:
        result = db.query(func.coalesce(func.sum(Expense.total_cost_microcents), 0)) \
                   .filter(Expense.budget_id == budget_id).scalar()
        return int(result)
    finally:
        db.close()


def get_budget_remaining_microcents(budget_id: int):
    """Returns remaining µ¢, or None if infinite (dollar_amount == -1)."""
    from .crud_infra import get_budget
    budget = get_budget(budget_id)
    if budget is None or budget.dollar_amount == -1:
        return None
    limit_microcents = int(budget.dollar_amount * 100 * 1_000_000)
    spent = get_budget_spent_microcents(budget_id)
    return max(0, limit_microcents - spent)


def budget_has_capacity(budget_id: int, worst_case_microcents: int) -> bool:
    remaining = get_budget_remaining_microcents(budget_id)
    if remaining is None:
        return True
    return remaining >= worst_case_microcents


def get_budget_summary(budget_id=None):
    """Aggregate totals for a budget (or all budgets if None)."""
    db = SessionLocal()
    try:
        q = db.query(
            func.count(BudgetEntry.id).label('total_entries'),
            func.coalesce(func.sum(BudgetEntry.prompt_cost), 0).label('total_prompt_tokens'),
            func.coalesce(func.sum(BudgetEntry.generation_cost), 0).label('total_generation_tokens'),
            func.coalesce(func.sum(BudgetEntry.tool_calls), 0).label('total_tool_calls'),
        )
        if budget_id is not None:
            q = q.filter(BudgetEntry.budget_id == budget_id)
        row = q.one()
        return {
            'total_entries': row.total_entries,
            'total_prompt_tokens': row.total_prompt_tokens,
            'total_generation_tokens': row.total_generation_tokens,
            'total_tool_calls': row.total_tool_calls,
        }
    finally:
        db.close()
