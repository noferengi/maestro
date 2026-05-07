"""
SQLAlchemy ORM models — one class per database table.

All models inherit from Base (declared in session.py).  Import order within
this file matters for ForeignKey references: referenced tables must be declared
first.  Dependency order: LLM, Budget → Task → everything else.
"""

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, JSON,
    ForeignKey, UniqueConstraint, Boolean, Float,
)
from sqlalchemy.orm import relationship, object_session
from datetime import datetime, timezone

from .session import Base


# ---------------------------------------------------------------------------
# Infrastructure / configuration tables
# ---------------------------------------------------------------------------

class ComputeNode(Base):
    """Physical or virtual compute resource that hosts one or more LLM endpoints."""
    __tablename__ = "compute_nodes"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    description = Column(Text, nullable=True)
    max_parallel_sessions = Column(Integer, nullable=False, default=1)
    max_loaded_models = Column(Integer, nullable=False, default=1)

    def __repr__(self):
        return (
            f"<ComputeNode(id={self.id}, name='{self.name}', "
            f"sessions={self.max_parallel_sessions}, models={self.max_loaded_models})>"
        )


class LLM(Base):
    """LLM endpoint configuration."""
    __tablename__ = "llms"

    id = Column(Integer, primary_key=True, autoincrement=True)
    address = Column(String, nullable=False, default='localhost')
    port = Column(Integer, nullable=False, default=8008)
    model = Column(String, nullable=False, default='')
    settings = Column(JSON, nullable=True)
    parallel_sessions = Column(Integer, nullable=False, default=1)
    max_context = Column(Integer, nullable=False, default=4096)
    notes = Column(String, nullable=False, default='')
    cost_per_million_prompt_tokens = Column(Float, nullable=False, default=0.0)
    cost_per_million_completion_tokens = Column(Float, nullable=False, default=0.0)
    compute_node_id = Column(Integer, ForeignKey("compute_nodes.id"), nullable=True)

    __table_args__ = (
        UniqueConstraint('address', 'port', 'model', name='uq_llm_endpoint'),
    )

    @property
    def label(self):
        return f"{self.address}:{self.port} serving {self.model}"

    def __repr__(self):
        return f"<LLM(id={self.id}, {self.label})>"


class Budget(Base):
    """Budget configuration with extensible settings."""
    __tablename__ = "budgets"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    dollar_amount = Column(Float, nullable=False, default=-1.0)
    settings = Column(JSON, nullable=True)

    def __repr__(self):
        return f"<Budget(id={self.id}, name='{self.name}')>"


class Project(Base):
    """
    Project registry — maps a project name to its filesystem root.

    Every task references a project by name (tasks.project) and now also by
    numeric FK (tasks.project_id).  Migration 0044 added the integer PK;
    migration 0045 (future) will drop tasks.project once all code uses project_id.
    """
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String, nullable=False, unique=True)
    path = Column(String, nullable=True)       # Absolute path to the project root
    description = Column(Text, nullable=True)
    llm_id = Column(Integer, ForeignKey("llms.id"), nullable=True)     # Default LLM for maintenance jobs
    budget_id = Column(Integer, ForeignKey("budgets.id"), nullable=True)  # Default budget for maintenance jobs
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Project(id={self.id}, name='{self.name}', path='{self.path}')>"


# ---------------------------------------------------------------------------
# Core task table
# ---------------------------------------------------------------------------

