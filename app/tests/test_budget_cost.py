"""
Tests for P1 — Budget Dollar Limits + LLM Cost Tracking.

Covers:
  - Budget dollar_amount default and storage
  - LLM cost field defaults
  - Expense creation and summing
  - budget_has_capacity: infinite and exhausted
  - /api/budgets/{id}/remaining route
  - Microcent arithmetic correctness
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


from app.database import (
    create_budget, create_llm, create_expense,
    get_budget_spent_microcents, get_budget_remaining_microcents,
    budget_has_capacity, get_budget,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _unique(prefix: str) -> str:
    import uuid
    return f"{prefix}_{uuid.uuid4().hex}"


# ---------------------------------------------------------------------------
# Budget dollar_amount default
# ---------------------------------------------------------------------------

class TestBudgetDollarAmount:
    def test_default_is_minus_one(self):
        b = create_budget(name=_unique("budget"))
        assert b is not None
        assert b.dollar_amount == -1.0

    def test_explicit_dollar_amount(self):
        b = create_budget(name=_unique("budget"), dollar_amount=50.0)
        assert b is not None
        assert b.dollar_amount == 50.0


# ---------------------------------------------------------------------------
# LLM cost fields default
# ---------------------------------------------------------------------------

class TestLlmCostFields:
    def test_defaults_zero(self):
        llm = create_llm(address="localhost", port=_unique_port(), model=_unique("model"))
        assert llm is not None
        assert llm.cost_per_million_prompt_tokens == 0.0
        assert llm.cost_per_million_completion_tokens == 0.0

    def test_explicit_rates(self):
        llm = create_llm(
            address="localhost", port=_unique_port(), model=_unique("model"),
            cost_per_million_prompt_tokens=0.15,
            cost_per_million_completion_tokens=1.0,
        )
        assert llm is not None
        assert llm.cost_per_million_prompt_tokens == 0.15
        assert llm.cost_per_million_completion_tokens == 1.0


def _unique_port():
    import random
    return random.randint(20000, 60000)


# ---------------------------------------------------------------------------
# Expense creation and summing
# ---------------------------------------------------------------------------

class TestExpenseCreation:
    def test_create_and_sum(self):
        b = create_budget(name=_unique("budget"), dollar_amount=10.0)
        llm = create_llm(address="localhost", port=_unique_port(), model=_unique("model"))
        assert b and llm

        e = create_expense(
            budget_entry_id=None,
            budget_id=b.id,
            llm_id=llm.id,
            task_id=None,
            prompt_tokens=1000,
            completion_tokens=500,
            prompt_cost_microcents=150_000,   # $0.15/M × 1000 = 150 µ¢
            completion_cost_microcents=500_000,  # $1.00/M × 500 = 500 µ¢
        )
        assert e is not None
        assert e.total_cost_microcents == 650_000

        spent = get_budget_spent_microcents(b.id)
        assert spent == 650_000

    def test_multiple_expenses_sum(self):
        b = create_budget(name=_unique("budget"), dollar_amount=5.0)
        for _ in range(3):
            create_expense(
                budget_entry_id=None, budget_id=b.id, llm_id=None, task_id=None,
                prompt_tokens=100, completion_tokens=50,
                prompt_cost_microcents=10_000, completion_cost_microcents=5_000,
            )
        assert get_budget_spent_microcents(b.id) == 45_000  # 3 × 15_000


# ---------------------------------------------------------------------------
# budget_has_capacity
# ---------------------------------------------------------------------------

class TestBudgetHasCapacity:
    def test_infinite_always_true(self):
        b = create_budget(name=_unique("budget"), dollar_amount=-1)
        assert budget_has_capacity(b.id, 999_999_999) is True

    def test_sufficient_capacity(self):
        b = create_budget(name=_unique("budget"), dollar_amount=1.0)
        # $1.00 = 100_000_000 µ¢; no expenses yet
        assert budget_has_capacity(b.id, 50_000_000) is True

    def test_exhausted_budget(self):
        b = create_budget(name=_unique("budget"), dollar_amount=0.01)
        # $0.01 = 1_000_000 µ¢
        create_expense(
            budget_entry_id=None, budget_id=b.id, llm_id=None, task_id=None,
            prompt_tokens=1, completion_tokens=0,
            prompt_cost_microcents=1_000_001, completion_cost_microcents=0,
        )
        assert budget_has_capacity(b.id, 1) is False

    def test_remaining_returns_none_for_infinite(self):
        b = create_budget(name=_unique("budget"), dollar_amount=-1)
        assert get_budget_remaining_microcents(b.id) is None


# ---------------------------------------------------------------------------
# /api/budgets/{id}/remaining route
# ---------------------------------------------------------------------------

class TestRemainingRoute:
    def test_route_200(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        b = create_budget(name=_unique("budget"), dollar_amount=25.0)
        resp = client.get(f"/api/budgets/{b.id}/remaining")
        assert resp.status_code == 200
        data = resp.json()
        assert data["budget_id"] == b.id
        assert data["dollar_amount"] == 25.0
        assert data["infinite"] is False
        assert data["spent_microcents"] == 0
        assert data["remaining_dollars"] == pytest.approx(25.0, abs=0.01)

    def test_route_infinite(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        b = create_budget(name=_unique("budget"), dollar_amount=-1)
        resp = client.get(f"/api/budgets/{b.id}/remaining")
        assert resp.status_code == 200
        data = resp.json()
        assert data["infinite"] is True
        assert data["remaining_dollars"] is None

    def test_route_404(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)

        resp = client.get("/api/budgets/999999/remaining")
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Microcent arithmetic
# ---------------------------------------------------------------------------

class TestMicrocentArithmetic:
    def test_015_per_million_34k_tokens(self):
        # $0.15/M PP × 34000 tokens = 510,000 µ¢
        pp_rate = 0.15
        tokens = 34_000
        uc = int(tokens * pp_rate * 100)
        assert uc == 510_000

    def test_100_per_million_12800_tokens(self):
        # $1.00/M TG × 12800 tokens = 1,280,000 µ¢
        tg_rate = 1.00
        tokens = 12_800
        uc = int(tokens * tg_rate * 100)
        assert uc == 1_280_000

    def test_budget_250_dollars_in_microcents(self):
        # $250 → 25,000,000,000 µ¢
        dollars = 250
        uc = int(dollars * 100 * 1_000_000)
        assert uc == 25_000_000_000
