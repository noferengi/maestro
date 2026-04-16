# Database Schema — data/kanban.db

SQLite database. All tables listed with columns, types, nullability, and defaults.

---

## tasks
Primary task/card table. `type` is the pipeline stage.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | TEXT | yes | — |
| title | TEXT | no | — |
| type | TEXT | no | — |
| description | TEXT | yes | — |
| owner | TEXT | yes | `'user'` |
| tags | JSON | yes | — |
| content | JSON | yes | — |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| history | JSON | yes | — |
| position | INTEGER | yes | `0` |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |
| updated_at | DATETIME | yes | CURRENT_TIMESTAMP |
| prerequisites | JSON | yes | — |
| project | TEXT | yes | `'TheMaestro'` |
| parent_task_id | TEXT | yes | — |
| subdivision_generation | INTEGER | no | `0` |
| is_big_idea | INTEGER | no | `0` |
| interface_contracts | TEXT | yes | — |
| review_notes | TEXT | yes | — |
| demotion_count | INTEGER | no | `0` |
| demotion_history | JSON | yes | — |
| map_x | REAL | yes | — |
| map_y | REAL | yes | — |
| is_active | INTEGER | no | `1` |

Valid `type` values (pipeline stages): `idea`, `planning`, `indev`, `conceptual_review`, `optimization`, `security`, `full_review`, `completed`, `cancelled`, `subdividing`, `accepted`, `architecture`

---

## projects

| Column | Type | Nullable | Default |
|---|---|---|---|
| name | TEXT | yes | — |
| path | TEXT | yes | — |
| description | TEXT | yes | — |
| created_at | TEXT | no | `datetime('now')` |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |

---

## llms

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| address | TEXT | no | `'localhost'` |
| port | INTEGER | no | `8008` |
| model | TEXT | no | `''` |
| settings | JSON | yes | — |
| parallel_sessions | INTEGER | no | `1` |
| max_context | INTEGER | no | `4096` |
| notes | TEXT | no | `''` |
| cost_per_million_prompt_tokens | REAL | yes | `0.0` |
| cost_per_million_completion_tokens | REAL | yes | `0.0` |
| compute_node_id | INTEGER | yes | — |

---

## budgets

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| name | TEXT | no | — |
| settings | JSON | yes | — |
| dollar_amount | REAL | yes | `-1` |

`dollar_amount = -1` means infinite/unlimited.

---

## compute_nodes

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| name | TEXT | no | — |
| description | TEXT | yes | — |
| max_parallel_sessions | INTEGER | no | `1` |
| max_loaded_models | INTEGER | no | `1` |

---

## budget_entries
One row per LLM API call. `task_id` is NULL for project-level prewarm calls (file summaries).

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| task_id | TEXT | yes | — |
| prompt_cost | INTEGER | no | `0` |
| generation_cost | INTEGER | no | `0` |
| tool_calls | INTEGER | no | `0` |
| prompt_data | TEXT | yes | — |
| response_data | TEXT | yes | — |
| created_at | DATETIME | no | CURRENT_TIMESTAMP |

---

## expenses
Aggregated cost record per call (microcents).

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| budget_entry_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| llm_id | INTEGER | yes | — |
| remote_call_id | TEXT | yes | — |
| task_id | TEXT | yes | — |
| prompt_tokens | INTEGER | no | `0` |
| completion_tokens | INTEGER | no | `0` |
| total_tokens | INTEGER | no | `0` |
| prompt_cost_microcents | INTEGER | no | `0` |
| completion_cost_microcents | INTEGER | no | `0` |
| total_cost_microcents | INTEGER | no | `0` |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |

---

## transition_votes
Individual stage votes within an intake pipeline run.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| task_id | TEXT | no | — |
| transition | TEXT | no | — |
| stage | TEXT | no | — |
| verdict | TEXT | no | — |
| confidence | INTEGER | no | — |
| justification | TEXT | yes | — |
| raw_response | JSON | yes | — |
| prompt_tokens | INTEGER | yes | — |
| completion_tokens | INTEGER | yes | — |
| model | TEXT | yes | — |
| budget_id | INTEGER | yes | — |
| created_at | DATETIME | no | CURRENT_TIMESTAMP |

`transition` e.g. `idea_to_planning`. `stage` e.g. `scope_analysis`, `static_analysis`, `conflict_detection`, `feasibility_analysis`.

---

## transition_results
Aggregate result for a full transition run (all stages combined).

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| task_id | TEXT | no | — |
| transition | TEXT | no | — |
| outcome | TEXT | no | — |
| vote_summary | JSON | yes | — |
| total_prompt_tokens | INTEGER | yes | — |
| total_completion_tokens | INTEGER | yes | — |
| created_at | DATETIME | no | CURRENT_TIMESTAMP |