class Task(Base):
    """
    Kanban Task Model
    Represents a single task on the Kanban board with full history tracking.
    """
    __tablename__ = "tasks"

    id = Column(String, primary_key=True, index=True)
    title = Column(String, nullable=False)
    type = Column(String, nullable=False, index=True)  # planning, development, review, completed, architecture
    description = Column(Text, nullable=True)
    owner = Column(String, default="user")
    tags = Column(JSON, nullable=True, default=list)
    content = Column(JSON, nullable=True)  # For architecture tasks: frontend, backend, etc.
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    llm_ref = relationship('LLM', lazy='joined')
    budget_ref = relationship('Budget', lazy='joined')
    history = Column(JSON, nullable=True, default=list)  # Array of {status, timestamp}
    prerequisites = Column(JSON, nullable=True, default=list)  # List of prerequisite task IDs
    position = Column(Integer, nullable=True, default=0)  # Position within column (0 = first)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True)  # Numeric FK (migration 0044)
    project_ref = relationship('Project', foreign_keys=[project_id], lazy='joined')
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=True)  # Links sub-ideas to origin
    subdivision_generation = Column(Integer, nullable=False, default=0)  # Recursion depth (0=human)
    is_big_idea = Column(Boolean, nullable=False, default=False)  # Flagged when subdivision produces children
    interface_contracts = Column(Text, nullable=True)  # JSON: API contracts between sub-ideas
    review_notes = Column(Text, nullable=True)
    demotion_count = Column(Integer, nullable=False, default=0)
    demotion_history = Column(JSON, nullable=True)
    map_x = Column(Float, nullable=True)   # Saved 2D canvas X position (Column Map View)
    map_y = Column(Float, nullable=True)   # Saved 2D canvas Y position (Column Map View)
    is_active = Column(Boolean, nullable=False, default=True)  # False = soft-deleted (hidden everywhere)
    intake_exhausted_at = Column(String, nullable=True)  # Set when scheduler gives up retrying intake
    cache_mode = Column(String, nullable=False, default='normal')  # normal | force_with_context | force_fresh
    # Intake clarification fields (migration 0055)
    clarification_status = Column(String, nullable=False, default='none')  # none | pending | awaiting_user | approved | skipped
    description_original = Column(Text, nullable=True)  # Raw user input before clarification rewrite
    acceptance_criteria = Column(Text, nullable=True)  # JSON array of strings, extracted from approved clarification draft
    last_progress_at = Column(DateTime, nullable=True, default=datetime.utcnow)
    is_starred = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    @property
    def project(self) -> "str | None":
        """Backward-compat shim: returns the project name string via the FK relationship."""
        if self.project_ref is not None:
            return self.project_ref.name
        return None

    @project.setter
    def project(self, value: "str | None") -> None:
        """No-op setter — project is now set via project_id.
        Accepts the kwarg in Task(..., project=name) for backward compatibility
        with direct model construction in tests and legacy call sites.
        Use create_task(project=name) or set task.project_id directly instead.
        """

    def __repr__(self):
        return f"<Task(id={self.id}, title='{self.title}', type='{self.type}', project_id={self.project_id}, position={self.position})>"


# ---------------------------------------------------------------------------
# Cost / budget tracking tables
# ---------------------------------------------------------------------------

class BudgetEntry(Base):
    """Individual LLM call log entry for cost tracking and dataset building."""
    __tablename__ = "budget_entries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=True)
    prompt_cost = Column(Integer, nullable=False, default=0)        # total prompt tokens
    generation_cost = Column(Integer, nullable=False, default=0)    # total completion tokens
    tool_calls = Column(Integer, nullable=False, default=0)         # total LLM turns
    prompt_data = Column(Text, nullable=True)                       # full prompt messages (JSON)
    response_data = Column(Text, nullable=True)                     # full response (JSON)
    session_id = Column(String, nullable=True)                      # UUID shared by all calls in one agent run
    agent_name = Column(String, nullable=True)                      # e.g. "Subdivision Agent"
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BudgetEntry(id={self.id}, llm={self.llm_id}, budget={self.budget_id}, task={self.task_id}, agent={self.agent_name}, prompt={self.prompt_cost}, gen={self.generation_cost})>"


