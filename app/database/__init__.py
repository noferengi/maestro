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
        'app.database.crud_files', 'app.database.crud_inbox',
        'app.database.crud_sessions', 'app.database.crud_dreamer',
        'app.database.crud_survey', 'app.database.crud_clarification',
        ]:  # NOTE: keep this list in sync with the from-imports below

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
    ComputeNode,
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
    FinalReviewResult,
    MergeRecord,
    PerformanceImprovementPlan,
    PipVerification,
    PipResolutionJob,
    ResearchJob,
    FileSummaryJob,
    OptimizationBenchmark,
    ArchGenJob,
    AgentSession,
    FileSummary,
    SearchCache,
    InboxMessage,
    DreamerRun,
    ScopeSummary,
    ScopeSurveyJob,
    IntakeDraft,
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
    get_deleted_tasks_by_project,
    get_all_tasks,
    update_task,
    batch_update_map_positions,
    delete_task,
    get_task_history,
    append_task_history,
    touch_progress,
    reorder_tasks,
    batch_reorder_tasks,
    set_big_idea_flag,
    get_child_tasks,
    get_active_child_tasks,
    count_total_sub_ideas,
    get_descendant_tree,
    task_to_dict,
    get_tasks_needing_clarification,
    create_pip,
    get_pips_for_task,
    satisfy_pips,
    create_pip_verification,
    get_latest_pip_verification,
    get_pip_verification_map,
    get_pip_verifications_for_pip,
    pip_status_at_stage,
    create_pip_resolution_job,
    get_pending_pip_resolution_jobs,
    get_active_pip_resolution_jobs_for_task,
    update_pip_resolution_job,
)

# Project CRUD
from .crud_projects import (
    get_all_projects,
    get_project,
    get_project_path,
    upsert_project,
    rename_project,
    delete_project,
)

# LLM + Budget + ComputeNode CRUD
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
    get_all_compute_nodes,
    get_compute_node,
    create_compute_node,
    update_compute_node,
    delete_compute_node,
    llm_to_dict,
    budget_to_dict,
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
    get_transition_votes_for_result,
    create_subdivision_record,
    get_subdivision_records,
    update_subdivision_record,
    create_planning_result,
    get_planning_result,
    get_latest_planning_result,
    supersede_planning_results,
    update_planning_result,
    get_reusable_planning_result,
    get_prior_failure_context,
    mark_gate_passed,
    restore_planning_result,
    create_component_result,
    get_component_results,
    get_latest_dev_run_number,
    update_component_result,
    create_optimization_result,
    get_optimization_result,
    update_optimization_result,
    create_security_review_result,
    get_security_review_results,
    update_security_review_result,
    create_final_review_result,
    get_final_review_results,
    update_final_review_result,
    create_merge_record,
    get_merge_record,
    update_merge_record,
)

# Background job tables
from .crud_jobs import (
    create_research_job,
    get_research_job,
    get_pending_research_jobs,
    get_retriable_research_jobs,
    update_research_job,
    get_research_jobs_for_task,
    count_pending_research_jobs,
    create_file_summary_job,
    get_pending_file_summary_jobs,
    get_retriable_file_summary_jobs,
    get_file_summary_job_by_sha1,
    update_file_summary_job,
    count_pending_file_summary_jobs,
    cancel_bad_file_summary_jobs,
    create_optimization_benchmark,
    get_optimization_benchmarks,
    create_arch_gen_job,
    get_pending_arch_gen_jobs,
    update_arch_gen_job,
    get_retriable_arch_gen_jobs,
)

# File + search caches
from .crud_files import (
    get_file_summary,
    create_file_summary,
    get_file_summary_by_path,
    get_file_summaries_for_project_root,
    get_search_cache,
    create_search_cache,
    delete_search_cache,
    get_last_search_time,
)

# Inbox / notifications
from .crud_inbox import (
    create_inbox_message,
    get_inbox_messages,
    get_inbox_message,
    mark_inbox_read,
    mark_all_inbox_read,
    delete_inbox_message,
    count_unread_inbox,
)

# Agent session tracking
from .crud_sessions import (
    create_agent_session,
    close_agent_session,
    close_zombie_sessions,
    close_zombie_sessions_for_tasks,
    get_agent_sessions_for_task,
)

# Dreamer run tracking
from .crud_dreamer import (
    create_dreamer_run,
    update_dreamer_run,
    get_dreamer_runs,
    get_dreamer_run,
)

# Intake clarification drafts
from .crud_clarification import (
    create_intake_draft,
    get_intake_draft,
    update_intake_draft,
    append_conversation_message,
    intake_draft_to_dict,
)

# Project survey / summarization
from .crud_survey import (
    upsert_scope_summary,
    get_scope_summary,
    list_scope_summaries,
    mark_scope_stale,
    enqueue_scope_survey_job,
    get_pending_scope_survey_jobs,
    update_scope_survey_job,
    get_scope_survey_page_jobs,
)