`outcome` values: `passed`, `rejected`.

---

## planning_results

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| file_manifest | TEXT | yes | — |
| dependency_graph | TEXT | yes | — |
| interface_contracts | TEXT | yes | — |
| test_strategy | TEXT | yes | — |
| implementation_steps | TEXT | yes | — |
| mermaid_diagrams | TEXT | yes | — |
| pitfalls_identified | TEXT | yes | — |
| review_votes | TEXT | yes | — |
| codebase_survey | TEXT | yes | — |
| best_of_n_designs | TEXT | yes | — |
| selected_design_index | INTEGER | yes | — |
| selection_justification | TEXT | yes | — |
| confidence | INTEGER | yes | — |
| prompt_tokens | INTEGER | yes | — |
| completion_tokens | INTEGER | yes | — |
| status | VARCHAR | no | — |
| created_at | DATETIME | yes | — |

---

## component_results
Per-step results from the dev orchestrator (MaestroLoop implementation batches).

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| component_name | VARCHAR | no | — |
| step_order | INTEGER | no | — |
| batch_number | INTEGER | no | — |
| status | VARCHAR | no | — |
| files_changed | TEXT | yes | — |
| tests_passed | INTEGER | yes | — |
| turns_used | INTEGER | yes | — |
| error_detail | TEXT | yes | — |
| prompt_tokens | INTEGER | yes | — |
| completion_tokens | INTEGER | yes | — |
| created_at | DATETIME | yes | — |
| completed_at | DATETIME | yes | — |

---

## optimization_results

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| baseline_report | TEXT | yes | — |
| proposals | TEXT | yes | — |
| judge_scores | TEXT | yes | — |
| winning_proposal_index | INTEGER | yes | — |
| winning_score | INTEGER | yes | — |
| post_report | TEXT | yes | — |
| improvement_summary | TEXT | yes | — |
| outcome | VARCHAR | no | — |
| total_prompt_tokens | INTEGER | yes | — |
| total_completion_tokens | INTEGER | yes | — |
| created_at | DATETIME | yes | — |

---

## optimization_benchmarks

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| task_id | TEXT | no | — |
| parent_task_id | TEXT | no | — |
| benchmark_type | TEXT | no | — |
| metrics | TEXT | no | — |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |

---

## security_review_results

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| reviewer_type | VARCHAR | no | — |
| owasp_findings | TEXT | yes | — |
| secrets_detected | TEXT | yes | — |
| dependency_vulnerabilities | TEXT | yes | — |
| data_flow_map | TEXT | yes | — |
| compliance_findings | TEXT | yes | — |
| optimization_regressions | TEXT | yes | — |
| verdict | VARCHAR | no | — |
| confidence | INTEGER | no | — |
| justification | TEXT | yes | — |
| critical_count | INTEGER | yes | — |
| high_count | INTEGER | yes | — |
| raw_response | TEXT | yes | — |
| prompt_tokens | INTEGER | yes | — |
| completion_tokens | INTEGER | yes | — |
| model | VARCHAR | yes | — |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| created_at | DATETIME | yes | — |

---

## full_review_results

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| reviewer_type | VARCHAR | no | — |
| test_results | TEXT | yes | — |
| quality_findings | TEXT | yes | — |
| requirements_mapping | TEXT | yes | — |
| integration_checks | TEXT | yes | — |
| verdict | VARCHAR | no | — |
| confidence | INTEGER | no | — |
| justification | TEXT | yes | — |
| raw_response | TEXT | yes | — |
| prompt_tokens | INTEGER | yes | — |
| completion_tokens | INTEGER | yes | — |
| model | VARCHAR | yes | — |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| created_at | DATETIME | yes | — |

---

## merge_records

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | no | — |
| task_id | VARCHAR | no | — |
| branch_name | VARCHAR | no | — |
| merge_commit_sha | VARCHAR | yes | — |
| status | VARCHAR | no | — |
| test_output | TEXT | yes | — |
| error_detail | TEXT | yes | — |
| security_review_ids | TEXT | yes | — |
| full_review_ids | TEXT | yes | — |
| total_pipeline_tokens | INTEGER | yes | — |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| created_at | DATETIME | yes | — |

---

## subdivision_records
Audit trail of subdivision attempts for Big Idea tasks.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| parent_task_id | TEXT | no | — |
| attempt_number | INTEGER | no | `1` |
| generation | INTEGER | no | `1` |
| child_task_ids | JSON | no | — |
| rejection_context | JSON | yes | — |
| agent_vote | JSON | yes | — |
| prompt_tokens | INTEGER | yes | `0` |
| completion_tokens | INTEGER | yes | `0` |
| status | TEXT | no | `'active'` |
| created_at | DATETIME | no | CURRENT_TIMESTAMP |
| interface_contracts | TEXT | yes | — |