class Expense(Base):
    """Per-LLM-call cost record in microcents (µ¢ = millionths of a US cent).

    One row per LLM call — 1:1 with budget_entries.
    3-way identity: budget_id + llm_id + budget_entry_id (remote_call_id for external audit).
    Token columns are always populated.  Cost columns are 0 when LLM rates = $0.00/M.
    """
    __tablename__ = "expenses"

    id = Column(Integer, primary_key=True, autoincrement=True)
    budget_entry_id = Column(Integer, ForeignKey('budget_entries.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    remote_call_id = Column(String, nullable=True)          # API response "id" (chatcmpl-xxx)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=True)
    prompt_tokens = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)
    total_tokens = Column(Integer, nullable=False, default=0)   # stored sum for easy aggregation
    prompt_cost_microcents = Column(Integer, nullable=False, default=0)
    completion_cost_microcents = Column(Integer, nullable=False, default=0)
    total_cost_microcents = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Expense(id={self.id}, budget={self.budget_id}, tokens={self.total_tokens}, total_µ¢={self.total_cost_microcents})>"


# ---------------------------------------------------------------------------
# Pipeline audit / result tables (one per pipeline stage)
# ---------------------------------------------------------------------------

class TransitionVote(Base):
    __tablename__ = "transition_votes"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    transition = Column(String, nullable=False)
    stage = Column(String, nullable=False)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    raw_response = Column(JSON, nullable=True)
    prompt_tokens = Column(Integer, nullable=True)
    completion_tokens = Column(Integer, nullable=True)
    model = Column(String, nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class TransitionResult(Base):
    __tablename__ = "transition_results"
    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    transition = Column(String, nullable=False)
    outcome = Column(String, nullable=False)
    vote_summary = Column(JSON, nullable=True)
    total_prompt_tokens = Column(Integer, nullable=True)
    total_completion_tokens = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class SubdivisionRecord(Base):
    """Audit trail for task subdivision attempts."""
    __tablename__ = "subdivision_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    attempt_number = Column(Integer, nullable=False, default=1)
    generation = Column(Integer, nullable=False, default=1)
    child_task_ids = Column(JSON, nullable=False)
    rejection_context = Column(JSON, nullable=True)
    agent_vote = Column(JSON, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    interface_contracts = Column(Text, nullable=True)  # JSON: interface contracts from subdivision agent
    status = Column(String, nullable=False, default='active')  # active | superseded | failed
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SubdivisionRecord(id={self.id}, parent={self.parent_task_id}, attempt={self.attempt_number}, status={self.status})>"


class PlanningResult(Base):
    """Audit trail for planning pipeline results."""
    __tablename__ = "planning_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    file_manifest = Column(Text, nullable=True)
    dependency_graph = Column(Text, nullable=True)
    interface_contracts = Column(Text, nullable=True)
    test_strategy = Column(Text, nullable=True)
    implementation_steps = Column(Text, nullable=True)
    mermaid_diagrams = Column(Text, nullable=True)
    pitfalls_identified = Column(Text, nullable=True)
    review_votes = Column(Text, nullable=True)
    codebase_survey = Column(Text, nullable=True)
    best_of_n_designs = Column(Text, nullable=True)
    selected_design_index = Column(Integer, nullable=True)
    selection_justification = Column(Text, nullable=True)
    gate_checks = Column(Text, nullable=True)   # JSON: [{name, passed, hard_fail, detail}]
    error_message = Column(Text, nullable=True)  # set on status='failed' rows
    content_hash = Column(String, nullable=True)   # SHA256(title || description) at run time
    was_gate_passed = Column(Integer, nullable=False, default=0)  # 1 = gate passed; enables cache reuse
    confidence = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    status = Column(String, nullable=False, default='active')
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PlanningResult(id={self.id}, task={self.task_id}, status={self.status})>"


class ComponentResult(Base):
    """Audit trail for component-level development agents."""
    __tablename__ = "component_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    component_name = Column(String, nullable=False)
    step_order = Column(Integer, nullable=False)
    batch_number = Column(Integer, nullable=False)
    dev_run_number = Column(Integer, nullable=False, default=0)
    status = Column(String, nullable=False, default='pending')
    files_changed = Column(Text, nullable=True)
    tests_passed = Column(Integer, default=0)
    turns_used = Column(Integer, default=0)
    error_detail = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    test_output = Column(Text, nullable=True)
    coverage_pct = Column(Float, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ComponentResult(id={self.id}, task={self.task_id}, component={self.component_name})>"


class OptimizationResult(Base):
    """Audit trail for optimization pipeline."""
    __tablename__ = "optimization_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    baseline_report = Column(Text, nullable=True)
    proposals = Column(Text, nullable=True)
    judge_scores = Column(Text, nullable=True)
    winning_proposal_index = Column(Integer, nullable=True)
    winning_score = Column(Integer, nullable=True)  # stored as int to avoid Float import
    post_report = Column(Text, nullable=True)
    improvement_summary = Column(Text, nullable=True)
    outcome = Column(String, nullable=False)
    total_prompt_tokens = Column(Integer, default=0)
    total_completion_tokens = Column(Integer, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<OptimizationResult(id={self.id}, task={self.task_id}, outcome={self.outcome})>"


class SecurityReviewResult(Base):
    """Security review findings with veto power."""
    __tablename__ = "security_review_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    reviewer_type = Column(String, nullable=False)
    owasp_findings = Column(Text, nullable=True)
    secrets_detected = Column(Text, nullable=True)
    dependency_vulnerabilities = Column(Text, nullable=True)
    data_flow_map = Column(Text, nullable=True)
    compliance_findings = Column(Text, nullable=True)
    optimization_regressions = Column(Text, nullable=True)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    critical_count = Column(Integer, default=0)
    high_count = Column(Integer, default=0)
    raw_response = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    model = Column(String, nullable=True)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SecurityReviewResult(id={self.id}, task={self.task_id}, type={self.reviewer_type})>"


class FinalReviewResult(Base):
    """Final AI review findings."""
    __tablename__ = "final_review_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    reviewer_type = Column(String, nullable=False)
    test_results = Column(Text, nullable=True)
    quality_findings = Column(Text, nullable=True)
    requirements_mapping = Column(Text, nullable=True)
    integration_checks = Column(Text, nullable=True)
    verdict = Column(String, nullable=False)
    confidence = Column(Integer, nullable=False)
    justification = Column(Text, nullable=True)
    raw_response = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    model = Column(String, nullable=True)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<FinalReviewResult(id={self.id}, task={self.task_id}, type={self.reviewer_type})>"


class MergeRecord(Base):
    """Audit trail for merge-to-main operations."""
    __tablename__ = "merge_records"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    branch_name = Column(String, nullable=False)
    merge_commit_sha = Column(String, nullable=True)
    status = Column(String, nullable=False)
    test_output = Column(Text, nullable=True)
    error_detail = Column(Text, nullable=True)
    security_review_ids = Column(Text, nullable=True)
    final_review_ids = Column(Text, nullable=True)
    total_pipeline_tokens = Column(Integer, default=0)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MergeRecord(id={self.id}, task={self.task_id}, status={self.status})>"


class PerformanceImprovementPlan(Base):
    """Quality gate requirements generated after a task demotion."""
    __tablename__ = "performance_improvement_plans"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    origin_stage = Column(String, nullable=False)
    requirements = Column(Text, nullable=False)        # JSON bullet points
    status = Column(String, nullable=False, default='active')  # deprecated — use pip_verifications
    verified_at = Column(DateTime, nullable=True)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    created_at_commit = Column(String, nullable=False, default='none')  # git SHA at creation time
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<PerformanceImprovementPlan(id={self.id}, task={self.task_id}, status={self.status})>"


class PipVerification(Base):
    """Audit trail for pre-flight PIP gate checks — one row per (pip, stage, run)."""
    __tablename__ = "pip_verifications"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pip_id = Column(Integer, ForeignKey("performance_improvement_plans.id"), nullable=False)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    checked_at_stage = Column(String, nullable=False)
    outcome = Column(String, nullable=False)   # 'passed' | 'failed' | 'pending'
    summary = Column(Text, nullable=True)
    findings = Column(Text, nullable=True)     # JSON: [{requirement, status, detail}]
    agent_session_id = Column(String, nullable=True)
    created_at = Column(String, nullable=False)

    def __repr__(self):
        return (
            f"<PipVerification(id={self.id}, pip={self.pip_id}, "
            f"stage={self.checked_at_stage!r}, outcome={self.outcome!r})>"
        )


class IntakeDraft(Base):
    """Working draft produced by the clarification agent for an IDEA card.

    One row per task (UNIQUE on task_id).  Holds the LLM-rewritten description,
    suggested prerequisites, suggested subtasks, and the running conversation
    history between the user and the refinement LLM.
    """
    __tablename__ = "intake_drafts"

    id                      = Column(Integer, primary_key=True, autoincrement=True)
    task_id                 = Column(String, ForeignKey('tasks.id'), nullable=False, unique=True)
    rewritten_description   = Column(Text, nullable=True)
    design_rationale        = Column(Text, nullable=True)
    acceptance_criteria     = Column(Text, nullable=True)   # JSON array of strings
    out_of_scope            = Column(Text, nullable=True)
    open_questions          = Column(Text, nullable=True)   # JSON array of strings
    suggested_prerequisites = Column(Text, nullable=True)   # JSON: [{task_id, title, reason}]
    suggested_subtasks      = Column(Text, nullable=True)   # JSON: [{title, description, order}]
    conversation_history    = Column(Text, nullable=True)   # JSON: [{role, content, timestamp}]
    agent_token_cost        = Column(Integer, nullable=True)
    created_at              = Column(String, nullable=False)
    updated_at              = Column(String, nullable=False)

    def __repr__(self):
        return f"<IntakeDraft(id={self.id}, task={self.task_id})>"


# ---------------------------------------------------------------------------
# Background job tables
# ---------------------------------------------------------------------------

class ResearchJob(Base):
    """Background research job — tracks inline and queued agent investigations."""
    __tablename__ = "research_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    parent_job_id = Column(Integer, ForeignKey('research_jobs.id'), nullable=True)
    question = Column(Text, nullable=False)
    context = Column(Text, nullable=True)           # JSON string
    status = Column(String, nullable=False, default='pending')
    priority = Column(Float, nullable=False, default=0.0)   # lower = higher priority
    depth = Column(Integer, nullable=False, default=0)
    verdict = Column(Text, nullable=True)           # JSON vote dict
    findings = Column(Text, nullable=True)
    lives_used = Column(Integer, default=0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ResearchJob(id={self.id}, task={self.task_id}, status={self.status})>"


class FileSummaryJob(Base):
    """Background job for scheduler-dispatched file summary LLM calls."""
    __tablename__ = "file_summary_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sha1_hash = Column(String, nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    file_path = Column(String, nullable=False)
    file_content = Column(Text, nullable=False)          # capped at 32k chars by enqueue
    static_analysis_json = Column(Text, nullable=True)
    status = Column(String, nullable=False, default='pending')
    priority = Column(Float, nullable=False, default=-1.0)  # negative = above research (0.0)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    task_id = Column(String, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    previous_summary = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<FileSummaryJob(id={self.id}, sha1={self.sha1_hash[:8]}…, status='{self.status}')>"


class OptimizationBenchmark(Base):
    """Before/after profiling metrics for optimization sub-tasks."""
    __tablename__ = "optimization_benchmarks"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=False)
    benchmark_type = Column(String, nullable=False)     # 'before' | 'after'
    metrics = Column(Text, nullable=False)              # JSON
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<OptimizationBenchmark(id={self.id}, task={self.task_id}, type={self.benchmark_type})>"


class PipResolutionJob(Base):
    """Background job for scheduler-dispatched PIP resolution agents.

    Lifecycle: pending → researching → resolving → done | failed
    One row per (task, pip) blocking event.  A new row is created each time a
    pre-flight gate blocks a stage transition for a given PIP.
    """
    __tablename__ = "pip_resolution_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    task_id = Column(String, ForeignKey("tasks.id"), nullable=False)
    pip_id = Column(Integer, ForeignKey("performance_improvement_plans.id"), nullable=False)
    stage_blocked_at = Column(String, nullable=False)
    research_findings = Column(Text, nullable=True)
    status = Column(String, nullable=False, default="pending")
    created_at = Column(String, nullable=False)

    def __repr__(self):
        return (
            f"<PipResolutionJob(id={self.id}, task={self.task_id!r}, "
            f"pip={self.pip_id}, stage={self.stage_blocked_at!r}, status={self.status!r})>"
        )


class ArchGenJob(Base):
    """Background job for scheduler-dispatched architecture card generation."""
    __tablename__ = "arch_gen_jobs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    project_id = Column(Integer, ForeignKey('projects.id'), nullable=True)
    project_ref = relationship('Project', foreign_keys=[project_id], lazy='joined')
    category = Column(String, nullable=False)
    llm_id = Column(Integer, ForeignKey('llms.id'), nullable=True)
    budget_id = Column(Integer, ForeignKey('budgets.id'), nullable=True)
    status = Column(String, nullable=False, default='pending')
    priority = Column(Float, nullable=False, default=1.0)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    error_message = Column(Text, nullable=True)
    retry_count = Column(Integer, nullable=False, default=0)
    created_at = Column(DateTime, default=datetime.utcnow)
    completed_at = Column(DateTime, nullable=True)

    @property
    def project(self) -> "str | None":
        """Backward-compat shim: returns the project name string via the FK relationship."""
        if self.project_ref is not None:
            return self.project_ref.name
        return None

    @project.setter
    def project(self, value: "str | None") -> None:
        """No-op setter for backward compatibility. Set project_id directly instead."""

    def __repr__(self):
        return f"<ArchGenJob(id={self.id}, project_id={self.project_id}, category={self.category!r}, status={self.status!r}, retries={self.retry_count})>"


# ---------------------------------------------------------------------------
# Agent session tracking
# ---------------------------------------------------------------------------

class AgentSession(Base):
    """Persistent record of a single agent invocation.

    One row is written when an agent starts and updated when it exits.
    Covers all scheduler-dispatched workers and API-triggered pipelines.

    agent_type values:
        intake, planning, maestro_loop, dev_orchestrator, conceptual_review,
        optimization, security, final_review, pip_preflight, pip_research,
        pip_resolution, subdivision, arch_gen

    exit_reason values:
        completed, max_turns, stalled, error, shutdown, passed, rejected,
        subdivide, pip_blocked

    scheduler_reason values:
        scheduler, user_triggered
    """
    __tablename__ = "agent_sessions"

    id                = Column(Integer, primary_key=True, autoincrement=True)
    task_id           = Column(String, nullable=False)  # No FK — synthetic IDs (e.g. "survey-N") are valid
    agent_type        = Column(String, nullable=False)
    started_at        = Column(String, nullable=False)
    ended_at          = Column(String, nullable=True)
    turn_count        = Column(Integer, nullable=True)
    max_turns         = Column(Integer, nullable=True)
    exit_reason       = Column(String, nullable=True)
    exit_summary      = Column(Text, nullable=True)
    scheduler_reason  = Column(String, nullable=False, default="scheduler")
    llm_id            = Column(Integer, nullable=True)
    budget_id         = Column(Integer, nullable=True)
    prompt_tokens     = Column(Integer, nullable=False, default=0)
    completion_tokens = Column(Integer, nullable=False, default=0)

    def __repr__(self):
        return (
            f"<AgentSession(id={self.id}, task={self.task_id!r}, "
            f"type={self.agent_type!r}, reason={self.exit_reason!r})>"
        )


# ---------------------------------------------------------------------------
# Dreamer run log
# ---------------------------------------------------------------------------

class DreamerRun(Base):
    """Audit record for a single Dreamer agent invocation.

    Dreamer fires when a project has had no pipeline progress for
    DREAMER_STALL_TICKS consecutive scheduler ticks.  One row per run.

    status values: running | completed | failed
    """
    __tablename__ = "dreamer_runs"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    project_name = Column(String, nullable=False)
    started_at   = Column(String, nullable=False)
    finished_at  = Column(String, nullable=True)
    status       = Column(String, nullable=False, default="running")
    stall_reason = Column(Text, nullable=True)
    actions_taken = Column(Text, nullable=True)    # JSON list of {action, ...}
    new_task_ids  = Column(Text, nullable=True)    # JSON list of task ID strings
    budget_id    = Column(Integer, ForeignKey("budgets.id"), nullable=True)
    llm_id       = Column(Integer, ForeignKey("llms.id"),    nullable=True)

    def __repr__(self):
        return (
            f"<DreamerRun(id={self.id}, project={self.project_name!r}, "
            f"status={self.status!r})>"
        )


# ---------------------------------------------------------------------------
# Cache tables
# ---------------------------------------------------------------------------

class FileSummary(Base):
    """DB-cached natural-language file summary keyed on SHA1 + file size.

    The cache key is (sha1_hash, file_size_bytes) — identical file content
    at any path will hit the same row.  file_path is informational only.
    """
    __tablename__ = "file_summaries"

    id = Column(Integer, primary_key=True, autoincrement=True)
    sha1_hash = Column(String, nullable=False)
    file_size_bytes = Column(Integer, nullable=False)
    file_path = Column(String, nullable=False)          # last-known path, not a key
    summary = Column(Text, nullable=False)              # comprehensive multi-paragraph description
    short_summary = Column(Text, nullable=True)         # exactly 2 sentences for listings/snapshots
    static_analysis_json = Column(Text, nullable=True)  # JSON from static_analysis.analyze_file()
    created_at = Column(DateTime, default=datetime.utcnow)

    __table_args__ = (
        UniqueConstraint('sha1_hash', 'file_size_bytes', name='uq_file_summary_sha1_size'),
    )

    def __repr__(self):
        return f"<FileSummary(id={self.id}, sha1={self.sha1_hash[:8]}…, path='{self.file_path}')>"


class SearchCache(Base):
    """
    Local cache of web search results.
    Prevents redundant API calls for identical queries.
    """
    __tablename__ = "search_cache"

    id = Column(Integer, primary_key=True, autoincrement=True)
    query = Column(String, nullable=False, index=True, unique=True)
    result_json = Column(Text, nullable=False)  # Full JSON response from the search provider
    provider = Column(String, nullable=False, default='brave')
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<SearchCache(id={self.id}, query='{self.query[:40]}…', provider='{self.provider}')>"


# ---------------------------------------------------------------------------
# Inbox / notification table
# ---------------------------------------------------------------------------

class InboxMessage(Base):
    """
    Persistent notification inbox — stores pipeline results and agent alerts
    for later review by the user.  One row per notification event.
    """
    __tablename__ = "inbox_messages"

    id = Column(String, primary_key=True)                       # UUID
    subject = Column(String, nullable=False)
    source_type = Column(String, nullable=False, default='intake_result')
    task_id = Column(String, nullable=True)                     # soft ref — no FK (task may be deleted)
    task_title = Column(String, nullable=True)                  # snapshot at creation time
    outcome = Column(String, nullable=True)                     # rejected | passed | failed | subdivide
    data_json = Column(Text, nullable=True)                     # full payload snapshot (JSON string)
    read = Column(Boolean, nullable=False, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<InboxMessage(id={self.id[:8]}…, subject='{self.subject[:40]}', read={self.read})>"


# ---------------------------------------------------------------------------
# Project survey / summarization tables
# ---------------------------------------------------------------------------

class ScopeSummary(Base):
    """Hierarchical project health summaries (Directory -> Module -> Project)."""
    __tablename__ = "scope_summaries"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    project_name    = Column(String, nullable=False)
    scope_type      = Column(String, nullable=False)    # 'directory' | 'module' | 'collection' | 'project'
    scope_key       = Column(String, nullable=False)    # rel_dir, module name, or '__ROOT__'
    parent_scope_key = Column(String, nullable=True)    # enables hierarchy navigation
    depth           = Column(Integer, nullable=False, default=0)
    summary         = Column(Text, nullable=False)
    short_summary   = Column(Text, nullable=True)       # 2-sentence version for context injection
    file_paths      = Column(Text, nullable=True)       # JSON array of relative paths in this scope
    file_count      = Column(Integer, nullable=False, default=0)
    content_hash    = Column(String, nullable=True)     # SHA1 of sorted child hashes (staleness key)
    git_commit      = Column(String, nullable=True)     # HEAD at generation time
    staleness_state = Column(String, nullable=False, default="fresh") # fresh | stale | checking
    llm_id          = Column(Integer, nullable=True)
    budget_id       = Column(Integer, nullable=True)
    created_at      = Column(DateTime, default=datetime.utcnow)
    updated_at      = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<ScopeSummary(id={self.id}, project={self.project_name!r}, type={self.scope_type!r}, key={self.scope_key!r})>"


class ScopeSurveyJob(Base):
    """Background job for scheduler-dispatched project survey LLM calls."""
    __tablename__ = "scope_survey_jobs"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    project_name    = Column(String, nullable=False)
    scope_type      = Column(String, nullable=False)
    scope_key       = Column(String, nullable=False)
    action          = Column(String, nullable=False, default='generate') # generate | staleness_check | edit_summary
    status          = Column(String, nullable=False, default='pending')  # pending | running | done | failed
    priority        = Column(Float, nullable=False, default=0.0)
    llm_id          = Column(Integer, nullable=True)
    budget_id       = Column(Integer, nullable=True)
    prompt_tokens   = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
    error_message   = Column(Text, nullable=True)
    retry_count     = Column(Integer, nullable=False, default=0)
    created_at      = Column(DateTime, default=datetime.utcnow)
    completed_at    = Column(DateTime, nullable=True)

    def __repr__(self):
        return f"<ScopeSurveyJob(id={self.id}, project={self.project_name!r}, type={self.scope_type!r}, key={self.scope_key!r}, status={self.status!r})>"
