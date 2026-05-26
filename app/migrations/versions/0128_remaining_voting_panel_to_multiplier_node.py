description = "Convert remaining voting_panel stages to multiplier_node (vote_tally)"

import json as _json

# Default tool set for reviewer agents that have no per-reviewer tools defined.
_DEFAULT_VOTER_TOOLS = [
    "read_file", "read_file_metadata", "list_directory",
    "grep_file", "run_pytest",
]

# Keys that are voting_panel-specific and must not be forwarded to the new config.
_VP_ONLY_KEYS = {"reviewers", "voter_tools", "voter_max_turns", "voter_count", "voter_system_prompt"}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, template_id, stage_key):
    return conn.execute(
        "SELECT id, config FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": template_id, "key": stage_key},
    ).fetchone()


def _to_multiplier_cfg(cfg, default_output_key):
    """Translate a voting_panel config dict to a multiplier_node (vote_tally) config dict."""
    reviewers = cfg.get("reviewers", [])

    voter_tools_str = cfg.get("voter_tools", "")
    global_tools = (
        [t.strip() for t in voter_tools_str.split(",") if t.strip()]
        if voter_tools_str
        else _DEFAULT_VOTER_TOOLS
    )
    global_max_turns = cfg.get("voter_max_turns", 10)

    agents = [
        {
            "name":          r["name"],
            "system_prompt": r["system_prompt"],
            "tools":         r.get("tools", global_tools),
            "max_turns":     r.get("max_turns", global_max_turns),
        }
        for r in reviewers
    ]

    new_cfg = {
        "agents":         agents,
        "collapser_mode": "vote_tally",
        "tally_strategy": cfg.get("tally_strategy", "majority"),
        "on_tie":         cfg.get("on_tie", "reject"),
        "output_key":     cfg.get("output_key", default_output_key),
    }

    # Carry forward keys that are not voting_panel-specific so scheduler-level
    # features (required_input_keys, required_tool_groups) remain active.
    for k, v in cfg.items():
        if k not in _VP_ONLY_KEYS and k not in new_cfg:
            new_cfg[k] = v

    return new_cfg, agents


def _to_voting_panel_cfg(cfg):
    """Translate a multiplier_node config dict back to voting_panel format."""
    agents = cfg.get("agents", [])
    tools = agents[0]["tools"] if agents else _DEFAULT_VOTER_TOOLS
    max_turns = agents[0]["max_turns"] if agents else 10
    reviewers = [{"name": a["name"], "system_prompt": a["system_prompt"]} for a in agents]

    old_cfg = {
        "voter_count":     len(agents),
        "voter_max_turns": max_turns,
        "voter_tools":     ", ".join(tools),
        "tally_strategy":  cfg.get("tally_strategy", "majority"),
        "on_tie":          cfg.get("on_tie", "reject"),
        "output_key":      cfg.get("output_key"),
        "reviewers":       reviewers,
    }

    # Re-attach any non-multiplier keys (e.g. required_tool_groups, required_input_keys).
    _MN_KEYS = {"agents", "collapser_mode", "tally_strategy", "on_tie", "output_key"}
    for k, v in cfg.items():
        if k not in _MN_KEYS and k not in old_cfg:
            old_cfg[k] = v

    return old_cfg


# ---------------------------------------------------------------------------
# Stage descriptors: (template_name, stage_key, default_output_key)
# ---------------------------------------------------------------------------
_STAGES = [
    ("Software Development",            "planning_review",     "design_review_result"),
    ("Software Development",            "security",            "security_vote"),
    ("Bug Triage",                      "root_cause",          "root_cause_analysis"),
    ("Mathematics / Proof Exploration", "FORMAL_VERIFICATION", "formal_verification_result"),
    ("Research Report",                 "fact_check",          "fact_check_result"),
]


def up(conn):
    for template_name, stage_key, default_output_key in _STAGES:
        tid = _get_template_id(conn, template_name)
        if not tid:
            print(f"[0128] WARNING: template '{template_name}' not found — skipping.")
            continue

        stage = _get_stage(conn, tid, stage_key)
        if not stage:
            print(f"[0128] WARNING: stage '{stage_key}' in '{template_name}' not found — skipping.")
            continue

        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})

        new_cfg, agents = _to_multiplier_cfg(cfg, default_output_key)

        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = :at, config = CAST(:cfg AS jsonb) "
            "WHERE id = :id",
            {"at": "multiplier_node", "cfg": _json.dumps(new_cfg), "id": stage["id"]},
        )
        print(f"[0128] {template_name}/{stage_key} -> multiplier_node "
              f"({len(agents)} agents, tally={new_cfg['tally_strategy']}).")

    print("[0128] Done.")


def down(conn):
    for template_name, stage_key, _default in _STAGES:
        tid = _get_template_id(conn, template_name)
        if not tid:
            continue

        stage = _get_stage(conn, tid, stage_key)
        if not stage:
            continue

        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})

        old_cfg = _to_voting_panel_cfg(cfg)

        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = :at, config = CAST(:cfg AS jsonb) "
            "WHERE id = :id",
            {"at": "voting_panel", "cfg": _json.dumps(old_cfg), "id": stage["id"]},
        )
        print(f"[0128] reverted {template_name}/{stage_key} -> voting_panel.")

    print("[0128] Done.")
