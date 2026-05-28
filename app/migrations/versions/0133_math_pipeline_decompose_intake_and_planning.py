"""Replace monolithic intake_agent and planning_agent in the Math/Proof template
with the same decomposed nodes used in Software Development.

Intake: intake_scope → intake_conflict → intake_feasibility → intake_gate
Planning: planning_survey_node → multiplier_node (propose) → multiplier_node (review)
          → pitfall_node → consolidation_node → planning_gate_node
          → json_schema_gate → planning_correction_stage

All planning nodes already have _is_proof detection; math-specific system prompts are
embedded in the proposal/review agent configs.
"""

import json

description = "Math pipeline: decompose intake_agent and planning_agent into SW-Dev-style nodes"

# ── Configs ────────────────────────────────────────────────────────────────

_INTAKE_FEASIBILITY_PROMPT = """\
You are an expert analyst performing feasibility analysis on a proposed mathematical task
that will be executed by the Maestro agentic platform.

## Maestro Platform Capabilities

Maestro is an agentic workflow platform specialising in formal mathematics:
- LLM agents that can reason, write Lean4 proofs, and use SymPy for symbolic computation.
- Access to Mathlib (the Lean4 mathematical library) and arXiv for literature search.
- Docker-isolated sandboxes running Lean4 + SymPy for formal verification.
- Human review gates for open-ended or high-stakes problems.

## Feasibility Criteria

Assess the following:
1. PROBLEM CLARITY — Is the mathematical statement well-posed and unambiguous?
2. SCOPE — Is this a tractable problem (not an open Millennium Prize-level question)?
3. FORMALIZATION — Can the problem be expressed in Lean4/Mathlib notation?
4. RESOURCE FIT — Is the depth appropriate for the token/turn budget?
5. DUPLICATION — Does this overlap with an existing task in the project?

## Output

Respond with: FEASIBLE, INFEASIBLE, or NEEDS_CLARIFICATION, followed by a brief rationale.
If INFEASIBLE, explain why (e.g. unsolved open problem, too vague, trivial duplicate).
"""

_INTAKE_CONFLICT_PROMPT = """\
You are a project coordinator performing conflict detection on a proposed mathematical task.

You will receive:
1. The proposed task description, title, and scope analysis.
2. A list of all current non-completed tasks in the project.

Your job is to detect:
- Duplicate proofs: tasks working on the same theorem or lemma.
- Dependency conflicts: tasks that depend on results not yet proven by other tasks.
- Scope overlaps: tasks whose proof strategies would cover the same mathematical ground.

Output a structured conflict report. If no conflicts, state "No conflicts detected."
"""

_PLAN_PROPOSE_AGENTS = [
    {
        "name": "induction_specialist",
        "max_turns": 30,
        "system_prompt": (
            "Your primary concern is finding a proof by induction or recursion. "
            "Analyse the structure of the mathematical objects involved. "
            "Identify the base case, inductive step, and the precise induction hypothesis. "
            "Check whether strong induction, transfinite induction, or structural induction is needed. "
            "Verify that the required Mathlib lemmas for the inductive step exist. "
            "In your design_rationale, explain why induction is the right approach and how each step closes. "
            "Call submit_work with your full proof design JSON."
        ),
    },
    {
        "name": "algebraic_analyst",
        "max_turns": 30,
        "system_prompt": (
            "Your primary concern is algebraic and combinatorial structure. "
            "Look for direct constructions, bijections, generating functions, or algebraic identities "
            "that reduce the problem to known results. "
            "Check arXiv and Mathlib for relevant theorems that can be composed directly. "
            "Prefer short proofs built from existing library lemmas over long hand-rolled arguments. "
            "In your design_rationale, explain the algebraic strategy and cite the key Mathlib entries. "
            "Call submit_work with your full proof design JSON."
        ),
    },
    {
        "name": "analysis_topologist",
        "max_turns": 30,
        "system_prompt": (
            "Your primary concern is analytic and topological arguments. "
            "Consider contradiction, compactness, continuity arguments, or limit-based reasoning. "
            "Identify whether the problem lives in a metric space, topological space, or measure space "
            "and which Mathlib topology/analysis libraries apply. "
            "In your design_rationale, explain the analytic strategy, the critical lemmas, "
            "and why this approach closes the proof obligation. "
            "Call submit_work with your full proof design JSON."
        ),
    },
]

