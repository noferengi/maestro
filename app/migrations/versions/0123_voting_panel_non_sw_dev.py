description = "Apply voting_panel to FORMAL_VERIFICATION (Math), root_cause (Bug Triage), fact_check (Research Report)"

import json as _json

_MATH_FORMAL_VERIFICATION_CONFIG = {
    "reviewers": [
        {
            "name": "symbolic",
            "system_prompt": (
                "You are a symbolic verifier reviewing a mathematical proof. "
                "Check that each proof step follows validly from axioms or prior steps using accepted inference rules. "
                "Flag any gap, unproven leap, or implicit assumption. "
                "Call submit_work with ACCEPTED if the proof is symbolically sound, REJECTED otherwise."
            ),
            "max_turns": 12,
        },
        {
            "name": "logical",
            "system_prompt": (
                "You are a logical completeness checker reviewing a mathematical proof. "
                "Verify no proof steps are skipped, unjustified, or rely on unstated lemmas. "
                "Check that the conclusion follows from the premises as stated. "
                "Call submit_work with ACCEPTED if the proof is logically complete, REJECTED otherwise."
            ),
            "max_turns": 12,
        },
        {
            "name": "intuition",
            "system_prompt": (
                "You are a mathematical intuition challenger reviewing a proof. "
                "Look for hidden assumptions, edge cases the proof misses, or cases where the result seems wrong. "
                "Attempt to construct a counterexample. "
                "Call submit_work with ACCEPTED if no counterexample can be found, REJECTED if one exists."
            ),
            "max_turns": 12,
        },
    ],
    "tally_strategy": "majority",
    "output_key": "formal_verification_result",
}

_BUG_ROOT_CAUSE_CONFIG = {
    "reviewers": [
        {
            "name": "runtime",
            "system_prompt": (
                "You are a runtime execution analyst reviewing a bug report and its proposed root cause. "
                "Trace the probable execution path and runtime state at the point of failure. "
                "Assess whether the proposed root cause correctly explains the observed symptoms. "
                "Call submit_work with ACCEPTED if the root cause is correct, REJECTED otherwise."
            ),
            "max_turns": 10,
        },
        {
            "name": "logic",
            "system_prompt": (
                "You are a logic and algorithm reviewer examining a bug and its proposed root cause. "
                "Look for off-by-one errors, race conditions, incorrect state transitions, or flawed assumptions. "
                "Assess whether the proposed root cause identifies the true logical error. "
                "Call submit_work with ACCEPTED if correct, REJECTED otherwise."
            ),
            "max_turns": 10,
        },
        {
            "name": "regression",
            "system_prompt": (
                "You are a regression analyst reviewing a bug report. "
                "Investigate whether this bug is a regression introduced by a recent change. "
                "Assess the proposed root cause and fix surface area: is it minimal and correct? "
                "Call submit_work with ACCEPTED if the root cause and scope are accurate, REJECTED otherwise."
            ),
            "max_turns": 10,
        },
    ],
    "tally_strategy": "majority",
    "output_key": "root_cause_analysis",
}

_RESEARCH_FACT_CHECK_CONFIG = {
    "reviewers": [
        {
            "name": "source_validator",
            "system_prompt": (
                "You are a source validator reviewing a research report draft. "
                "Verify that all cited sources exist, are credible, and actually support the claims made. "
                "Flag any missing citations or misrepresented sources. "
                "Call submit_work with ACCEPTED if sources are valid and complete, REJECTED otherwise."
            ),
            "max_turns": 10,
        },
        {
            "name": "claim_strength",
            "system_prompt": (
                "You are a claim strength evaluator reviewing a research report draft. "
                "Assess whether the strength of each claim is justified by the evidence presented. "
                "Flag overstated conclusions, unsupported generalizations, or hedged claims stated as certainties. "
                "Call submit_work with ACCEPTED if claims are appropriately supported, REJECTED otherwise."
            ),
            "max_turns": 10,
        },
        {
            "name": "bias_detector",
            "system_prompt": (
                "You are a bias detection reviewer examining a research report draft. "
                "Look for confirmation bias, missing contrary evidence, selective citation, or framing effects. "
                "Call submit_work with ACCEPTED if the report is balanced and fair, REJECTED if significant bias is present."
            ),
            "max_turns": 10,
        },
    ],
    "tally_strategy": "majority",
    "output_key": "fact_check_result",
}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage_id(conn, template_id, stage_key):
    row = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :sk LIMIT 1",
        {"tid": template_id, "sk": stage_key},
    ).fetchone()
    return row["id"] if row else None


