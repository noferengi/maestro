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
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from .session import Base


# ---------------------------------------------------------------------------
# Infrastructure / configuration tables
# ---------------------------------------------------------------------------

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

    Every task references a project by name (tasks.project).  This table
    stores the canonical filesystem path so the agent can run git operations
    in the correct repository instead of Maestro's own source tree.
    """
    __tablename__ = "projects"

    name = Column(String, primary_key=True)
    path = Column(String, nullable=True)       # Absolute path to the project root
    description = Column(Text, nullable=True)
    llm_id = Column(Integer, ForeignKey("llms.id"), nullable=True)     # Default LLM for maintenance jobs
    budget_id = Column(Integer, ForeignKey("budgets.id"), nullable=True)  # Default budget for maintenance jobs
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<Project(name='{self.name}', path='{self.path}')>"


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
    project = Column(String, default='TheMaestro')  # Project this task belongs to
    parent_task_id = Column(String, ForeignKey('tasks.id'), nullable=True)  # Links sub-ideas to origin
    subdivision_generation = Column(Integer, nullable=False, default=0)  # Recursion depth (0=human)
    is_big_idea = Column(Boolean, nullable=False, default=False)  # Flagged when subdivision produces children
    interface_contracts = Column(Text, nullable=True)  # JSON: API contracts between sub-ideas
    review_notes = Column(Text, nullable=True)
    demotion_count = Column(Integer, nullable=False, default=0)
    demotion_history = Column(JSON, nullable=True)
    map_x = Column(Float, nullable=True)   # Saved 2D canvas X position (Column Map View)
    map_y = Column(Float, nullable=True)   # Saved 2D canvas Y position (Column Map View)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<Task(id={self.id}, title='{self.title}', type='{self.type}', project='{self.project}', position={self.position})>"


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
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<BudgetEntry(id={self.id}, llm={self.llm_id}, budget={self.budget_id}, task={self.task_id}, prompt={self.prompt_cost}, gen={self.generation_cost})>"


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
    status = Column(String, nullable=False, default='pending')
    files_changed = Column(Text, nullable=True)
    tests_passed = Column(Integer, default=0)
    turns_used = Column(Integer, default=0)
    error_detail = Column(Text, nullable=True)
    prompt_tokens = Column(Integer, default=0)
    completion_tokens = Column(Integer, default=0)
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


class FullReviewResult(Base):
    """Full/final review findings."""
    __tablename__ = "full_review_results"

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
        return f"<FullReviewResult(id={self.id}, task={self.task_id}, type={self.reviewer_type})>"


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
    full_review_ids = Column(Text, nullable=True)
    total_pipeline_tokens = Column(Integer, default=0)
    llm_id = Column(Integer, nullable=True)
    budget_id = Column(Integer, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<MergeRecord(id={self.id}, task={self.task_id}, status={self.status})>"


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
    summary = Column(Text, nullable=False)
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