_PLAN_REVIEW_AGENTS = [
    {
        "name": "soundness",
        "tools": ["get_document", "list_documents", "search_mathlib", "submit_work"],
        "max_turns": 12,
        "system_prompt": (
            "You are a mathematical soundness reviewer. "
            "Read the winning proof design and check: "
            "Does the proof strategy actually prove what is claimed? "
            "Are all cases covered? Is the induction hypothesis strong enough? "
            "Are any steps circular or hand-wavy? "
            "Vote ACCEPTED if sound, REJECTED if there is a gap. "
            "Call submit_work with signal='ACCEPTED'|'REJECTED', "
            "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
        ),
    },
    {
        "name": "mathlib_coverage",
        "tools": ["search_mathlib", "list_mathlib_topics", "get_document", "submit_work"],
        "max_turns": 12,
        "system_prompt": (
            "You are a Mathlib coverage reviewer. "
            "Read the winning proof design and verify that every cited Mathlib lemma "
            "actually exists and has the correct type signature. "
            "Flag invented or incorrectly named lemmas. "
            "Check whether the proposed Lean4 import structure is valid. "
            "Vote ACCEPTED if all library claims are correct, REJECTED if lemmas are fictional. "
            "Call submit_work with signal='ACCEPTED'|'REJECTED', "
            "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
        ),
    },
    {
        "name": "mechanization",
        "tools": ["get_document", "list_documents", "run_lean4", "submit_work"],
        "max_turns": 12,
        "system_prompt": (
            "You are a Lean4 mechanization reviewer. "
            "Read the winning proof design and assess whether it can be formally mechanized: "
            "Is every proof step expressible in Lean4 tactic mode? "
            "Are there sorry-free paths for all sub-goals? "
            "Does the design specify lake build verification? "
            "Vote ACCEPTED if mechanizable, REJECTED if structural gaps prevent Lean4 encoding. "
            "Call submit_work with signal='ACCEPTED'|'REJECTED', "
            "payload={'verdict': ..., 'confidence': 0-100, 'justification': '...'}."
        ),
    },
]

_JSON_SCHEMA_GATE_CFG = {
    "source": "planning_result",
    "on_fail": "fail",
    "on_pass": "pass",
    "output_key": "gate_result",
    "max_retries": 2,
    "required_fields": [
        {"key": "design_rationale", "hard_fail": True, "validator": "non_empty_string"},
    ],
    "retry_condition": "retry",
}


