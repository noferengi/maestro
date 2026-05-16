description = "seed 6 built-in pipeline templates (Phase 10)"

# ---------------------------------------------------------------------------
# Template seed data — each entry fully describes one built-in template.
# stages[*].transitions reference stage_keys within the same template.
# ---------------------------------------------------------------------------
_TEMPLATES = [
    {
        "name": "Novel Writing",
        "description": "Outline -> chapter factory -> draft -> continuity check -> line edit -> publish",
        "stages": [
            {"key": "idea",              "label": "Idea",             "agent": "intake_agent",         "pos": 0},
            {"key": "outline",           "label": "Outline",          "agent": "planning_agent",        "pos": 1},
            {"key": "chapter_factory",   "label": "Chapter Factory",  "agent": "factory_node",          "pos": 2,
             "config": {"factory_mode": "manual_prompt", "segmentation": "llm"}},
            {"key": "chapter_draft",     "label": "Chapter Draft",    "agent": "writing_agent",         "pos": 3,
             "config": {"required_input_keys": ["outline"]}},
            {"key": "continuity_check",  "label": "Continuity Check", "agent": "custom_agent",          "pos": 4,
             "config": {"arch_category_keys": ["characters", "timeline"]}},
            {"key": "line_edit",         "label": "Line Edit",        "agent": "writing_agent",         "pos": 5},
            {"key": "human_review",      "label": "Human Review",     "agent": "human_gate",            "pos": 6},
            {"key": "published",         "label": "Published",        "agent": "terminal",              "pos": 7},
        ],
        "transitions": [
            ("idea",             "outline",          "pass"),
            ("outline",          "chapter_factory",  "pass"),
            ("chapter_factory",  "chapter_draft",    "pass"),
            ("chapter_draft",    "continuity_check", "pass"),
            ("continuity_check", "line_edit",        "pass"),
            ("continuity_check", "chapter_draft",    "fail"),
            ("line_edit",        "human_review",     "pass"),
            ("human_review",     "published",        "pass"),
        ],
        "arch_categories": [
            "Characters", "Themes", "Plot", "World Building",
            "Timeline", "Voice/Style", "Research Notes", "Continuity Log",
        ],
    },
    {
        "name": "Research Report",
        "description": "Refine topic -> research -> outline -> draft -> fact-check -> format -> publish",
        "stages": [
            {"key": "idea",              "label": "Idea",             "agent": "intake_agent",    "pos": 0},
            {"key": "topic_refinement",  "label": "Topic Refinement", "agent": "planning_agent",  "pos": 1},
            {"key": "research",          "label": "Research",         "agent": "research_agent",  "pos": 2,
             "config": {"tools": ["web_search"]}},
            {"key": "outline",           "label": "Outline",          "agent": "planning_agent",  "pos": 3},
            {"key": "draft",             "label": "Draft",            "agent": "writing_agent",   "pos": 4},
            {"key": "fact_check",        "label": "Fact Check",       "agent": "custom_agent",    "pos": 5},
            {"key": "formatting",        "label": "Formatting",       "agent": "writing_agent",   "pos": 6},
            {"key": "human_review",      "label": "Human Review",     "agent": "human_gate",      "pos": 7},
            {"key": "published",         "label": "Published",        "agent": "terminal",        "pos": 8},
        ],
        "transitions": [
            ("idea",            "topic_refinement", "pass"),
            ("topic_refinement","research",         "pass"),
            ("research",        "outline",          "pass"),
            ("outline",         "draft",            "pass"),
            ("draft",           "fact_check",       "pass"),
            ("draft",           "draft",            "fail"),
            ("fact_check",      "formatting",       "pass"),
            ("formatting",      "human_review",     "pass"),
            ("human_review",    "published",        "pass"),
        ],
        "arch_categories": [
            "Sources", "Key Claims", "Methodology", "Glossary", "Open Questions",
        ],
    },
    {
        "name": "Data Analysis",
        "description": "Refine question -> parallel data collection & schema design -> analysis -> viz -> write-up",
        "stages": [
            {"key": "idea",                "label": "Idea",               "agent": "intake_agent",    "pos": 0},
            {"key": "question_refinement", "label": "Question Refinement","agent": "planning_agent",  "pos": 1},
            {"key": "data_collection",     "label": "Data Collection",    "agent": "research_agent",  "pos": 2},
            {"key": "schema_design",       "label": "Schema Design",      "agent": "planning_agent",  "pos": 3},
            {"key": "analysis",            "label": "Analysis",           "agent": "custom_agent",    "pos": 4,
             "config": {"verifier": "python_sympy"}},
            {"key": "visualization",       "label": "Visualization",      "agent": "custom_agent",    "pos": 5},
            {"key": "write_up",            "label": "Write-Up",           "agent": "writing_agent",   "pos": 6},
            {"key": "human_review",        "label": "Human Review",       "agent": "human_gate",      "pos": 7},
            {"key": "completed",           "label": "Completed",          "agent": "terminal",        "pos": 8},
        ],
        "groups": [
            {"name": "Parallel Collection", "color": "#2196f3", "pos": 2,
             "members": ["data_collection", "schema_design"]},
        ],
        "transitions": [
            ("idea",                "question_refinement", "pass"),
            ("question_refinement", "data_collection",     "pass"),
            ("question_refinement", "schema_design",       "pass"),
            ("data_collection",     "analysis",            "pass"),
            ("schema_design",       "analysis",            "pass"),
            ("analysis",            "visualization",       "pass"),
            ("visualization",       "write_up",            "pass"),
            ("write_up",            "human_review",        "pass"),
            ("human_review",        "completed",           "pass"),
        ],
        "arch_categories": [
            "Datasets", "Hypotheses", "Statistical Methods", "Findings", "Caveats",
        ],
    },
    {
        "name": "Mathematics / Proof Exploration",
        "description": "Problem statement -> approach factory -> proof attempts -> peer review -> synthesis",
        "stages": [
            {"key": "idea",               "label": "Idea",              "agent": "intake_agent",   "pos": 0},
            {"key": "problem_statement",  "label": "Problem Statement", "agent": "planning_agent", "pos": 1,
             "config": {"output_keys": ["problem_statement", "known_results"]}},
            {"key": "approach_factory",   "label": "Approach Factory",  "agent": "factory_node",   "pos": 2,
             "config": {"factory_mode": "manual_prompt", "segmentation": "llm"}},
            {"key": "approach_planning",  "label": "Approach Planning", "agent": "planning_agent", "pos": 3,
             "config": {"output_keys": ["approach_plan"]}},
            {"key": "proof_attempt",      "label": "Proof Attempt",     "agent": "custom_agent",   "pos": 4,
             "config": {"verifier": "python_sympy"}},
            {"key": "peer_review",        "label": "Peer Review",       "agent": "custom_agent",   "pos": 5,
             "config": {"reads_doc_pattern": "proofs/*"}},
            {"key": "synthesis",          "label": "Synthesis",         "agent": "custom_agent",   "pos": 6,
             "config": {"reads_doc_pattern": "proofs/*"}},
            {"key": "human_review",       "label": "Human Review",      "agent": "human_gate",     "pos": 7},
            {"key": "accepted",           "label": "Accepted",          "agent": "terminal",       "pos": 8},
        ],
        "transitions": [
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
        ],
        "arch_categories": [
            "Known Theorems", "Definitions", "Conjectures",
            "Failed Approaches", "Partial Results", "Open Sub-Problems",
        ],
    },
    {
        "name": "Bug Triage",
        "description": "Reproduce -> root cause -> fix -> regression test -> resolve",
        "stages": [
            {"key": "bug_report",      "label": "Bug Report",      "agent": "intake_agent",          "pos": 0},
            {"key": "reproduce",       "label": "Reproduce",       "agent": "custom_agent",           "pos": 1},
            {"key": "root_cause",      "label": "Root Cause",      "agent": "custom_agent",           "pos": 2},
            {"key": "fix",             "label": "Fix",             "agent": "implementation_agent",   "pos": 3},
            {"key": "regression_test", "label": "Regression Test", "agent": "custom_agent",           "pos": 4,
             "config": {"verifier": "run_pytest"}},
            {"key": "human_review",    "label": "Human Review",    "agent": "human_gate",             "pos": 5},
            {"key": "resolved",        "label": "Resolved",        "agent": "terminal",               "pos": 6},
            {"key": "wontfix",         "label": "Won't Fix",       "agent": "terminal",               "pos": 7},
        ],
        "transitions": [
            ("bug_report",      "reproduce",       "pass"),
            ("reproduce",       "root_cause",      "pass"),
            ("reproduce",       "wontfix",         "fail"),
            ("root_cause",      "fix",             "pass"),
            ("fix",             "regression_test", "pass"),
            ("regression_test", "human_review",    "pass"),
            ("regression_test", "fix",             "fail"),
            ("human_review",    "resolved",        "pass"),
        ],
        "arch_categories": [
            "Repro Steps", "Root Cause", "Test Coverage", "Known Workarounds",
        ],
    },
    {
        "name": "Overnight Generation",
        "description": "Story bible -> nightly chapter factory -> outline -> draft -> continuity check",
        "stages": [
            {"key": "seed_prompt",      "label": "Seed Prompt",      "agent": "intake_agent",    "pos": 0},
            {"key": "story_bible",      "label": "Story Bible",      "agent": "planning_agent",  "pos": 1},
            {"key": "chapter_factory",  "label": "Chapter Factory",  "agent": "factory_node",    "pos": 2,
             "config": {"factory_mode": "cron", "cron": "0 23 * * *", "segmentation": "llm"}},
            {"key": "chapter_outline",  "label": "Chapter Outline",  "agent": "planning_agent",  "pos": 3},
            {"key": "chapter_draft",    "label": "Chapter Draft",    "agent": "writing_agent",   "pos": 4},
            {"key": "continuity_check", "label": "Continuity Check", "agent": "custom_agent",    "pos": 5,
             "config": {"arch_category_keys": ["characters", "story_arc"]}},
            {"key": "chapter_archive",  "label": "Chapter Archive",  "agent": "terminal",        "pos": 6},
        ],
        "transitions": [
            ("seed_prompt",     "story_bible",      "pass"),
            ("story_bible",     "chapter_factory",  "pass"),
            ("chapter_factory", "chapter_outline",  "pass"),
            ("chapter_outline", "chapter_draft",    "pass"),
            ("chapter_draft",   "continuity_check", "pass"),
            ("continuity_check","chapter_archive",  "pass"),
            ("continuity_check","chapter_draft",    "fail"),
        ],
        "arch_categories": [
            "Characters", "World Building", "Story Arc", "Chapter Log", "Style Guide",
        ],
    },
]


