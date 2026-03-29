"""
app.database — public re-export surface.

Reload cascade
--------------
Tests that need an isolated DB do:
    monkeypatch.setenv("MAESTRO_TEST_DB", tmp_path / "x.db")
    importlib.reload(app.database)

With the old monolithic database.py a single reload was enough because
everything (DATABASE_PATH, engine, SessionLocal) lived in one module.
With this package, reloading only __init__.py leaves the submodules cached
in sys.modules with stale engines.

Solution: on every reload (detected via the ``_initialized`` sentinel set at
the bottom of this file) cascade-reload all submodules in dependency order
BEFORE re-running the ``from .X import`` statements.  First-time imports skip
the cascade — ``_initialized`` is not yet set.

Every name that was previously importable from the monolithic database.py is
re-exported here unchanged.  All existing import statements across the
codebase continue to work without modification:

    from app.database import Task, get_task, init_db, ...
    import app.database as db_mod          # monkeypatching in tests
    from database import Task, ...         # path-relative imports in app/

Internal submodule structure:
    session.py      — engine, Base, SessionLocal, get_db, init_db_tables
    models.py       — all 20 SQLAlchemy model classes
    crud_tasks.py   — Task CRUD + history + reorder + seed + subdivision helpers
    crud_projects.py— Project CRUD + get_project_path
    crud_infra.py   — LLM + Budget CRUD
    crud_costs.py   — BudgetEntry + Expense + budget math helpers
    crud_pipeline.py— all pipeline audit tables (votes, planning, component, etc.)
    crud_jobs.py    — ResearchJob + FileSummaryJob + OptimizationBenchmark
    crud_files.py   — FileSummary + SearchCache
"""

import sys as _sys

# On reload (not first import): cascade-reload submodules so DATABASE_PATH,
# engine, and SessionLocal are re-initialized before the from-imports below.
if globals().get('_initialized'):
    import importlib as _il
    for _sub in [
        'app.database.session', 'app.database.models',
        'app.database.crud_tasks', 'app.database.crud_projects',
        'app.database.crud_infra', 'app.database.crud_costs',
        'app.database.crud_pipeline', 'app.database.crud_jobs',
        'app.database.crud_files',
    ]:
        if _sub in _sys.modules:
            _il.reload(_sys.modules[_sub])


# Session / engine
from .session import (
    DATABASE_PATH,
    engine,
    SessionLocal,
    Base,
    get_db,
    init_db_tables,
)

# Models
from .models import (
    LLM,
    Budget,
    Project,
    Task,
    BudgetEntry,
    Expense,
    TransitionVote,
    TransitionResult,
    SubdivisionRecord,
    PlanningResult,
    ComponentResult,
    OptimizationResult,
    SecurityReviewResult,
    FullReviewResult,
    MergeRecord,
    ResearchJob,
    FileSummaryJob,
    OptimizationBenchmark,
    FileSummary,
    SearchCache,
)

# Task CRUD + seeding + helpers
from .crud_tasks import (
    init_db,
    seed_sample_tasks,
    seed_task,
    seed_sample_tasks_raw,
    create_task,
    get_task,
    get_tasks_by_type,
    get_tasks_by_project,
    get_all_tasks,
    update_task,
    batch_update_map_positions,
    delete_task,
    get_task_history,
    append_task_history,
    reorder_tasks,
    batch_reorder_tasks,
    set_big_idea_flag,
    get_child_tasks,
    get_active_child_tasks,
    count_total_sub_ideas,
    get_descendant_tree,
)

# Project CRUD
from .crud_projects import (
    get_all_projects,
    get_project,
    get_project_path,
    upsert_project,
    delete_project,
)

# LLM + Budget CRUD
from .crud_infra import (
    get_all_llms,
    get_llm,
    create_llm,
    update_llm,
    delete_llm,
    get_all_budgets,
    get_budget,
    create_budget,
    update_budget,
    delete_budget,
)

# BudgetEntry + Expense + budget math
from .crud_costs import (
    create_budget_entry,
    get_budget_entries,
    get_budget_entry,
    create_expense,
    get_budget_spent_microcents,
    get_budget_remaining_microcents,
    budget_has_capacity,
    get_budget_summary,
)

