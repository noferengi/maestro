"""
Migration 0067 — PostgreSQL baseline schema verification.

This migration acts as the canonical PostgreSQL baseline: it records the
verified schema state of the live database at the point the project
transitioned from SQLite-history migrations to PostgreSQL-native ones.

Behaviour
---------
- SQLite: no-op (the 0001–0066 chain already owns SQLite history).
- PostgreSQL: checks that every expected table and column exists with a
  compatible type.  Extra tables and extra columns are silently tolerated
  (forward-compatible).  Missing tables, missing columns, or incompatible
  types raise RuntimeError with specific details so the operator knows
  exactly what is wrong before touching anything.

Adding a new environment
------------------------
New PostgreSQL installations should restore from the schema dump
(baseline_postgresql.sql in the project root) and then mark migrations
0001–0066 as applied without running them:

    psql -f baseline_postgresql.sql ...
    python app/migrations/runner.py migrate   # only 0067+ will run

This migration will then pass because the restored schema matches.
"""

description = "postgresql baseline schema verification"

# ---------------------------------------------------------------------------
# Expected schema captured from the verified live database on 2026-05-13.
# Keys are table names; values map column name → PostgreSQL data_type as
# reported by information_schema.columns.
# ---------------------------------------------------------------------------
_EXPECTED_SCHEMA = {
    'agent_sessions': {
        'id': 'integer',
        'task_id': 'text',
        'agent_type': 'text',
        'started_at': 'text',
        'ended_at': 'text',
        'turn_count': 'integer',
        'max_turns': 'integer',
        'exit_reason': 'text',
        'exit_summary': 'text',
        'scheduler_reason': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
    },
    'arch_gen_jobs': {
        'id': 'integer',
        'category': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'status': 'text',
        'priority': 'real',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'error_message': 'text',
        'created_at': 'timestamp without time zone',
        'completed_at': 'timestamp without time zone',
        'retry_count': 'integer',
        'project_id': 'integer',
    },
    'budget_entries': {
        'id': 'integer',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'task_id': 'text',
        'prompt_cost': 'integer',
        'generation_cost': 'integer',
        'tool_calls': 'integer',
        'prompt_data': 'text',
        'response_data': 'text',
        'created_at': 'timestamp without time zone',
        'session_id': 'text',
        'agent_name': 'text',
    },
    'budget_token_totals': {
        'budget_id': 'integer',
        'total_entries': 'bigint',
        'total_prompt_tokens': 'bigint',
        'total_completion_tokens': 'bigint',
        'total_tokens': 'bigint',
    },
    'budgets': {
        'id': 'integer',
        'name': 'text',
        'settings': 'json',
        'dollar_amount': 'real',
    },
    'component_results': {
        'id': 'integer',
        'task_id': 'text',
        'component_name': 'text',
        'step_order': 'integer',
        'batch_number': 'integer',
        'status': 'text',
        'files_changed': 'text',
        'tests_passed': 'integer',
        'turns_used': 'integer',
        'error_detail': 'text',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'created_at': 'timestamp without time zone',
        'completed_at': 'timestamp without time zone',
        'dev_run_number': 'integer',
        'test_output': 'text',
        'coverage_pct': 'real',
    },
    'compute_nodes': {
        'id': 'integer',
        'name': 'text',
        'description': 'text',
        'max_parallel_sessions': 'integer',
        'max_loaded_models': 'integer',
    },
    'expenses': {
        'id': 'integer',
        'budget_entry_id': 'integer',
        'budget_id': 'integer',
        'llm_id': 'integer',
        'remote_call_id': 'text',
        'task_id': 'text',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'total_tokens': 'integer',
        'prompt_cost_microcents': 'integer',
        'completion_cost_microcents': 'integer',
        'total_cost_microcents': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'file_summaries': {
        'id': 'integer',
        'sha1_hash': 'text',
        'file_size_bytes': 'integer',
        'file_path': 'text',
        'summary': 'text',
        'static_analysis_json': 'text',
        'created_at': 'timestamp without time zone',
        'short_summary': 'text',
    },
    'file_summary_jobs': {
        'id': 'integer',
        'sha1_hash': 'text',
        'file_size_bytes': 'integer',
        'file_path': 'text',
        'file_content': 'text',
        'static_analysis_json': 'text',
        'status': 'text',
        'priority': 'real',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'task_id': 'text',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'error_message': 'text',
        'created_at': 'timestamp without time zone',
        'completed_at': 'timestamp without time zone',
        'previous_summary': 'text',
    },
    'final_review_results': {
        'id': 'integer',
        'task_id': 'text',
        'reviewer_type': 'text',
        'test_results': 'text',
        'quality_findings': 'text',
        'requirements_mapping': 'text',
        'integration_checks': 'text',
        'verdict': 'text',
        'confidence': 'integer',
        'justification': 'text',
        'raw_response': 'text',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'model': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'inbox_messages': {
        'id': 'text',
        'subject': 'text',
        'source_type': 'text',
        'task_id': 'text',
        'task_title': 'text',
        'outcome': 'text',
        'data_json': 'text',
        'read': 'boolean',
        'created_at': 'text',
    },
    'intake_drafts': {
        'id': 'integer',
        'task_id': 'text',
        'rewritten_description': 'text',
        'design_rationale': 'text',
        'acceptance_criteria': 'text',
        'out_of_scope': 'text',
        'open_questions': 'text',
        'suggested_prerequisites': 'text',
        'suggested_subtasks': 'text',
        'conversation_history': 'text',
        'agent_token_cost': 'integer',
        'created_at': 'text',
        'updated_at': 'text',
    },
    'llms': {
        'id': 'integer',
        'address': 'text',
        'port': 'integer',
        'model': 'text',
        'settings': 'json',
        'parallel_sessions': 'integer',
        'max_context': 'integer',
        'notes': 'text',
        'cost_per_million_prompt_tokens': 'real',
        'cost_per_million_completion_tokens': 'real',
        'compute_node_id': 'integer',
    },
    'maestro_runs': {
        'id': 'integer',
        'project_name': 'text',
        'started_at': 'text',
        'finished_at': 'text',
        'status': 'text',
        'stall_reason': 'text',
        'actions_taken': 'text',
        'new_task_ids': 'text',
        'budget_id': 'integer',
        'llm_id': 'integer',
    },
    'merge_records': {
        'id': 'integer',
        'task_id': 'text',
        'branch_name': 'text',
        'merge_commit_sha': 'text',
        'status': 'text',
        'test_output': 'text',
        'error_detail': 'text',
        'security_review_ids': 'text',
        'final_review_ids': 'text',
        'total_pipeline_tokens': 'integer',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'optimization_benchmarks': {
        'id': 'integer',
        'task_id': 'text',
        'parent_task_id': 'text',
        'benchmark_type': 'text',
        'metrics': 'text',
        'created_at': 'timestamp without time zone',
    },
    'optimization_results': {
        'id': 'integer',
        'task_id': 'text',
        'baseline_report': 'text',
        'proposals': 'text',
        'judge_scores': 'text',
        'winning_proposal_index': 'integer',
        'winning_score': 'real',
        'post_report': 'text',
        'improvement_summary': 'text',
        'outcome': 'text',
        'total_prompt_tokens': 'integer',
        'total_completion_tokens': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'performance_improvement_plans': {
        'id': 'integer',
        'task_id': 'text',
        'origin_stage': 'text',
        'requirements': 'text',
        'status': 'text',
        'verified_at': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'created_at': 'text',
        'created_at_commit': 'text',
    },
    'pip_resolution_jobs': {
        'id': 'integer',
        'task_id': 'text',
        'pip_id': 'integer',
        'stage_blocked_at': 'text',
        'research_findings': 'text',
        'status': 'text',
        'created_at': 'text',
    },
    'pip_verifications': {
        'id': 'integer',
        'pip_id': 'integer',
        'task_id': 'text',
        'checked_at_stage': 'text',
        'outcome': 'text',
        'summary': 'text',
        'findings': 'text',
        'agent_session_id': 'text',
        'created_at': 'text',
    },
    'planning_results': {
        'id': 'integer',
        'task_id': 'text',
        'file_manifest': 'text',
        'dependency_graph': 'text',
        'interface_contracts': 'text',
        'test_strategy': 'text',
        'implementation_steps': 'text',
        'mermaid_diagrams': 'text',
        'pitfalls_identified': 'text',
        'review_votes': 'text',
        'codebase_survey': 'text',
        'best_of_n_designs': 'text',
        'selected_design_index': 'integer',
        'selection_justification': 'text',
        'confidence': 'integer',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'status': 'text',
        'created_at': 'timestamp without time zone',
        'gate_checks': 'text',
        'error_message': 'text',
        'correction_attempts': 'integer',
        'content_hash': 'character varying',
        'was_gate_passed': 'boolean',
    },
    'project_decisions': {
        'id': 'integer',
        'project_id': 'integer',
        'topic': 'text',
        'decision': 'text',
        'rationale': 'text',
        'is_binding': 'boolean',
        'created_at': 'timestamp without time zone',
    },
    'projects': {
        'id': 'integer',
        'name': 'text',
        'path': 'text',
        'description': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'text',
    },
    'research_jobs': {
        'id': 'integer',
        'task_id': 'text',
        'parent_job_id': 'integer',
        'question': 'text',
        'context': 'text',
        'status': 'text',
        'priority': 'real',
        'depth': 'integer',
        'verdict': 'text',
        'findings': 'text',
        'lives_used': 'integer',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
        'completed_at': 'timestamp without time zone',
    },
    'scope_summaries': {
        'id': 'integer',
        'project_name': 'text',
        'scope_type': 'text',
        'scope_key': 'text',
        'parent_scope_key': 'text',
        'depth': 'integer',
        'summary': 'text',
        'short_summary': 'text',
        'file_paths': 'text',
        'file_count': 'integer',
        'content_hash': 'text',
        'git_commit': 'text',
        'staleness_state': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
        'updated_at': 'timestamp without time zone',
    },
    'scope_survey_jobs': {
        'id': 'integer',
        'project_name': 'text',
        'scope_type': 'text',
        'scope_key': 'text',
        'action': 'text',
        'status': 'text',
        'priority': 'real',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'error_message': 'text',
        'retry_count': 'integer',
        'created_at': 'timestamp without time zone',
        'completed_at': 'timestamp without time zone',
    },
    'search_cache': {
        'id': 'integer',
        'query': 'text',
        'result_json': 'text',
        'provider': 'text',
        'created_at': 'timestamp without time zone',
    },
    'security_review_results': {
        'id': 'integer',
        'task_id': 'text',
        'reviewer_type': 'text',
        'owasp_findings': 'text',
        'secrets_detected': 'text',
        'dependency_vulnerabilities': 'text',
        'data_flow_map': 'text',
        'compliance_findings': 'text',
        'optimization_regressions': 'text',
        'verdict': 'text',
        'confidence': 'integer',
        'justification': 'text',
        'critical_count': 'integer',
        'high_count': 'integer',
        'raw_response': 'text',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'model': 'text',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'subdivision_records': {
        'id': 'integer',
        'parent_task_id': 'text',
        'attempt_number': 'integer',
        'generation': 'integer',
        'child_task_ids': 'json',
        'rejection_context': 'json',
        'agent_vote': 'json',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'status': 'text',
        'created_at': 'timestamp without time zone',
        'interface_contracts': 'text',
    },
    'system_settings': {
        'key': 'character varying',
        'value': 'json',
        'description': 'character varying',
        'updated_at': 'timestamp without time zone',
    },
    'task_session_states': {
        'task_id': 'text',
        'session_id': 'integer',
        'turn_count': 'integer',
        'messages': 'text',
        'updated_at': 'timestamp without time zone',
    },
    'tasks': {
        'id': 'text',
        'title': 'text',
        'type': 'text',
        'description': 'text',
        'owner': 'text',
        'tags': 'json',
        'content': 'json',
        'llm_id': 'integer',
        'budget_id': 'integer',
        'history': 'json',
        'position': 'integer',
        'created_at': 'timestamp without time zone',
        'updated_at': 'timestamp without time zone',
        'prerequisites': 'json',
        'parent_task_id': 'text',
        'subdivision_generation': 'integer',
        'is_big_idea': 'boolean',
        'interface_contracts': 'text',
        'review_notes': 'text',
        'demotion_count': 'integer',
        'demotion_history': 'json',
        'map_x': 'real',
        'map_y': 'real',
        'is_active': 'boolean',
        'project_id': 'integer',
        'intake_exhausted_at': 'text',
        'cache_mode': 'character varying',
        'clarification_status': 'text',
        'description_original': 'text',
        'acceptance_criteria': 'text',
        'last_progress_at': 'timestamp without time zone',
        'is_starred': 'boolean',
        'consultation_payload': 'text',
    },
    'tool_bug_reports': {
        'id': 'integer',
        'task_id': 'text',
        'session_id': 'integer',
        'tool_name': 'text',
        'trying_to': 'text',
        'expected': 'text',
        'actual': 'text',
        'created_at': 'timestamp without time zone',
        'viewed_at': 'timestamp without time zone',
    },
    'transition_results': {
        'id': 'integer',
        'task_id': 'text',
        'transition': 'text',
        'outcome': 'text',
        'vote_summary': 'json',
        'total_prompt_tokens': 'integer',
        'total_completion_tokens': 'integer',
        'created_at': 'timestamp without time zone',
    },
    'transition_votes': {
        'id': 'integer',
        'task_id': 'text',
        'transition': 'text',
        'stage': 'text',
        'verdict': 'text',
        'confidence': 'integer',
        'justification': 'text',
        'raw_response': 'json',
        'prompt_tokens': 'integer',
        'completion_tokens': 'integer',
        'model': 'text',
        'budget_id': 'integer',
        'created_at': 'timestamp without time zone',
    },
}

# ---------------------------------------------------------------------------
# Type normalisation — information_schema returns canonical names, but keep
# a small alias table for robustness across PG minor versions.
# ---------------------------------------------------------------------------
_ALIASES = {
    'int4':   'integer',
    'int8':   'bigint',
    'int2':   'smallint',
    'float4': 'real',
    'float8': 'double precision',
    'bool':   'boolean',
    'varchar': 'character varying',
    'timestamp without time zone': 'timestamp without time zone',
    'timestamp with time zone':    'timestamp with time zone',
}


def _norm(t: str) -> str:
    return _ALIASES.get(t.lower(), t.lower())


# ---------------------------------------------------------------------------
# Migration entry points
# ---------------------------------------------------------------------------

def up(conn) -> None:
    if not conn.is_postgres:
        print("[0067] SQLite detected — baseline verification skipped.")
        return

    # Read the current schema from information_schema
    conn.execute("""
        SELECT table_name, column_name, data_type
        FROM information_schema.columns
        WHERE table_schema = 'public'
          AND table_name != 'schema_migrations'
        ORDER BY table_name, ordinal_position
    """)
    rows = conn.fetchall()

    actual: dict = {}
    for row in rows:
        actual.setdefault(row[0], {})[row[1]] = row[2]

    errors: list = []
    for table, expected_cols in sorted(_EXPECTED_SCHEMA.items()):
        if table not in actual:
            errors.append(f"  MISSING TABLE: {table}")
            continue
        for col, expected_type in expected_cols.items():
            if col not in actual[table]:
                errors.append(f"  MISSING COLUMN: {table}.{col}")
            else:
                got = _norm(actual[table][col])
                exp = _norm(expected_type)
                if got != exp:
                    errors.append(
                        f"  TYPE MISMATCH: {table}.{col} "
                        f"expected={exp!r} got={got!r}"
                    )

    if errors:
        raise RuntimeError(
            f"[0067] PostgreSQL baseline verification failed "
            f"({len(errors)} issue(s)):\n" + "\n".join(errors)
        )

    n_tables = len(_EXPECTED_SCHEMA)
    n_cols = sum(len(c) for c in _EXPECTED_SCHEMA.values())
    print(f"[0067] PostgreSQL baseline verified — "
          f"{n_tables} tables, {n_cols} columns match.")


def down(conn) -> None:
    # Verification-only: nothing to undo.
    pass