def _seed_template(conn, tpl):
    import json as _json

    name = tpl["name"]
    conn.execute("""
        INSERT INTO pipeline_templates (name, description, is_default, is_builtin)
        VALUES (:name, :desc, FALSE, TRUE)
        ON CONFLICT (name) DO NOTHING
    """, {"name": name, "desc": tpl["description"]})

    res = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    )
    row = res.fetchone()
    if not row:
        return
    tid = row["id"]

    # Only seed stages/transitions when the template is fresh (no stages yet)
    existing = conn.execute(
        "SELECT COUNT(*) AS n FROM pipeline_stages WHERE template_id = :tid", {"tid": tid}
    ).fetchone()["n"]
    if existing > 0:
        return

    # Groups (optional)
    group_name_to_id = {}
    for g in tpl.get("groups", []):
        conn.execute("""
            INSERT INTO pipeline_stage_groups (template_id, name, color, position)
            VALUES (:tid, :name, :color, :pos)
        """, {"tid": tid, "name": g["name"], "color": g.get("color"), "pos": g["pos"]})
        res = conn.execute("""
            SELECT id FROM pipeline_stage_groups
            WHERE template_id = :tid AND name = :name
        """, {"tid": tid, "name": g["name"]})
        group_name_to_id[g["name"]] = res.fetchone()["id"]

    # Stages
    stage_key_to_id = {}
    for s in tpl["stages"]:
        group_id = None
        for g in tpl.get("groups", []):
            if s["key"] in g.get("members", []):
                group_id = group_name_to_id.get(g["name"])
                break

        config_str = _json.dumps(s["config"]) if s.get("config") else None
        # Use CAST(:config AS jsonb) to avoid mixing :: cast with SQLAlchemy param syntax
        if conn.is_postgres and config_str is not None:
            insert_sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, group_id, config)
                VALUES (:tid, :key, :label, :agent, :pos, :gid, CAST(:config AS jsonb))
            """
        else:
            insert_sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, group_id, config)
                VALUES (:tid, :key, :label, :agent, :pos, :gid, :config)
            """
        conn.execute(insert_sql, {
            "tid": tid, "key": s["key"], "label": s["label"],
            "agent": s["agent"], "pos": s["pos"],
            "gid": group_id, "config": config_str,
        })
        res = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": s["key"]},
        )
        stage_key_to_id[s["key"]] = res.fetchone()["id"]

    # Transitions (self-referencing allowed, e.g. draft -> draft on fail)
    for (from_key, to_key, cond) in tpl["transitions"]:
        from_id = stage_key_to_id.get(from_key)
        to_id   = stage_key_to_id.get(to_key)
        if from_id and to_id:
            conn.execute("""
                INSERT INTO pipeline_transitions
                    (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :fid, :toid, :cond)
                ON CONFLICT DO NOTHING
            """, {"tid": tid, "fid": from_id, "toid": to_id, "cond": cond})

    # Arch categories
    for pos, label in enumerate(tpl.get("arch_categories", [])):
        key = label.lower().replace("/", "_").replace(" ", "_")
        conn.execute("""
            INSERT INTO pipeline_arch_categories (template_id, key, label, position)
            VALUES (:tid, :key, :label, :pos)
            ON CONFLICT (template_id, key) DO NOTHING
        """, {"tid": tid, "key": key, "label": label, "pos": pos})