__all__ = [
    # session
    "DATABASE_PATH", "engine", "SessionLocal", "Base", "get_db", "init_db_tables",
    # models
    "ComputeNode", "LLM", "Budget", "Project", "Task", "BudgetEntry", "Expense",
    "TransitionVote", "TransitionResult", "SubdivisionRecord",
    "PlanningResult", "ComponentResult", "OptimizationResult",
    "SecurityReviewResult", "FinalReviewResult", "MergeRecord", "PerformanceImprovementPlan",
    "PipVerification",
    "PipResolutionJob",
    "ResearchJob", "FileSummaryJob", "OptimizationBenchmark", "ArchGenJob",
    "AgentSession",
    "FileSummary", "SearchCache", "InboxMessage",
    "DreamerRun",
    "ScopeSummary", "ScopeSurveyJob",
    "IntakeDraft",
    # crud_tasks
    "init_db", "seed_sample_tasks", "seed_task", "seed_sample_tasks_raw",
    "create_task", "get_task", "get_tasks_by_type", "get_tasks_by_project",
    "get_all_tasks", "update_task", "batch_update_map_positions", "delete_task",
    "get_task_history", "append_task_history", "touch_progress", "reorder_tasks", "batch_reorder_tasks",
    "set_big_idea_flag", "get_child_tasks", "get_active_child_tasks",
    "count_total_sub_ideas", "get_descendant_tree", "task_to_dict",
    "get_tasks_needing_clarification",
    "create_pip", "get_pips_for_task", "satisfy_pips",
    "create_pip_verification", "get_latest_pip_verification",
    "get_pip_verification_map", "get_pip_verifications_for_pip", "pip_status_at_stage",
    "create_pip_resolution_job", "get_pending_pip_resolution_jobs",
    "get_active_pip_resolution_jobs_for_task", "update_pip_resolution_job",
    # crud_projects
    "get_all_projects", "get_project", "get_project_path", "upsert_project", "delete_project",
    # crud_infra
    "get_all_llms", "get_llm", "create_llm", "update_llm", "delete_llm",
    "get_all_budgets", "get_budget", "create_budget", "update_budget", "delete_budget",
    "get_all_compute_nodes", "get_compute_node", "create_compute_node",
    "update_compute_node", "delete_compute_node",
    "llm_to_dict", "budget_to_dict",
    # crud_costs
    "create_budget_entry", "get_budget_entries", "get_budget_entry",
    "create_expense", "get_budget_spent_microcents", "get_budget_remaining_microcents",
    "budget_has_capacity", "get_budget_summary",
    # crud_pipeline
    "create_transition_vote", "get_transition_votes",
    "create_transition_result", "get_transition_results", "get_transition_votes_for_result",
    "create_subdivision_record", "get_subdivision_records", "update_subdivision_record",
    "create_planning_result", "get_planning_result", "get_latest_planning_result",
    "supersede_planning_results", "update_planning_result",
    "get_reusable_planning_result", "get_prior_failure_context",
    "mark_gate_passed", "restore_planning_result",
    "create_component_result", "get_component_results", "get_latest_dev_run_number", "update_component_result",
    "create_optimization_result", "get_optimization_result", "update_optimization_result",
    "create_security_review_result", "get_security_review_results", "update_security_review_result",
    "create_final_review_result", "get_final_review_results", "update_final_review_result",
    "create_merge_record", "get_merge_record", "update_merge_record",
    # crud_jobs
    "create_research_job", "get_research_job", "get_pending_research_jobs",
    "get_retriable_research_jobs", "update_research_job",
    "get_research_jobs_for_task", "count_pending_research_jobs",
    "create_file_summary_job", "get_pending_file_summary_jobs",
    "get_retriable_file_summary_jobs", "get_file_summary_job_by_sha1",
    "update_file_summary_job", "count_pending_file_summary_jobs", "cancel_bad_file_summary_jobs",
    "create_optimization_benchmark", "get_optimization_benchmarks",
    "create_arch_gen_job", "get_pending_arch_gen_jobs",
    "update_arch_gen_job", "get_retriable_arch_gen_jobs",
    # crud_files
    "get_file_summary", "create_file_summary", "get_file_summary_by_path",
    "get_file_summaries_for_project_root",
    "get_search_cache", "create_search_cache", "delete_search_cache", "get_last_search_time",
    # crud_inbox
    "create_inbox_message", "get_inbox_messages", "get_inbox_message",
    "mark_inbox_read", "mark_all_inbox_read", "delete_inbox_message", "count_unread_inbox",
    # crud_sessions
    "create_agent_session", "close_agent_session", "get_agent_sessions_for_task",
    # crud_dreamer
    "create_dreamer_run", "update_dreamer_run", "get_dreamer_runs", "get_dreamer_run",
    # crud_survey
    "upsert_scope_summary", "get_scope_summary", "list_scope_summaries",
    "mark_scope_stale", "enqueue_scope_survey_job",
    "get_pending_scope_survey_jobs", "update_scope_survey_job",
    "get_scope_survey_page_jobs",
    # crud_clarification
    "create_intake_draft", "get_intake_draft", "update_intake_draft",
    "append_conversation_message", "intake_draft_to_dict",
]

# Sentinel — presence of this flag on a subsequent execution means we're
# being reloaded (not imported for the first time).  The cascade logic at the
# top of this file reads it via globals().get('_initialized').
_initialized = True