`status` values: `active`, `superseded`.

---

## file_summaries
Cache of LLM-generated file summaries, keyed by SHA1 hash.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| sha1_hash | TEXT | no | — |
| file_size_bytes | INTEGER | no | — |
| file_path | TEXT | no | — |
| summary | TEXT | no | — |
| static_analysis_json | TEXT | yes | — |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |
| short_summary | TEXT | yes | — |

---

## file_summary_jobs
Queue for pending/in-progress file summary generation.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| sha1_hash | TEXT | no | — |
| file_size_bytes | INTEGER | no | — |
| file_path | TEXT | no | — |
| file_content | TEXT | no | — |
| static_analysis_json | TEXT | yes | — |
| status | TEXT | no | `'pending'` |
| priority | REAL | no | `-1.0` |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| task_id | TEXT | yes | — |
| prompt_tokens | INTEGER | yes | `0` |
| completion_tokens | INTEGER | yes | `0` |
| error_message | TEXT | yes | — |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |
| completed_at | DATETIME | yes | — |
| previous_summary | TEXT | yes | — |

`status` values: `pending`, `in_progress`, `completed`, `failed`.

---

## research_jobs

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| task_id | TEXT | no | — |
| parent_job_id | INTEGER | yes | — |
| question | TEXT | no | — |
| context | TEXT | yes | — |
| status | TEXT | no | `'pending'` |
| priority | REAL | no | `0.0` |
| depth | INTEGER | no | `0` |
| verdict | TEXT | yes | — |
| findings | TEXT | yes | — |
| lives_used | INTEGER | yes | `0` |
| prompt_tokens | INTEGER | yes | `0` |
| completion_tokens | INTEGER | yes | `0` |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| created_at | DATETIME | yes | CURRENT_TIMESTAMP |
| completed_at | DATETIME | yes | — |

---

## inbox_messages

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | TEXT | yes | — |
| subject | TEXT | no | — |
| source_type | TEXT | no | `'intake_result'` |
| task_id | TEXT | yes | — |
| task_title | TEXT | yes | — |
| outcome | TEXT | yes | — |
| data_json | TEXT | yes | — |
| read | INTEGER | no | `0` |
| created_at | TEXT | no | `datetime('now')` |

---

## search_cache

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes | — |
| query | TEXT | no | — |
| result_json | TEXT | no | — |
| provider | TEXT | no | `'brave'` |
| created_at | DATETIME | no | `datetime('now')` |

---

## schema_migrations

| Column | Type | Nullable | Default |
|---|---|---|---|
| migration_id | TEXT | yes | — |
| applied_at | DATETIME | no | — |

---

## sqlite_sequence
Internal SQLite auto-increment tracking table. Do not modify directly.

---

## agent_sessions

One row per agent invocation. Written by the scheduler `_run_*` functions and the
`@_pipeline_session` decorator in `main.py`. Stays open (no `ended_at`) while the
agent is running; closed with exit details on completion or error.

| Column | Type | Nullable | Default |
|---|---|---|---|
| id | INTEGER | yes (PK AUTOINCREMENT) | — |
| task_id | TEXT | no | — |
| agent_type | TEXT | no | — |
| started_at | TEXT | no | — |
| ended_at | TEXT | yes | — |
| turn_count | INTEGER | yes | — |
| max_turns | INTEGER | yes | — |
| exit_reason | TEXT | yes | — |
| exit_summary | TEXT | yes | — |
| scheduler_reason | TEXT | no | `'scheduler'` |
| llm_id | INTEGER | yes | — |
| budget_id | INTEGER | yes | — |
| prompt_tokens | INTEGER | no | `0` |
| completion_tokens | INTEGER | no | `0` |

**`agent_type` values:** `intake`, `planning`, `maestro_loop`, `dev_orchestrator`,
`conceptual_review`, `optimization`, `security`, `full_review`, `pip_preflight`,
`pip_research`, `pip_resolution`, `subdivision`, `arch_gen`

**`exit_reason` values:** `completed`, `max_turns`, `stalled`, `error`, `shutdown`,
`passed`, `rejected`, `subdivide`, `pip_blocked`

**`scheduler_reason` values:** `scheduler`, `user_triggered`

**Index:** `(task_id, started_at)` — primary query pattern is sessions for a given task
ordered by time.

**API:** `GET /api/tasks/{task_id}/agent-sessions` — returns all rows oldest-first with
a computed `duration_seconds` field (null if still running).
