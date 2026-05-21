description = "Replace Mathematics / Proof Exploration template with 9-stage GAP-3 design"

import json as _json

_TEMPLATE_NAME = "Mathematics / Proof Exploration"

# ---------------------------------------------------------------------------
# New 11-stage template (idea + 9 GAP-3 stages + accepted)
# ---------------------------------------------------------------------------

_SYSTEM_PROMPTS = {
    "LITERATURE_SURVEY": (
        "You are a mathematical literature researcher. Your goal is to understand what is already "
        "known about the problem. Search arXiv for relevant papers, check the OEIS for related "
        "sequences, and use web search for additional context. Record every relevant theorem, "
        "partial result, and known technique in the document store under 'literature/*' keys. "
        "Dead ends in the literature are as valuable as successes — document them too. "
        "When you have surveyed the topic thoroughly, call submit_work with signal ACCEPTED."
    ),
    "PROBLEM_FORMALIZATION": (
        "You are a mathematical formalist. Translate the informal problem statement into precise "
        "mathematical notation. Define all terms, state the unknowns, and identify the key "
        "unknowns. Use run_sympy to test your notation for computational consistency. Store your "
        "formalization in the document store under 'formalization/problem'. "
        "When the problem is precisely formulated, call submit_work with signal ACCEPTED."
    ),
    "CALIBRATION": (
        "You are a mathematical calibrator. Before tackling the main problem, prove a weaker "
        "related result where the answer is already known. This establishes that the pipeline and "
        "your approach work correctly. Use run_sympy for all computations. Store the calibration "
        "result under 'calibration/result'. "
        "When the calibration proof is complete and verified by run_sympy, submit_work ACCEPTED."
    ),
    "COMPUTATIONAL_EXPLORATION": (
        "You are a computational mathematician. Perform numerical and symbolic searches up to "
        "stated bounds. Record what you find and what you rule out. Every null result is as "
        "important as a positive result — document bounds clearly. Use run_sympy for all "
        "computations. Store results under 'exploration/*' keys in the document store. "
        "State the bound you searched to. When exploration is complete, submit_work ACCEPTED."
    ),
    "HYPOTHESIS_GENERATION": (
        "You are a mathematical hypothesis generator. Review the exploration results and "
        "literature survey to synthesize candidate sub-conjectures, structural observations, "
        "and potential proof directions. Consult maestro if you need perspective on the "
        "mathematical significance. Store hypotheses under 'hypotheses/*' in the document store. "
        "When you have articulated at least one testable hypothesis, submit_work ACCEPTED."
    ),
    "PROOF_STRATEGY": (
        "You are a proof strategist. Choose a proof approach for the main problem, sketch the "
        "argument, and identify the critical lemmas needed. Draw on the literature survey and "
        "hypotheses. Use run_sympy to check intermediate claims computationally. Store your "
        "strategy under 'strategy/approach' in the document store. "
        "When the strategy is concrete and the key lemmas are identified, submit_work ACCEPTED."
    ),
    "PROOF_ATTEMPT": (
        "You are a proof writer. Write the formal proof based on the strategy. For each lemma, "
        "use run_sympy to verify any computational sub-claims. Store your proof draft under "
        "'proof/draft' in the document store and save the SymPy verification code as "
        "'sympy_proof_code' in the task content (for the verification gate). "
        "If a computation fails, read the error carefully — treat it as a unit test. "
        "When you have a complete proof and the SymPy verification passes, submit_work ACCEPTED."
    ),
    "FORMAL_VERIFICATION": (
        "You are a formal verification checker. Retrieve the proof from the document store "
        "('proof/draft') and the SymPy verification code from task content. Run the SymPy "
        "verification code using run_sympy. If it passes (exit code 0), submit_work ACCEPTED. "
        "If it fails, read the error output carefully and submit_work REJECTED with a detailed "
        "explanation of what failed and why."
    ),
    "WRITEUP": (
        "You are a mathematical writer. Produce a clean mathematical exposition: motivation, "
        "approach, result, and open questions. Draw on all documents in the store. The writeup "
        "should be suitable for a knowledgeable reader who has not seen the prior stages. "
        "Store the final writeup under 'writeup/final'. "
        "When the writeup is complete, submit_work ACCEPTED."
    ),
}