def up(conn):
    # ── 1. Resolve template and legacy stage IDs ───────────────────────────
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Mathematics / Proof Exploration'"
    ).fetchone()
    if not row:
        print("[0133] Math template not found — skipping.")
        return
    tmpl_id = row[0]

    old_stages = conn.execute(
        "SELECT id, stage_key FROM pipeline_stages WHERE template_id = :t "
        "AND stage_key IN ('idea', 'planning')",
        {"t": tmpl_id},
    ).fetchall()
    old_map = {r[1]: r[0] for r in old_stages}   # stage_key → id

    if not old_map:
        print("[0133] Legacy idea/planning stages already removed — skipping.")
        return

    old_ids = list(old_map.values())

    # ── 2. Delete transitions touching the legacy stages ───────────────────
    for sid in old_ids:
        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
            {"t": tmpl_id, "s": sid},
        )

    # ── 3. Delete the legacy stages ───────────────────────────────────────
    for sid in old_ids:
        conn.execute(
            "DELETE FROM pipeline_stages WHERE id = :s", {"s": sid}
        )

    # ── 4. Shift existing stage positions up by 11 (4 intake + 7 planning) ─
    conn.execute(
        "UPDATE pipeline_stages SET position = position + 11 "
        "WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # ── 5. Insert new intake stages ────────────────────────────────────────
    def ins(stage_key, label, agent_type, position, config):
        conn.execute(
            "INSERT INTO pipeline_stages "
            "(template_id, stage_key, label, agent_type, position, config) "
            "VALUES (:t, :sk, :lbl, :at, :pos, CAST(:cfg AS jsonb))",
            {
                "t": tmpl_id, "sk": stage_key, "lbl": label,
                "at": agent_type, "pos": position,
                "cfg": json.dumps(config),
            },
        )
        return conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = :sk",
            {"t": tmpl_id, "sk": stage_key},
        ).fetchone()[0]

    s_scope      = ins("intake_scope",      "Intake: Scope",       "intake_scope",      0, {})
    s_conflict   = ins("intake_conflict",   "Intake: Conflict",    "intake_conflict",   1,
                       {"system_prompt": _INTAKE_CONFLICT_PROMPT})
    s_feasib     = ins("intake_feasibility","Intake: Feasibility", "intake_feasibility",2,
                       {"system_prompt": _INTAKE_FEASIBILITY_PROMPT})
    s_igate      = ins("intake_gate",       "Intake: Gate",        "intake_gate",       3, {})

    # ── 6. Insert new planning stages ─────────────────────────────────────
    s_survey     = ins("planning_survey",    "Planning: Survey",      "planning_survey_node",    4, {})
    s_propose    = ins("planning_propose",   "Planning: Propose",     "multiplier_node",         5, {
        "n": 3,
        "collapser_mode": "judge_select",
        "agent_max_turns": 30,
        "judge_max_turns": 15,
        "output_key": "winning_design",
        "agents": _PLAN_PROPOSE_AGENTS,
        "judge_system_prompt": (
            "You are a senior mathematician reviewing three proof design proposals. "
            "Pick the most promising approach based on: mathematical soundness, "
            "Lean4 mechanizability, and elegance. "
            "Return the index (0, 1, or 2) of the winning design and a brief justification. "
            "Call submit_work with your verdict."
        ),
    })
    s_review     = ins("planning_review",    "Planning: Review",      "multiplier_node",         6, {
        "n": 3,
        "collapser_mode": "vote_tally",
        "tally_strategy": "majority",
        "on_tie": "reject",
        "agent_max_turns": 12,
        "output_key": "review_result",
        "agents": _PLAN_REVIEW_AGENTS,
    })
    s_pitfalls   = ins("planning_pitfalls",  "Planning: Pitfalls",    "pitfall_node",            7, {
        "system_prompt": "You are a software quality analyst. Use submit_work to output pitfalls when ready.",
        "system_prompt_proof": "You are a formal-proof quality reviewer. Use submit_work to output pitfalls.",
    })
    s_consol     = ins("planning_consolidate","Planning: Consolidate", "consolidation_node",      8, {
        "system_prompt": "You are a software architect. Use submit_work to output the final design.",
        "system_prompt_proof": "You are a formal proof specialist. Use submit_work to output the final proof design.",
    })
    s_pgate      = ins("planning_gate",      "Planning: Gate",        "planning_gate_node",      9, {})
    s_schema     = ins("json_schema_gate",   "Schema Gate",           "json_schema_gate",       10,
                       _JSON_SCHEMA_GATE_CFG)
    s_correction = ins("planning_correction","Planning: Correction",  "planning_correction_stage",11,
                       {"max_turns": 20})

    # ── 7. Fetch first core stage (LITERATURE_SURVEY) for wiring ──────────
    lit_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'LITERATURE_SURVEY'",
        {"t": tmpl_id},
    ).fetchone()[0]

    # ── 8. Insert new transitions ──────────────────────────────────────────
    edges = [
        # intake loop-back on fail → restart scope
        (s_scope,    s_scope,      "fail"),
        (s_scope,    s_conflict,   "pass"),
        (s_conflict, s_scope,      "fail"),
        (s_conflict, s_feasib,     "pass"),
        (s_feasib,   s_scope,      "fail"),
        (s_feasib,   s_igate,      "pass"),
        (s_igate,    s_scope,      "fail"),
        (s_igate,    s_survey,     "pass"),
        # planning chain
        (s_survey,   s_propose,    "pass"),
        (s_propose,  s_review,     "pass"),
        (s_review,   s_propose,    "fail"),
        (s_review,   s_propose,    "reject"),
        (s_review,   s_pitfalls,   "pass"),
        (s_pitfalls, s_consol,     "pass"),
        (s_consol,   s_pgate,      "pass"),
        (s_pgate,    s_correction, "fail"),
        (s_pgate,    s_schema,     "pass"),
        (s_schema,   lit_id,       "pass"),
        (s_schema,   s_correction, "retry"),
        (s_correction, s_pgate,    "fail"),
        (s_correction, s_schema,   "pass"),
    ]
    for (fr, to, cond) in edges:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )

    print(f"[0133] Inserted 4 intake + 8 planning stages; {len(edges)} transitions.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Mathematics / Proof Exploration'"
    ).fetchone()
    if not row:
        return
    tmpl_id = row[0]

    new_keys = [
        "intake_scope", "intake_conflict", "intake_feasibility", "intake_gate",
        "planning_survey", "planning_propose", "planning_review", "planning_pitfalls",
        "planning_consolidate", "planning_gate", "json_schema_gate", "planning_correction",
    ]
    rows = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = ANY(:keys)",
        {"t": tmpl_id, "keys": new_keys},
    ).fetchall()
    for r in rows:
        conn.execute(
            "DELETE FROM pipeline_transitions "
            "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
            {"t": tmpl_id, "s": r[0]},
        )
    conn.execute(
        "DELETE FROM pipeline_stages WHERE template_id = :t AND stage_key = ANY(:keys)",
        {"t": tmpl_id, "keys": new_keys},
    )
    # Shift remaining stages back down by 11
    conn.execute(
        "UPDATE pipeline_stages SET position = position - 11 WHERE template_id = :t",
        {"t": tmpl_id},
    )
    # Re-insert legacy stages
    idea_id = conn.execute(
        "INSERT INTO pipeline_stages (template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'idea', 'Idea', 'intake_agent', 0, CAST('{}' AS jsonb)) RETURNING id",
        {"t": tmpl_id},
    ).fetchone()[0]
    plan_id = conn.execute(
        "INSERT INTO pipeline_stages (template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'planning', 'Planning', 'planning_agent', 1, CAST('{}' AS jsonb)) RETURNING id",
        {"t": tmpl_id},
    ).fetchone()[0]
    lit_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'LITERATURE_SURVEY'",
        {"t": tmpl_id},
    ).fetchone()[0]
    for (fr, to, cond) in [
        (idea_id, plan_id, "pass"),
        (plan_id, lit_id,  "pass"),
        (plan_id, idea_id, "fail"),
    ]:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )
