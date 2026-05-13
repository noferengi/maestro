"""
Tests for optimization Phase 4 sub-task spawning and record_benchmark tool.
"""

from __future__ import annotations

import json
import pytest


@pytest.fixture(autouse=True)
def _restore_database_module():
    """Reload app.database after each test to undo any importlib.reload() side-effects.

    Tests in this file redirect app.database to a tmp_path DB via
    monkeypatch.setenv + importlib.reload.  When monkeypatch teardown runs
    (before this fixture teardown, because LIFO), it restores MAESTRO_TEST_DB
    to the session test.db path.  We then reload the module so that
    app.database.SessionLocal points at test.db again — preventing DB-path
    pollution for every test file that runs afterwards.
    """
    yield
    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)


# ---------------------------------------------------------------------------
# OptimizationBenchmark CRUD
# ---------------------------------------------------------------------------

def test_create_and_get_optimization_benchmark(tmp_path, monkeypatch):
    """create_optimization_benchmark + get_optimization_benchmarks round-trip."""
    db_path = str(tmp_path / "bench.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    from migrations.runner import migrate as run_migrate, ConnectionWrapper
    with db_mod.engine.begin() as _conn:
        run_migrate(ConnectionWrapper(_conn, is_postgres=False))

    session = db_mod.SessionLocal()
    parent = db_mod.Task(id="task-parent", title="Parent", type="optimization", project="P", history=[])
    child = db_mod.Task(id="task-child", title="Child", type="idea", project="P", history=[])
    session.add_all([parent, child])
    session.commit()
    session.close()

    metrics = {"test_duration_ms": 1200, "memory_peak_mb": 80, "complexity_score": 42}
    bench = db_mod.create_optimization_benchmark(
        task_id="task-child",
        parent_task_id="task-parent",
        benchmark_type="before",
        metrics=json.dumps(metrics),
    )
    assert bench is not None
    assert bench.benchmark_type == "before"

    results = db_mod.get_optimization_benchmarks("task-parent")
    assert len(results) == 1
    stored_metrics = json.loads(results[0].metrics)
    assert stored_metrics["test_duration_ms"] == 1200


# ---------------------------------------------------------------------------
# record_benchmark tool
# ---------------------------------------------------------------------------

def test_record_benchmark_tool_success(tmp_path, monkeypatch):
    """record_benchmark tool returns OK on valid input."""
    db_path = str(tmp_path / "bench2.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    from migrations.runner import migrate as run_migrate, ConnectionWrapper
    with db_mod.engine.begin() as _conn:
        run_migrate(ConnectionWrapper(_conn, is_postgres=False))

    session = db_mod.SessionLocal()
    parent = db_mod.Task(id="t-p", title="P", type="optimization", project="P", history=[])
    child = db_mod.Task(id="t-c", title="C", type="idea", project="P", history=[])
    session.add_all([parent, child])
    session.commit()
    session.close()

    import app.agent.tools as tools_mod
    result = tools_mod.write_benchmark(
        task_id="t-c",
        parent_task_id="t-p",
        benchmark_type="after",
        metrics=json.dumps({"test_duration_ms": 900, "complexity_score": 30}),
    )
    assert result.startswith("OK:")


def test_record_benchmark_tool_invalid_type(tmp_path, monkeypatch):
    """record_benchmark returns ERROR for invalid benchmark_type."""
    import app.agent.tools as tools_mod
    result = tools_mod.write_benchmark(
        task_id="t-c",
        parent_task_id="t-p",
        benchmark_type="during",
        metrics="{}",
    )
    assert result.startswith("ERROR:")


def test_record_benchmark_tool_bad_json(tmp_path, monkeypatch):
    """record_benchmark returns ERROR for non-JSON metrics."""
    import app.agent.tools as tools_mod
    result = tools_mod.write_benchmark(
        task_id="t-c",
        parent_task_id="t-p",
        benchmark_type="before",
        metrics="not json",
    )
    assert result.startswith("ERROR:")


# ---------------------------------------------------------------------------
# OptimizationPipeline._phase_implementation sub-task creation
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_phase_implementation_creates_subtasks(tmp_path, monkeypatch):
    """_phase_implementation creates Kanban cards and marks parent as big_idea."""
    db_path = str(tmp_path / "opt.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    from migrations.runner import migrate as run_migrate, ConnectionWrapper
    with db_mod.engine.begin() as _conn:
        run_migrate(ConnectionWrapper(_conn, is_postgres=False))

    session = db_mod.SessionLocal()
    task = db_mod.Task(
        id="task-opt-parent",
        title="Optimize sorting",
        type="optimization",
        project="TestProj",
        history=[],
        prerequisites=[],
        llm_id=None,
        budget_id=None,
        subdivision_generation=0,
    )
    session.add(task)
    session.commit()
    session.close()

    from app.agent.optimization import OptimizationPipeline

    pipeline = OptimizationPipeline.__new__(OptimizationPipeline)
    pipeline.task_id = "task-opt-parent"
    pipeline.llm_base_url = "http://localhost:8008/v1"
    pipeline.llm_model = "test"
    pipeline.llm_id = None
    pipeline.budget_id = None
    pipeline._total_prompt = 0
    pipeline._total_completion = 0

    proposal = {
        "lens": "performance",
        "proposals": [
            {
                "description": "Use timsort instead of quicksort",
                "rationale": "Timsort is faster for partially-sorted data",
                "implementation_steps": ["Replace sort call", "Run benchmarks"],
                "risk": "low",
            }
        ],
    }

    # Mock _wait_for_subtasks to return True immediately
    async def mock_wait(sub_task_ids, **kwargs):
        return True

    import unittest.mock as mock
    with mock.patch.object(pipeline, "_wait_for_subtasks", side_effect=mock_wait):
        result = await pipeline._phase_implementation(proposal)

    assert result is True

    # Verify a sub-task was created
    all_tasks = db_mod.get_all_tasks()
    subtasks = [t for t in all_tasks if t.parent_task_id == "task-opt-parent"]
    assert len(subtasks) == 1
    assert subtasks[0].type == "idea"
    assert "Opt:" in subtasks[0].title

    # Parent should be flagged as big idea
    parent = db_mod.get_task("task-opt-parent")
    assert parent.is_big_idea is True


def test_phase_implementation_prereq_deadlock_avoidance(tmp_path, monkeypatch):
    """Sub-tasks must inherit parent's prerequisites, NOT the parent's own ID."""
    db_path = str(tmp_path / "dl.db")
    monkeypatch.setenv("MAESTRO_TEST_DB", db_path)

    import importlib
    import app.database as db_mod
    importlib.reload(db_mod)
    from migrations.runner import migrate as run_migrate, ConnectionWrapper
    with db_mod.engine.begin() as _conn:
        run_migrate(ConnectionWrapper(_conn, is_postgres=False))

    session = db_mod.SessionLocal()
    # Create a prereq task
    prereq = db_mod.Task(id="task-prereq", title="Prereq", type="completed", project="P", history=[])
    parent = db_mod.Task(
        id="task-dl-parent",
        title="Parent",
        type="optimization",
        project="P",
        history=[],
        prerequisites=["task-prereq"],
        subdivision_generation=0,
    )
    session.add_all([prereq, parent])
    session.commit()
    session.close()

    from app.agent.optimization import OptimizationPipeline
    import asyncio

    pipeline = OptimizationPipeline.__new__(OptimizationPipeline)
    pipeline.task_id = "task-dl-parent"
    pipeline.llm_base_url = ""
    pipeline.llm_model = ""
    pipeline.llm_id = None
    pipeline.budget_id = None
    pipeline._total_prompt = 0
    pipeline._total_completion = 0

    proposal = {
        "proposals": [{"description": "opt A", "risk": "low"}],
        "lens": "size",
    }

    import unittest.mock as mock

    async def mock_wait(ids, **kwargs):
        return True

    async def run():
        with mock.patch.object(pipeline, "_wait_for_subtasks", side_effect=mock_wait):
            await pipeline._phase_implementation(proposal)

    asyncio.run(run())

    all_tasks = db_mod.get_all_tasks()
    subtasks = [t for t in all_tasks if t.parent_task_id == "task-dl-parent"]
    assert len(subtasks) == 1
    # Sub-task prereqs should be ["task-prereq"], NOT ["task-dl-parent"]
    assert subtasks[0].prerequisites == ["task-prereq"]
    assert "task-dl-parent" not in (subtasks[0].prerequisites or [])


# ---------------------------------------------------------------------------
# Scheduler priority scoring
# ---------------------------------------------------------------------------

def test_compute_dag_depth_no_prereqs():
    from app.agent.scheduler import _compute_dag_depth
    by_id = {"a": {"id": "a", "prerequisites": []}}
    assert _compute_dag_depth("a", by_id) == 0


def test_compute_dag_depth_chain():
    from app.agent.scheduler import _compute_dag_depth
    by_id = {
        "a": {"id": "a", "prerequisites": []},
        "b": {"id": "b", "prerequisites": ["a"]},
        "c": {"id": "c", "prerequisites": ["b"]},
    }
    assert _compute_dag_depth("c", by_id) == 2
    assert _compute_dag_depth("b", by_id) == 1
    assert _compute_dag_depth("a", by_id) == 0


def test_compute_priority_tiers():
    """Priority tiers: idea < starred < regular, stalest within tier wins."""
    import datetime as _dt
    from app.agent.scheduler import _compute_priority

    old_ts = (_dt.datetime.utcnow() - _dt.timedelta(days=10)).isoformat()
    new_ts = _dt.datetime.utcnow().isoformat()

    by_id = {
        "idea":    {"id": "idea",    "prerequisites": [], "type": "idea",     "position": 0,
                    "last_progress_at": new_ts, "is_starred": False},
        "starred": {"id": "starred", "prerequisites": [], "type": "planning", "position": 0,
                    "last_progress_at": new_ts, "is_starred": True},
        "regular": {"id": "regular", "prerequisites": [], "type": "planning", "position": 0,
                    "last_progress_at": new_ts, "is_starred": False},
        "stale":   {"id": "stale",   "prerequisites": [], "type": "planning", "position": 0,
                    "last_progress_at": old_ts, "is_starred": False},
    }
    p_idea    = _compute_priority(by_id["idea"],    by_id)
    p_starred = _compute_priority(by_id["starred"], by_id)
    p_regular = _compute_priority(by_id["regular"], by_id)
    p_stale   = _compute_priority(by_id["stale"],   by_id)

    assert p_idea < p_starred,  "idea must beat starred"
    assert p_starred < p_regular, "starred must beat regular"
    assert p_stale < p_regular,   "stalest regular task dispatched before fresher one"


# ---------------------------------------------------------------------------
# _compare_reports - benchmark data path
# ---------------------------------------------------------------------------

def _make_benchmark_record(task_id, parent_task_id, benchmark_type, metrics_dict):
    """Build a mock OptimizationBenchmark-like object."""
    import json
    from unittest.mock import MagicMock
    r = MagicMock()
    r.task_id = task_id
    r.parent_task_id = parent_task_id
    r.benchmark_type = benchmark_type
    r.metrics = json.dumps(metrics_dict)
    return r


def _make_pipeline():
    """Return an OptimizationPipeline instance without calling __init__."""
    from app.agent.optimization import OptimizationPipeline
    p = OptimizationPipeline.__new__(OptimizationPipeline)
    p.task_id = "parent-task"
    return p


def test_compare_reports_uses_benchmark_data_when_available(monkeypatch):
    """_compare_reports uses benchmark records when both before and after exist."""
    before = _make_benchmark_record("child-1", "parent-task", "before", {"test_duration_ms": 1000})
    after = _make_benchmark_record("child-1", "parent-task", "after", {"test_duration_ms": 800})

    monkeypatch.setattr(
        "app.database.get_optimization_benchmarks",
        lambda parent_task_id: [before, after],
    )

    pipeline = _make_pipeline()
    outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="parent-task")
    assert outcome == "optimized"
    assert "benchmark data" in summary
    assert "20.0%" in summary  # (1000-800)/1000 * 100 = 20%


def test_compare_reports_fallback_when_no_benchmark_records(monkeypatch):
    """_compare_reports falls back to profiling dict when no benchmark rows exist."""
    monkeypatch.setattr(
        "app.database.get_optimization_benchmarks",
        lambda parent_task_id: [],
    )

    pipeline = _make_pipeline()
    baseline = {"complexity_score": 100}
    post = {"complexity_score": 90}
    outcome, summary = pipeline._compare_reports(baseline, post, parent_task_id="parent-task")
    assert outcome == "optimized"
    assert "profiling data" in summary


def test_compare_reports_fallback_when_only_before_records(monkeypatch):
    """_compare_reports falls back when only 'before' records exist."""
    before = _make_benchmark_record("child-1", "parent-task", "before", {"test_duration_ms": 1000})

    monkeypatch.setattr(
        "app.database.get_optimization_benchmarks",
        lambda parent_task_id: [before],
    )

    pipeline = _make_pipeline()
    baseline = {"complexity_score": 100}
    post = {"complexity_score": 95}
    outcome, summary = pipeline._compare_reports(baseline, post, parent_task_id="parent-task")
    # Falls back to profiling
    assert "profiling data" in summary


def test_compare_reports_regression_detected_via_benchmarks(monkeypatch):
    """_compare_reports detects regressions using benchmark data."""
    before = _make_benchmark_record("child-1", "parent-task", "before", {"test_duration_ms": 500})
    after = _make_benchmark_record("child-1", "parent-task", "after", {"test_duration_ms": 600})

    monkeypatch.setattr(
        "app.database.get_optimization_benchmarks",
        lambda parent_task_id: [before, after],
    )

    pipeline = _make_pipeline()
    outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="parent-task")
    assert outcome == "rejected"
    assert "benchmark data" in summary


def test_compare_reports_no_parent_task_id_skips_benchmark(monkeypatch):
    """_compare_reports with no parent_task_id uses profiling only (no DB call)."""
    called = []
    monkeypatch.setattr(
        "app.database.get_optimization_benchmarks",
        lambda parent_task_id: called.append(parent_task_id) or [],
    )

    pipeline = _make_pipeline()
    pipeline._compare_reports({"complexity_score": 100}, {"complexity_score": 90})
    assert called == [], "Should not call get_optimization_benchmarks without parent_task_id"


# ---------------------------------------------------------------------------
# TestWeightedBenchmarkComparison
# ---------------------------------------------------------------------------

class TestWeightedBenchmarkComparison:
    """Tests for the weighted multi-metric _compare_benchmarks algorithm."""

    def test_compute_weighted_higher_than_memory(self, monkeypatch):
        """Large compute improvement + small memory regression -> still 'optimized'."""
        # compute: 1000->500ms = 50% improvement × weight 1.0
        # memory: 100->120mb = -20% (regression) × weight 0.6
        # weighted = (50*1.0 + -20*0.6) / (1.0+0.6) = (50-12)/1.6 = 38/1.6 = 23.75%
        before = _make_benchmark_record("c1", "p", "before", {
            "test_duration_ms": 1000, "memory_peak_mb": 100,
            "complexity_score": 50, "big_o_class": "O(n)",
            "scale_n": 10000, "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        after = _make_benchmark_record("c1", "p", "after", {
            "test_duration_ms": 500, "memory_peak_mb": 120,
            "complexity_score": 50, "big_o_class": "O(n)",
            "scale_n": 10000, "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "optimized"

    def test_big_o_bonus_applied(self, monkeypatch):
        """Modest time improvement + Big O rank improvement -> bonus pushes over threshold."""
        # compute: 1000->980ms = 2% improvement -> normally just at threshold
        # Big O: O(n^2) rank=5 -> O(n) rank=3 = 2 ranks × 10% = 20% bonus
        # weighted ≈ 2% + 20% = 22% -> optimized
        before = _make_benchmark_record("c1", "p", "before", {
            "test_duration_ms": 1000, "complexity_score": 50,
            "big_o_class": "O(n^2)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        after = _make_benchmark_record("c1", "p", "after", {
            "test_duration_ms": 980, "complexity_score": 45,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "optimized"
        assert "Big O" in summary

    def test_readability_penalty_applied(self, monkeypatch):
        """Good improvement + high readability_cost (0.9) -> penalty pulls below threshold -> skipped."""
        # compute: 1000->900ms = 10% improvement
        # readability_cost=0.9 -> penalty = 0.9 * 0.5 = 0.45 -> 10% * (1-0.45) = 5.5%
        # 5.5% > 2% threshold -> still optimized … let's use readability_cost=1.0
        # readability_cost=1.0 -> penalty = 1.0 * 0.5 = 0.5 -> 10% * 0.5 = 5% -> still > 2%
        # Use small improvement: 1000->975ms = 2.5% -> * 0.5 = 1.25% -> below 2% -> skipped
        before = _make_benchmark_record("c1", "p", "before", {
            "test_duration_ms": 1000, "complexity_score": 50,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        after = _make_benchmark_record("c1", "p", "after", {
            "test_duration_ms": 975, "complexity_score": 40,
            "big_o_class": "O(n)", "readability_cost": 1.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "skipped"

    def test_premature_optimization_requires_double_threshold(self, monkeypatch):
        """3% improvement + is_premature=True -> below 2×2%=4% threshold -> skipped."""
        before = _make_benchmark_record("c1", "p", "before", {
            "test_duration_ms": 1000, "complexity_score": 50,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        after = _make_benchmark_record("c1", "p", "after", {
            "test_duration_ms": 970, "complexity_score": 45,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": True, "tech_debt_resolved": False,
        })
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "skipped"
        assert "premature" in summary

    def test_tech_debt_bonus_applied(self, monkeypatch):
        """Borderline improvement + tech_debt_resolved=True -> bonus pushes over threshold -> optimized."""
        # compute: 1000->982ms = 1.8% -> below 2% threshold alone
        # tech_debt bonus = 1.0% -> 1.8% + 1.0% = 2.8% -> above 2% threshold
        before = _make_benchmark_record("c1", "p", "before", {
            "test_duration_ms": 1000, "complexity_score": 50,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": False,
        })
        after = _make_benchmark_record("c1", "p", "after", {
            "test_duration_ms": 982, "complexity_score": 45,
            "big_o_class": "O(n)", "readability_cost": 0.0,
            "is_premature": False, "tech_debt_resolved": True,
        })
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "optimized"
        assert "tech-debt" in summary

    def test_graceful_degradation_missing_fields(self, monkeypatch):
        """Old-format records (no new fields) fall back cleanly, no KeyError."""
        before = _make_benchmark_record("c1", "p", "before", {"test_duration_ms": 1000})
        after = _make_benchmark_record("c1", "p", "after", {"test_duration_ms": 800})
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [before, after])
        pipeline = _make_pipeline()
        # Should not raise
        outcome, summary = pipeline._compare_reports({}, {}, parent_task_id="p")
        assert outcome == "optimized"
        assert "20.0%" in summary


# ---------------------------------------------------------------------------
# TestBigOFallback
# ---------------------------------------------------------------------------

class TestBigOFallback:
    """Tests for Big O bonus applied in the profiling-dict fallback path."""

    def test_compare_reports_big_o_bonus_in_fallback(self, monkeypatch):
        """No benchmarks; profiling dicts have big_o_class -> bonus applied, outcome changes."""
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [])

        pipeline = _make_pipeline()
        # complexity_score alone: 100->99 = 1% -> below 2% threshold -> would be skipped
        # Big O: O(n^2) rank=5 -> O(n log n) rank=4 = 1 rank × 10% = 10% bonus
        # total = 1% + 10% = 11% -> optimized
        baseline = {"complexity_score": 100, "big_o_class": "O(n^2)"}
        post = {"complexity_score": 99, "big_o_class": "O(n log n)"}
        outcome, summary = pipeline._compare_reports(baseline, post, parent_task_id="p")
        assert outcome == "optimized"
        assert "Big O" in summary

    def test_compare_reports_no_big_o_no_bonus(self, monkeypatch):
        """No benchmarks, no big_o_class in dicts -> no bonus, below-threshold stays skipped."""
        monkeypatch.setattr("app.database.get_optimization_benchmarks", lambda pid: [])

        pipeline = _make_pipeline()
        baseline = {"complexity_score": 100}
        post = {"complexity_score": 99}  # 1% -> below 2% -> skipped
        outcome, summary = pipeline._compare_reports(baseline, post, parent_task_id="p")
        assert outcome == "skipped"
        assert "Big O" not in summary
