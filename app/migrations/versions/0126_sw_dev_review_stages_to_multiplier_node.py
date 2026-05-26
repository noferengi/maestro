description = "SW Dev: convert conceptual_review and final_review from voting_panel to multiplier_node"

import json as _json

_REVIEW_VOTER_TOOLS = [
    "read_file", "read_file_metadata", "list_directory",
    "grep_file", "run_pytest",
]


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def _get_stage(conn, tid, stage_key):
    return conn.execute(
        "SELECT id, config FROM pipeline_stages "
        "WHERE template_id = :tid AND stage_key = :key LIMIT 1",
        {"tid": tid, "key": stage_key},
    ).fetchone()


def up(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        print("[0126] WARNING: 'Software Development' template not found — skipping.")
        return

    for stage_key, default_output_key in [
        ("conceptual_review", "conceptual_vote"),
        ("final_review",      "final_vote"),
    ]:
        stage = _get_stage(conn, tid, stage_key)
        if not stage:
            print(f"[0126] WARNING: '{stage_key}' not found — skipping.")
            continue

        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})

        reviewers = cfg.get("reviewers", [])
        voter_tools_str = cfg.get("voter_tools", ", ".join(_REVIEW_VOTER_TOOLS))
        tools = [t.strip() for t in voter_tools_str.split(",") if t.strip()]
        max_turns = cfg.get("voter_max_turns", 10)

        agents = [
            {
                "name":          r["name"],
                "system_prompt": r["system_prompt"],
                "tools":         tools,
                "max_turns":     max_turns,
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

        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = :at, config = CAST(:cfg AS jsonb) "
            "WHERE id = :id",
            {"at": "multiplier_node", "cfg": _json.dumps(new_cfg), "id": stage["id"]},
        )
        print(f"[0126] {stage_key} -> multiplier_node ({len(agents)} agents).")

    print("[0126] Done.")


def down(conn):
    tid = _get_template_id(conn, "Software Development")
    if not tid:
        return

    for stage_key, default_output_key in [
        ("conceptual_review", "conceptual_vote"),
        ("final_review",      "final_vote"),
    ]:
        stage = _get_stage(conn, tid, stage_key)
        if not stage:
            continue

        raw = stage["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})

        agents = cfg.get("agents", [])
        tools = agents[0]["tools"] if agents else _REVIEW_VOTER_TOOLS
        max_turns = agents[0]["max_turns"] if agents else 10
        reviewers = [{"name": a["name"], "system_prompt": a["system_prompt"]} for a in agents]

        old_cfg = {
            "voter_count":    len(agents),
            "voter_max_turns": max_turns,
            "voter_tools":    ", ".join(tools),
            "on_tie":         cfg.get("on_tie", "reject"),
            "output_key":     cfg.get("output_key", default_output_key),
            "reviewers":      reviewers,
        }

        conn.execute(
            "UPDATE pipeline_stages "
            "SET agent_type = :at, config = CAST(:cfg AS jsonb) "
            "WHERE id = :id",
            {"at": "voting_panel", "cfg": _json.dumps(old_cfg), "id": stage["id"]},
        )
        print(f"[0126] reverted {stage_key} -> voting_panel.")

    print("[0126] Done.")
