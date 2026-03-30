"""
CRUD operations for LLM and Budget configuration tables.

These are user-managed resource records — the UI lets you add/edit/delete
LLM endpoints and spending budgets.  Both tables are referenced by tasks,
expenses, and job tables via foreign keys.
"""

import logging

from .session import SessionLocal
from .models import LLM, Budget, ComputeNode

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LLM CRUD
# ---------------------------------------------------------------------------

def get_all_llms():
    db = SessionLocal()
    try:
        return db.query(LLM).order_by(LLM.id).all()
    finally:
        db.close()


def get_llm(llm_id):
    db = SessionLocal()
    try:
        return db.query(LLM).filter(LLM.id == llm_id).first()
    finally:
        db.close()


def create_llm(address, port, model, settings=None, parallel_sessions=1, max_context=4096, notes='',
               cost_per_million_prompt_tokens=0.0, cost_per_million_completion_tokens=0.0):
    db = SessionLocal()
    try:
        llm = LLM(address=address, port=port, model=model, settings=settings,
                   parallel_sessions=parallel_sessions, max_context=max_context, notes=notes,
                   cost_per_million_prompt_tokens=cost_per_million_prompt_tokens,
                   cost_per_million_completion_tokens=cost_per_million_completion_tokens)
        db.add(llm)
        db.commit()
        db.refresh(llm)
        return llm
    except Exception as e:
        db.rollback()
        logger.error("Error creating LLM: %s", e)
        return None
    finally:
        db.close()


def update_llm(llm_id, **kwargs):
    db = SessionLocal()
    try:
        llm = db.query(LLM).filter(LLM.id == llm_id).first()
        if not llm:
            return None
        for key, value in kwargs.items():
            if hasattr(llm, key):
                setattr(llm, key, value)
        db.commit()
        db.refresh(llm)
        return llm
    except Exception as e:
        db.rollback()
        logger.error("Error updating LLM: %s", e)
        return None
    finally:
        db.close()


def delete_llm(llm_id):
    db = SessionLocal()
    try:
        llm = db.query(LLM).filter(LLM.id == llm_id).first()
        if not llm:
            return False
        db.delete(llm)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting LLM: %s", e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Budget CRUD
# ---------------------------------------------------------------------------

def get_all_budgets():
    db = SessionLocal()
    try:
        return db.query(Budget).order_by(Budget.id).all()
    finally:
        db.close()


def get_budget(budget_id):
    db = SessionLocal()
    try:
        return db.query(Budget).filter(Budget.id == budget_id).first()
    finally:
        db.close()


def create_budget(name, dollar_amount=-1.0, settings=None):
    db = SessionLocal()
    try:
        budget = Budget(name=name, dollar_amount=dollar_amount, settings=settings)
        db.add(budget)
        db.commit()
        db.refresh(budget)
        return budget
    except Exception as e:
        db.rollback()
        logger.error("Error creating budget: %s", e)
        return None
    finally:
        db.close()


def update_budget(budget_id, **kwargs):
    db = SessionLocal()
    try:
        budget = db.query(Budget).filter(Budget.id == budget_id).first()
        if not budget:
            return None
        for key, value in kwargs.items():
            if hasattr(budget, key):
                setattr(budget, key, value)
        db.commit()
        db.refresh(budget)
        return budget
    except Exception as e:
        db.rollback()
        logger.error("Error updating budget: %s", e)
        return None
    finally:
        db.close()


def delete_budget(budget_id):
    db = SessionLocal()
    try:
        budget = db.query(Budget).filter(Budget.id == budget_id).first()
        if not budget:
            return False
        db.delete(budget)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting budget: %s", e)
        return False
    finally:
        db.close()


# ---------------------------------------------------------------------------
# ComputeNode CRUD
# ---------------------------------------------------------------------------

def get_all_compute_nodes():
    db = SessionLocal()
    try:
        return db.query(ComputeNode).order_by(ComputeNode.id).all()
    finally:
        db.close()


def get_compute_node(node_id):
    db = SessionLocal()
    try:
        return db.query(ComputeNode).filter(ComputeNode.id == node_id).first()
    finally:
        db.close()


def create_compute_node(name, description=None, max_parallel_sessions=1):
    db = SessionLocal()
    try:
        node = ComputeNode(name=name, description=description,
                           max_parallel_sessions=max_parallel_sessions)
        db.add(node)
        db.commit()
        db.refresh(node)
        return node
    except Exception as e:
        db.rollback()
        logger.error("Error creating compute node: %s", e)
        return None
    finally:
        db.close()


def update_compute_node(node_id, **kwargs):
    db = SessionLocal()
    try:
        node = db.query(ComputeNode).filter(ComputeNode.id == node_id).first()
        if not node:
            return None
        for key, value in kwargs.items():
            if hasattr(node, key):
                setattr(node, key, value)
        db.commit()
        db.refresh(node)
        return node
    except Exception as e:
        db.rollback()
        logger.error("Error updating compute node: %s", e)
        return None
    finally:
        db.close()


def delete_compute_node(node_id):
    db = SessionLocal()
    try:
        node = db.query(ComputeNode).filter(ComputeNode.id == node_id).first()
        if not node:
            return False
        db.delete(node)
        db.commit()
        return True
    except Exception as e:
        db.rollback()
        logger.error("Error deleting compute node: %s", e)
        return False
    finally:
        db.close()
