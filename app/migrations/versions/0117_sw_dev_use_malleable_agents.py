description = "SW Dev template: switch indev/security/conceptual_review/final_review to malleable agent types"

import json as _json

# Switches four stages in the Software Development template to use the
# malleable executor types introduced in the gentle-painting-firefly plan:
#
#   indev             -> dangerous_edit_llm_agent
#                       seeds system_prompt from MAESTRO_SYSTEM_PROMPT
#                       seeds agent_tools   from INDEV_AGENT_TOOLS
#
#   security          -> voting_panel
#   conceptual_review -> voting_panel
#   final_review      -> voting_panel
#
# Reviewer definitions (reviewers[], tally_strategy) are already seeded by
# migration 0116, which must be applied before this one.
# Planning stays as planning_agent — the PlanningPipeline's gate + correction
# logic cannot be replaced by fan_out_judge at this stage.

_SECURITY_VOTER_TOOLS = [
    "read_file", "read_file_metadata", "list_directory",
    "find_in_files", "find_files",
    "run_audit_bandit", "run_audit_pip", "run_audit_semgrep", "run_audit_npm",
    "submit_work",
]

_REVIEW_VOTER_TOOLS = [
    "read_file", "read_file_metadata", "list_directory",
    "find_in_files", "find_files", "find_symbol",
    "read_git_diff", "read_diff_stat",
    "submit_work",
]


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id, config, agent_type FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def _update_stage(conn, sid, agent_type, cfg):
    conn.execute(
        "UPDATE pipeline_stages "
        "SET agent_type = :at, config = CAST(:cfg AS jsonb) "
        "WHERE id = :sid",
        {"at": agent_type, "cfg": _json.dumps(cfg), "sid": sid},
    )


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0117] WARNING: 'Software Development' template not found — skipping.")
        return

    # ── indev -> dangerous_edit_llm_agent ─────────────────────────────────────
    row = _get_stage(conn, tid, "indev")
    if row:
        from app.agent.system_prompt import MAESTRO_SYSTEM_PROMPT
        from app.agent.config import INDEV_AGENT_TOOLS
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.setdefault("system_prompt", MAESTRO_SYSTEM_PROMPT)
        cfg.setdefault("agent_tools",   INDEV_AGENT_TOOLS)
        cfg.setdefault("max_turns",     200)
        _update_stage(conn, row["id"], "dangerous_edit_llm_agent", cfg)
        print("[0117] indev -> dangerous_edit_llm_agent (system_prompt + agent_tools seeded).")
    else:
        print("[0117] WARNING: 'indev' stage not found.")

    # ── security -> voting_panel ───────────────────────────────────────────────
    row = _get_stage(conn, tid, "security")
    if row:
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.setdefault("voter_count",    3)
        cfg.setdefault("voter_max_turns", 12)
        cfg.setdefault("voter_tools",    ", ".join(_SECURITY_VOTER_TOOLS))
        cfg.setdefault("on_tie",         "reject")
        cfg.setdefault("output_key",     "security_vote")
        _update_stage(conn, row["id"], "voting_panel", cfg)
        print("[0117] security -> voting_panel.")
    else:
        print("[0117] WARNING: 'security' stage not found.")

    # ── conceptual_review -> voting_panel ─────────────────────────────────────
    row = _get_stage(conn, tid, "conceptual_review")
    if row:
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.setdefault("voter_count",    4)
        cfg.setdefault("voter_max_turns", 10)
        cfg.setdefault("voter_tools",    ", ".join(_REVIEW_VOTER_TOOLS))
        cfg.setdefault("on_tie",         "reject")
        cfg.setdefault("output_key",     "conceptual_vote")
        _update_stage(conn, row["id"], "voting_panel", cfg)
        print("[0117] conceptual_review -> voting_panel.")
    else:
        print("[0117] WARNING: 'conceptual_review' stage not found.")

    # ── final_review -> voting_panel ───────────────────────────────────────────
    row = _get_stage(conn, tid, "final_review")
    if row:
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        cfg.setdefault("voter_count",    3)
        cfg.setdefault("voter_max_turns", 10)
        cfg.setdefault("voter_tools",    ", ".join(_REVIEW_VOTER_TOOLS))
        cfg.setdefault("on_tie",         "reject")
        cfg.setdefault("output_key",     "final_vote")
        _update_stage(conn, row["id"], "voting_panel", cfg)
        print("[0117] final_review -> voting_panel.")
    else:
        print("[0117] WARNING: 'final_review' stage not found.")

    print("[0117] Done.")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    reversions = [
        ("indev",             "implementation_agent",
         ["system_prompt", "agent_tools", "max_turns"]),
        ("security",          "security_agent",
         ["voter_count", "voter_max_turns", "voter_tools", "on_tie", "output_key"]),
        ("conceptual_review", "review_agent",
         ["voter_count", "voter_max_turns", "voter_tools", "on_tie", "output_key"]),
        ("final_review",      "final_review_agent",
         ["voter_count", "voter_max_turns", "voter_tools", "on_tie", "output_key"]),
    ]

    for stage_key, old_type, keys_to_remove in reversions:
        row = _get_stage(conn, tid, stage_key)
        if not row:
            continue
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        for k in keys_to_remove:
            cfg.pop(k, None)
        _update_stage(conn, row["id"], old_type, cfg)
        print(f"[0117] Reverted '{stage_key}' -> '{old_type}'.")