_NEW_STAGES = [
    {"key": "idea",                    "label": "Idea",                    "agent": "intake_agent",  "pos": 0,  "config": {}},
    {"key": "LITERATURE_SURVEY",       "label": "Literature Survey",       "agent": "generic_stage", "pos": 1,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["LITERATURE_SURVEY"],
        "tool_allowlist": ["search_arxiv", "search_oeis", "web_search", "store_document", "get_document", "list_documents", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 30,
    }},
    {"key": "PROBLEM_FORMALIZATION",   "label": "Problem Formalization",   "agent": "generic_stage", "pos": 2,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["PROBLEM_FORMALIZATION"],
        "tool_allowlist": ["get_document", "store_document", "write_file", "read_file", "run_sympy", "consult_maestro", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 20,
    }},
    {"key": "CALIBRATION",             "label": "Calibration",             "agent": "generic_stage", "pos": 3,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["CALIBRATION"],
        "tool_allowlist": ["run_sympy", "search_arxiv", "read_file", "write_file", "store_document", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 30,
    }},
    {"key": "COMPUTATIONAL_EXPLORATION","label": "Computational Exploration","agent": "generic_stage","pos": 4,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["COMPUTATIONAL_EXPLORATION"],
        "tool_allowlist": ["run_sympy", "read_file", "write_file", "get_document", "store_document", "list_documents", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 50,
    }},
    {"key": "HYPOTHESIS_GENERATION",   "label": "Hypothesis Generation",   "agent": "generic_stage", "pos": 5,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["HYPOTHESIS_GENERATION"],
        "tool_allowlist": ["get_document", "store_document", "list_documents", "search_arxiv", "consult_maestro", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 20,
    }},
    {"key": "PROOF_STRATEGY",          "label": "Proof Strategy",          "agent": "generic_stage", "pos": 6,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["PROOF_STRATEGY"],
        "tool_allowlist": ["get_document", "store_document", "search_arxiv", "consult_maestro", "run_sympy", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 20,
    }},
    {"key": "PROOF_ATTEMPT",           "label": "Proof Attempt",           "agent": "generic_stage", "pos": 7,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["PROOF_ATTEMPT"],
        "tool_allowlist": ["run_sympy", "write_file", "read_file", "get_document", "store_document", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 50,
    }},
    {"key": "FORMAL_VERIFICATION",     "label": "Formal Verification",     "agent": "generic_stage", "pos": 8,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["FORMAL_VERIFICATION"],
        "tool_allowlist": ["run_sympy", "get_document", "submit_work"],
        "gate_type": "llm_judge",
        "max_turns": 10,
    }},
    {"key": "WRITEUP",                 "label": "Writeup",                 "agent": "generic_stage", "pos": 9,  "config": {
        "system_prompt": _SYSTEM_PROMPTS["WRITEUP"],
        "tool_allowlist": ["read_file", "get_document", "list_documents", "store_document", "write_file", "submit_work"],
        "gate_type": "single_pass",
        "max_turns": 30,
    }},
    {"key": "accepted",                "label": "Accepted",                "agent": "terminal",      "pos": 10, "config": {}},
]

_NEW_TRANSITIONS = [
    ("idea",                    "LITERATURE_SURVEY",       "pass"),
    ("LITERATURE_SURVEY",       "PROBLEM_FORMALIZATION",   "pass"),
    ("PROBLEM_FORMALIZATION",   "CALIBRATION",             "pass"),
    ("CALIBRATION",             "COMPUTATIONAL_EXPLORATION","pass"),
    ("CALIBRATION",             "PROBLEM_FORMALIZATION",   "fail"),
    ("COMPUTATIONAL_EXPLORATION","HYPOTHESIS_GENERATION",  "pass"),
    ("HYPOTHESIS_GENERATION",   "PROOF_STRATEGY",          "pass"),
    ("PROOF_STRATEGY",          "PROOF_ATTEMPT",           "pass"),
    ("PROOF_STRATEGY",          "COMPUTATIONAL_EXPLORATION","fail"),
    ("PROOF_ATTEMPT",           "FORMAL_VERIFICATION",     "pass"),
    ("PROOF_ATTEMPT",           "PROOF_STRATEGY",          "fail"),
    ("FORMAL_VERIFICATION",     "WRITEUP",                 "pass"),
    ("FORMAL_VERIFICATION",     "PROOF_ATTEMPT",           "fail"),
    ("WRITEUP",                 "accepted",                "pass"),
]

