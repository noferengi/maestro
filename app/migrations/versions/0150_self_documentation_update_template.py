description = "Self-Documentation Update built-in pipeline template"

import json as _json

_TEMPLATE = {
    "name": "Self-Documentation Update",
    "description": (
        "After a code change, identify stale documentation and rewrite it. "
        "Keeps ARCHITECTURE.md, CLAUDE.md, and CLAUDE_PIPELINE.md accurate without manual effort."
    ),
    "stages": [
        {
            "key": "diff_intake",
            "label": "Diff Intake",
            "agent": "intake_scope",
            "pos": 0,
        },
        {
            "key": "intake_gate",
            "label": "Intake Gate",
            "agent": "intake_gate",
            "pos": 1,
        },
        {
            "key": "identify_stale",
            "label": "Identify Stale Sections",
            "agent": "generic_stage",
            "pos": 2,
            "config": {
                "system_prompt": (
                    "You are a documentation analyst. Given a git diff, migration list, or "
                    "change summary, identify which sections of the Maestro documentation "
                    "files are stale and need updating.\n\n"
                    "Check the following documentation files:\n"
                    "- ARCHITECTURE.md — compute resource model, scheduler, agent types\n"
                    "- CLAUDE.md (project) — architecture overview, data flow, API routes\n"
                    "- CLAUDE_PIPELINE.md — template system, agent registry, card factory\n"
                    "- app/agent/CLAUDE.md — per-file agent descriptions\n"
                    "- app/tests/CLAUDE.md — test file descriptions and patch targets\n\n"
                    "For each stale section:\n"
                    "- File path and section heading\n"
                    "- What changed that made it stale\n"
                    "- What the section currently says (quote the first 100 chars)\n"
                    "- What it should say after the update\n\n"
                    "Call submit_work with JSON:\n"
                    "{\n"
                    "  \"stale_sections\": [\n"
                    "    {\"file\": \"...\", \"section\": \"...\", \"reason\": \"...\", "
                    "\"current_text\": \"...\", \"required_update\": \"...\"}\n"
                    "  ]\n"
                    "}"
                ),
                "tool_allowlist": [
                    "read_file", "find_files", "find_in_files",
                    "read_git_diff", "read_git_log", "submit_work",
                ],
                "output_keys": ["stale_sections"],
                "max_turns": 20,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "rewrite_sections",
            "label": "Rewrite Sections",
            "agent": "parallel_agents",
            "pos": 3,
            "config": {
                "agents": [
                    {
                        "name": "architecture_doc_writer",
                        "system_prompt": (
                            "You are a technical writer updating ARCHITECTURE.md.\n\n"
                            "Given the list of stale sections in ARCHITECTURE.md, rewrite "
                            "ONLY those sections. Do not change sections that are not stale. "
                            "Match the existing tone, heading style, and level of detail.\n\n"
                            "Read the current file first. Write only the changed sections as "
                            "a patch (old text → new text). "
                            "Call submit_work with {\"file\": \"ARCHITECTURE.md\", "
                            "\"patches\": [{\"section\": \"...\", \"old_text\": \"...\", "
                            "\"new_text\": \"...\"}]}"
                        ),
                        "tools": [
                            "read_file", "find_in_files", "read_git_diff",
                            "read_git_log", "submit_work",
                        ],
                        "max_turns": 20,
                    },
                    {
                        "name": "claude_md_writer",
                        "system_prompt": (
                            "You are a technical writer updating CLAUDE.md (the project-level "
                            "Claude Code instructions file).\n\n"
                            "Given the list of stale sections in CLAUDE.md, rewrite ONLY "
                            "those sections. Preserve all other content exactly. "
                            "CLAUDE.md is parsed by Claude Code — accuracy is critical. "
                            "Do not add examples or explanations not in the original style.\n\n"
                            "Call submit_work with {\"file\": \"CLAUDE.md\", "
                            "\"patches\": [{\"section\": \"...\", \"old_text\": \"...\", "
                            "\"new_text\": \"...\"}]}"
                        ),
                        "tools": [
                            "read_file", "find_in_files", "read_git_diff",
                            "read_git_log", "submit_work",
                        ],
                        "max_turns": 20,
                    },
                    {
                        "name": "pipeline_doc_writer",
                        "system_prompt": (
                            "You are a technical writer updating CLAUDE_PIPELINE.md.\n\n"
                            "Given the list of stale sections in CLAUDE_PIPELINE.md, rewrite "
                            "ONLY those sections. Preserve agent type descriptions, config "
                            "examples, and migration patterns — these are load-bearing for "
                            "Claude Code's understanding of the system. "
                            "Match the existing section structure and code-block style.\n\n"
                            "Call submit_work with {\"file\": \"CLAUDE_PIPELINE.md\", "
                            "\"patches\": [{\"section\": \"...\", \"old_text\": \"...\", "
                            "\"new_text\": \"...\"}]}"
                        ),
                        "tools": [
                            "read_file", "find_in_files", "read_git_diff",
                            "read_git_log", "submit_work",
                        ],
                        "max_turns": 20,
                    },
                ],
                "output_key": "doc_patches",
                "max_turns": 20,
            },
        },
        {
            "key": "consistency_check",
            "label": "Cross-Doc Consistency",
            "agent": "multiplier_node",
            "pos": 4,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["doc_patches"],
                "agents": [
                    {
                        "name": "terminology_checker",
                        "system_prompt": (
                            "You are a documentation consistency reviewer focused on terminology. "
                            "Check the proposed patches: do all three documentation files use "
                            "the same names for the same concepts? Do agent types, stage keys, "
                            "and pipeline names match between ARCHITECTURE.md, CLAUDE.md, and "
                            "CLAUDE_PIPELINE.md? "
                            "Vote ACCEPTED if terminology is consistent, REJECTED if conflicts found. "
                            "Call submit_work with {\"conflicts\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "accuracy_checker",
                        "system_prompt": (
                            "You are a documentation accuracy reviewer. Check the proposed patches "
                            "against the git diff and migration list. Does each patch correctly "
                            "describe what the code now does? Are there any inaccuracies, "
                            "omissions, or descriptions that conflict with the code? "
                            "Vote ACCEPTED if patches are accurate, REJECTED if inaccuracies found. "
                            "Call submit_work with {\"inaccuracies\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                    {
                        "name": "completeness_checker",
                        "system_prompt": (
                            "You are a documentation completeness reviewer. Check whether the "
                            "proposed patches cover all the stale sections identified. Are any "
                            "stale sections left unaddressed? Did the writers miss any implications "
                            "of the code change that should be documented? "
                            "Vote ACCEPTED if coverage is complete, REJECTED if significant gaps remain. "
                            "Call submit_work with {\"missing\": [...], \"verdict\": \"ACCEPTED|REJECTED\"}"
                        ),
                        "max_turns": 10,
                    },
                ],
                "output_key": "consistency_verdict",
            },
        },
        {
            "key": "human_review",
            "label": "Human Review",
            "agent": "human_gate",
            "pos": 5,
        },
        {
            "key": "docs_committed",
            "label": "Docs Committed",
            "agent": "terminal",
            "pos": 6,
        },
    ],
    "transitions": [
        ("diff_intake",       "intake_gate",        "pass"),
        ("intake_gate",       "identify_stale",     "pass"),
        ("intake_gate",       "diff_intake",        "fail"),
        ("identify_stale",    "rewrite_sections",   "pass"),
        ("rewrite_sections",  "consistency_check",  "pass"),
        ("consistency_check", "human_review",       "pass"),
        ("consistency_check", "rewrite_sections",   "fail"),
        ("human_review",      "docs_committed",     "pass"),
    ],
    "arch_categories": [
        "Change Diffs", "Stale Sections", "Doc Patches",
        "Consistency Reports", "Committed Docs",
    ],
}


def _seed(conn, tpl):
    name = tpl["name"]
    conn.execute(
        """
        INSERT INTO pipeline_templates (name, description, is_default, is_builtin)
        VALUES (:name, :desc, FALSE, TRUE)
        ON CONFLICT (name) DO NOTHING
        """,
        {"name": name, "desc": tpl["description"]},
    )
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    if not row:
        return
    tid = row["id"]

    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_stages WHERE template_id = :tid", {"tid": tid}
    ).fetchone()["n"]
    if existing > 0:
        return

    stage_key_to_id = {}
    for s in tpl["stages"]:
        config_str = _json.dumps(s["config"]) if s.get("config") else None
        conn.execute(
            """
            INSERT INTO pipeline_stages
                (template_id, stage_key, label, agent_type, position, config)
            VALUES (:tid, :key, :label, :agent, :pos, CAST(:config AS jsonb))
            """,
            {
                "tid": tid, "key": s["key"], "label": s["label"],
                "agent": s["agent"], "pos": s["pos"], "config": config_str,
            },
        )
        row = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": s["key"]},
        ).fetchone()
        stage_key_to_id[s["key"]] = row["id"]

    for from_key, to_key, cond in tpl["transitions"]:
        from_id = stage_key_to_id.get(from_key)
        to_id = stage_key_to_id.get(to_key)
        if from_id and to_id:
            conn.execute(
                """
                INSERT INTO pipeline_transitions
                    (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :fid, :toid, :cond)
                ON CONFLICT DO NOTHING
                """,
                {"tid": tid, "fid": from_id, "toid": to_id, "cond": cond},
            )

    for pos, label in enumerate(tpl.get("arch_categories", [])):
        key = label.lower().replace("/", "_").replace(" ", "_")
        conn.execute(
            """
            INSERT INTO pipeline_arch_categories (template_id, key, label, position)
            VALUES (:tid, :key, :label, :pos)
            ON CONFLICT (template_id, key) DO NOTHING
            """,
            {"tid": tid, "key": key, "label": label, "pos": pos},
        )


def up(conn):
    _seed(conn, _TEMPLATE)


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name AND is_builtin = TRUE",
        {"name": _TEMPLATE["name"]},
    ).fetchone()
    if not row:
        return
    tid = row["id"]
    conn.execute("DELETE FROM pipeline_arch_categories WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_transitions WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_stages WHERE template_id = :tid", {"tid": tid})
    conn.execute("DELETE FROM pipeline_templates WHERE id = :tid", {"tid": tid})