def _add_transition(conn, template_id, from_stage_id, to_stage_id, condition):
    existing = conn.execute(
        """SELECT id FROM pipeline_transitions
           WHERE template_id = :tid AND from_stage_id = :fid AND condition = :c LIMIT 1""",
        {"tid": template_id, "fid": from_stage_id, "c": condition},
    ).fetchone()
    if not existing:
        conn.execute(
            """INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority)
               VALUES (:tid, :fid, :toid, :c, 0)""",
            {"tid": template_id, "fid": from_stage_id, "toid": to_stage_id, "c": condition},
        )


def up(conn):
    # --- Math / Proof Exploration: FORMAL_VERIFICATION → voting_panel ---
    math_id = _get_template_id(conn, "Mathematics / Proof Exploration")
    if math_id:
        fv_id = _get_stage_id(conn, math_id, "FORMAL_VERIFICATION")
        if fv_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'voting_panel', config = :cfg WHERE id = :id",
                {"cfg": _json.dumps(_MATH_FORMAL_VERIFICATION_CONFIG), "id": fv_id},
            )
            # pass → WRITEUP and fail → PROOF_ATTEMPT transitions already exist; no changes needed.

    # --- Bug Triage: root_cause → voting_panel ---
    bug_id = _get_template_id(conn, "Bug Triage")
    if bug_id:
        rc_id = _get_stage_id(conn, bug_id, "root_cause")
        if rc_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'voting_panel', config = :cfg WHERE id = :id",
                {"cfg": _json.dumps(_BUG_ROOT_CAUSE_CONFIG), "id": rc_id},
            )
            # Add fail → reproduce transition (go back to reproduction on rejected root cause)
            reproduce_id = _get_stage_id(conn, bug_id, "reproduce")
            if reproduce_id:
                _add_transition(conn, bug_id, rc_id, reproduce_id, "fail")

    # --- Research Report: fact_check → voting_panel ---
    research_id = _get_template_id(conn, "Research Report")
    if research_id:
        fc_id = _get_stage_id(conn, research_id, "fact_check")
        if fc_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'voting_panel', config = :cfg WHERE id = :id",
                {"cfg": _json.dumps(_RESEARCH_FACT_CHECK_CONFIG), "id": fc_id},
            )
            # Add fail → draft transition (revise the draft when fact check rejects)
            draft_id = _get_stage_id(conn, research_id, "draft")
            if draft_id:
                _add_transition(conn, research_id, fc_id, draft_id, "fail")


def down(conn):
    math_id = _get_template_id(conn, "Mathematics / Proof Exploration")
    if math_id:
        fv_id = _get_stage_id(conn, math_id, "FORMAL_VERIFICATION")
        if fv_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'generic_stage', config = NULL WHERE id = :id",
                {"id": fv_id},
            )

    bug_id = _get_template_id(conn, "Bug Triage")
    if bug_id:
        rc_id = _get_stage_id(conn, bug_id, "root_cause")
        if rc_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'custom_agent', config = NULL WHERE id = :id",
                {"id": rc_id},
            )
            reproduce_id = _get_stage_id(conn, bug_id, "reproduce")
            if reproduce_id:
                conn.execute(
                    "DELETE FROM pipeline_transitions WHERE from_stage_id = :fid AND to_stage_id = :tid AND condition = 'fail'",
                    {"fid": rc_id, "tid": reproduce_id},
                )

    research_id = _get_template_id(conn, "Research Report")
    if research_id:
        fc_id = _get_stage_id(conn, research_id, "fact_check")
        if fc_id:
            conn.execute(
                "UPDATE pipeline_stages SET agent_type = 'custom_agent', config = NULL WHERE id = :id",
                {"id": fc_id},
            )
            draft_id = _get_stage_id(conn, research_id, "draft")
            if draft_id:
                conn.execute(
                    "DELETE FROM pipeline_transitions WHERE from_stage_id = :fid AND to_stage_id = :tid AND condition = 'fail'",
                    {"fid": fc_id, "tid": draft_id},
                )