def up(conn):
    print("[0073] Seeding built-in pipeline templates...")
    for tpl in _TEMPLATES:
        try:
            _seed_template(conn, tpl)
            print(f"  [0073] OK: {tpl['name']}")
        except Exception as exc:
            print(f"  [0073] WARN: {tpl['name']} — {exc}")
    print("[0073] Done.")


def down(conn):
    print("[0073] Removing built-in pipeline templates seeded by this migration...")
    names = [t["name"] for t in _TEMPLATES]
    placeholders = ", ".join(f":n{i}" for i in range(len(names)))
    params = {f"n{i}": n for i, n in enumerate(names)}
    res = conn.execute(
        f"SELECT id FROM pipeline_templates WHERE name IN ({placeholders}) AND is_builtin = TRUE",
        params,
    )
    tids = [r["id"] for r in res.fetchall()]
    if not tids:
        return
    tid_ph = ", ".join(str(t) for t in tids)
    conn.execute(f"DELETE FROM pipeline_transitions WHERE template_id IN ({tid_ph})")
    conn.execute(f"DELETE FROM pipeline_stages WHERE template_id IN ({tid_ph})")
    conn.execute(f"DELETE FROM pipeline_stage_groups WHERE template_id IN ({tid_ph})")
    conn.execute(f"DELETE FROM pipeline_arch_categories WHERE template_id IN ({tid_ph})")
    conn.execute(f"DELETE FROM pipeline_templates WHERE id IN ({tid_ph})")
    print(f"[0073] Removed {len(tids)} template(s).")