# The original 9-stage design from migration 0073 (for down())
_ORIGINAL_STAGES = [
    {"key": "idea",               "label": "Idea",              "agent": "intake_agent",   "pos": 0,  "config": {}},
    {"key": "problem_statement",  "label": "Problem Statement", "agent": "planning_agent", "pos": 1,  "config": {"output_keys": ["problem_statement", "known_results"]}},
    {"key": "approach_factory",   "label": "Approach Factory",  "agent": "factory_node",   "pos": 2,  "config": {"factory_mode": "manual_prompt", "segmentation": "llm"}},
    {"key": "approach_planning",  "label": "Approach Planning", "agent": "planning_agent", "pos": 3,  "config": {"output_keys": ["approach_plan"]}},
    {"key": "proof_attempt",      "label": "Proof Attempt",     "agent": "custom_agent",   "pos": 4,  "config": {"verifier": "python_sympy"}},
    {"key": "peer_review",        "label": "Peer Review",       "agent": "custom_agent",   "pos": 5,  "config": {"reads_doc_pattern": "proofs/*"}},
    {"key": "synthesis",          "label": "Synthesis",         "agent": "custom_agent",   "pos": 6,  "config": {"reads_doc_pattern": "proofs/*"}},
    {"key": "human_review",       "label": "Human Review",      "agent": "human_gate",     "pos": 7,  "config": {}},
    {"key": "accepted",           "label": "Accepted",          "agent": "terminal",       "pos": 8,  "config": {}},
]

_ORIGINAL_TRANSITIONS = [
    ("idea",              "problem_statement", "pass"),
    ("problem_statement", "approach_factory",  "pass"),
    ("approach_factory",  "approach_planning", "pass"),
    ("approach_planning", "proof_attempt",     "pass"),
    ("proof_attempt",     "peer_review",       "pass"),
    ("proof_attempt",     "approach_planning", "fail"),
    ("peer_review",       "synthesis",         "pass"),
    ("peer_review",       "proof_attempt",     "reject"),
    ("synthesis",         "human_review",      "pass"),
    ("human_review",      "accepted",          "pass"),
]


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    return row["id"] if row else None


def _replace_stages(conn, tid, stages, transitions):
    """Delete all stages and transitions for tid, then insert new ones."""
    conn.execute("DELETE FROM pipeline_transitions WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_stages WHERE template_id = :tid", {"tid": tid})

    stage_key_to_id = {}
    for s in stages:
        config = s.get("config") or {}
        config_str = _json.dumps(config) if config else None
        if config_str is not None:
            sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, config)
                VALUES (:tid, :key, :label, :agent, :pos, CAST(:config AS jsonb))
            """
        else:
            sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, config)
                VALUES (:tid, :key, :label, :agent, :pos, NULL)
            """
        conn.execute(sql, {
            "tid": tid, "key": s["key"], "label": s["label"],
            "agent": s["agent"], "pos": s["pos"], "config": config_str,
        })
        row = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": s["key"]},
        ).fetchone()
        if row:
            stage_key_to_id[s["key"]] = row["id"]

    for (from_key, to_key, cond) in transitions:
        from_id = stage_key_to_id.get(from_key)
        to_id   = stage_key_to_id.get(to_key)
        if from_id and to_id:
            conn.execute("""
                INSERT INTO pipeline_transitions
                    (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :fid, :toid, :cond)
                ON CONFLICT DO NOTHING
            """, {"tid": tid, "fid": from_id, "toid": to_id, "cond": cond})

    return stage_key_to_id


def up(conn):
    tid = _get_template_id(conn, _TEMPLATE_NAME)
    if not tid:
        print(f"[0088] WARNING: template '{_TEMPLATE_NAME}' not found — skipping.")
        return
    print(f"[0088] Replacing stages for '{_TEMPLATE_NAME}' (template_id={tid})...")
    _replace_stages(conn, tid, _NEW_STAGES, _NEW_TRANSITIONS)
    # Update the description to reflect the new design
    conn.execute(
        "UPDATE pipeline_templates SET description = :desc WHERE id = :tid",
        {"desc": "Literature survey -> formalization -> exploration -> proof attempt -> formal verification -> writeup",
         "tid": tid},
    )
    print(f"[0088] Done — {len(_NEW_STAGES)} stages, {len(_NEW_TRANSITIONS)} transitions.")


def down(conn):
    tid = _get_template_id(conn, _TEMPLATE_NAME)
    if not tid:
        return
    print(f"[0088] Restoring original stages for '{_TEMPLATE_NAME}'...")
    _replace_stages(conn, tid, _ORIGINAL_STAGES, _ORIGINAL_TRANSITIONS)
    conn.execute(
        "UPDATE pipeline_templates SET description = :desc WHERE id = :tid",
        {"desc": "Problem statement -> approach factory -> proof attempts -> peer review -> synthesis",
         "tid": tid},
    )
    print("[0088] Done.")