# Pipeline audit tables
from .crud_pipeline import (
    create_transition_vote,
    get_transition_votes,
    create_transition_result,
    get_transition_results,
    create_subdivision_record,
    get_subdivision_records,
    update_subdivision_record,
    create_planning_result,
    get_planning_result,
    update_planning_result,
    create_component_result,
    get_component_results,
    update_component_result,
    create_optimization_result,
    get_optimization_result,
    update_optimization_result,
    create_security_review_result,
    get_security_review_results,
    update_security_review_result,
    create_full_review_result,
    get_full_review_results,
    update_full_review_result,
    create_merge_record,
    get_merge_record,
    update_merge_record,
)

# Background job tables
from .crud_jobs import (
    create_research_job,
    get_research_job,
    get_pending_research_jobs,
    update_research_job,
    get_research_jobs_for_task,
    count_pending_research_jobs,
    create_file_summary_job,
    get_pending_file_summary_jobs,
    get_file_summary_job_by_sha1,
    update_file_summary_job,
    count_pending_file_summary_jobs,
    create_optimization_benchmark,
    get_optimization_benchmarks,
)

# File + search caches
from .crud_files import (
    get_file_summary,
    create_file_summary,
    get_file_summary_by_path,
    get_search_cache,
    create_search_cache,
)

__all__ = [
    # session
    "DATABASE_PATH", "engine", "SessionLocal", "Base", "get_db", "init_db_tables",
    # models
    "LLM", "Budget", "Project", "Task", "BudgetEntry", "Expense",
    "TransitionVote", "TransitionResult", "SubdivisionRecord",
    "PlanningResult", "ComponentResult", "OptimizationResult",
    "SecurityReviewResult", "FullReviewResult", "MergeRecord",
    "ResearchJob", "FileSummaryJob", "OptimizationBenchmark",
    "FileSummary", "SearchCache",
    # crud_tasks
    "init_db", "seed_sample_tasks", "seed_task", "seed_sample_tasks_raw",
    "create_task", "get_task", "get_tasks_by_type", "get_tasks_by_project",
    "get_all_tasks", "update_task", "batch_update_map_positions", "delete_task",
    "get_task_history", "append_task_history", "reorder_tasks", "batch_reorder_tasks",
    "set_big_idea_flag", "get_child_tasks", "get_active_child_tasks",
    "count_total_sub_ideas", "get_descendant_tree",
    # crud_projects
    "get_all_projects", "get_project", "get_project_path", "upsert_project", "delete_project",
    # crud_infra
    "get_all_llms", "get_llm", "create_llm", "update_llm", "delete_llm",
    "get_all_budgets", "get_budget", "create_budget", "update_budget", "delete_budget",
    # crud_costs
    "create_budget_entry", "get_budget_entries", "get_budget_entry",
    "create_expense", "get_budget_spent_microcents", "get_budget_remaining_microcents",
    "budget_has_capacity", "get_budget_summary",
    # crud_pipeline
    "create_transition_vote", "get_transition_votes",
    "create_transition_result", "get_transition_results",
    "create_subdivision_record", "get_subdivision_records", "update_subdivision_record",
    "create_planning_result", "get_planning_result", "update_planning_result",
    "create_component_result", "get_component_results", "update_component_result",
    "create_optimization_result", "get_optimization_result", "update_optimization_result",
    "create_security_review_result", "get_security_review_results", "update_security_review_result",
    "create_full_review_result", "get_full_review_results", "update_full_review_result",
    "create_merge_record", "get_merge_record", "update_merge_record",
    # crud_jobs
    "create_research_job", "get_research_job", "get_pending_research_jobs",
    "update_research_job", "get_research_jobs_for_task", "count_pending_research_jobs",
    "create_file_summary_job", "get_pending_file_summary_jobs", "get_file_summary_job_by_sha1",
    "update_file_summary_job", "count_pending_file_summary_jobs",
    "create_optimization_benchmark", "get_optimization_benchmarks",
    # crud_files
    "get_file_summary", "create_file_summary", "get_file_summary_by_path",
    "get_search_cache", "create_search_cache",
]

# Sentinel — presence of this flag on a subsequent execution means we're
# being reloaded (not imported for the first time).  The cascade logic at the
# top of this file reads it via globals().get('_initialized').
_initialized = True
