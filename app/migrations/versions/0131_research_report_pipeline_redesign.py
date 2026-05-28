description = "research report pipeline redesign"

# ---------------------------------------------------------------------------
# New Research Report template — replaces the shallow 9-stage placeholder with
# a production-quality pipeline using registered executors only.
# ---------------------------------------------------------------------------

_NEW_TEMPLATE = {
    "name": "Research Report",
    "description": (
        "Full-depth research pipeline: decomposed intake → topic survey → "
        "competitive research proposal → parallel research threads → "
        "source validation → synthesis → draft review panel → publish"
    ),
    "stages": [
        {"key": "idea",              "label": "Idea",                "agent": "intake_scope",      "pos": 0},
        {"key": "intake_conflict",   "label": "Intake: Conflict",    "agent": "intake_conflict",   "pos": 1},
        {"key": "intake_feasibility","label": "Intake: Feasibility", "agent": "intake_feasibility","pos": 2},
        {"key": "intake_gate",       "label": "Intake: Gate",        "agent": "intake_gate",       "pos": 3},
        {
            "key": "topic_survey", "label": "Topic Survey",
            "agent": "generic_stage", "pos": 4,
            "config": {
                "system_prompt": (
                    "You are a research analyst. Conduct a preliminary web survey of the given topic.\n"
                    "1. Identify the key sub-questions this topic raises.\n"
                    "2. Note the landscape: competing schools of thought, recent developments, known controversies.\n"
                    "3. Classify research complexity: simple (one dominant answer), contested (multiple valid "
                    "perspectives), or emerging (fast-moving, limited consensus).\n"
                    "Use web_search and web_fetch to survey 4-6 authoritative sources.\n"
                    "Call submit_work with a JSON payload containing exactly these keys:\n"
                    "  topic_survey: a 300-word structured overview of the topic\n"
                    "  key_questions: list of 4-6 sub-questions a complete report must answer"
                ),
                "tool_allowlist": ["web_search", "web_fetch", "submit_work"],
                "output_keys": ["topic_survey", "key_questions"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "research_propose", "label": "Research Proposal",
            "agent": "multiplier_node", "pos": 5,
            "config": {
                "n": 3,
                "collapser_mode": "judge_select",
                "agents": [
                    {
                        "name": "empiricist",
                        "system_prompt": (
                            "You are a systematic researcher who structures topics by empirical evidence. "
                            "Given the research topic and initial survey (provided in context), propose a "
                            "concrete research decomposition as a numbered list of research questions, "
                            "ordered from foundational facts → current applications → open debates. "
                            "Each question should be independently researchable. "
                            "Call submit_work with your proposed structure."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 10,
                    },
                    {
                        "name": "theorist",
                        "system_prompt": (
                            "You are a conceptual researcher who structures topics by intellectual lineage "
                            "and theoretical frameworks. Given the research topic (provided in context), "
                            "propose a research decomposition that traces competing schools of thought, "
                            "key theoretical models, and synthesizes a conceptual map of the field. "
                            "Call submit_work with your proposed structure."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 10,
                    },
                    {
                        "name": "practitioner",
                        "system_prompt": (
                            "You are a practical researcher focused on real-world impact. "
                            "Given the research topic (provided in context), propose a research decomposition "
                            "emphasizing concrete use cases, empirical outcomes, known limitations, and "
                            "actionable insights a reader would want from a report. "
                            "Call submit_work with your proposed structure."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 10,
                    },
                ],
                "judge_system_prompt": (
                    "You are a research director evaluating three proposed frameworks for investigating a topic. "
                    "Select the most comprehensive, rigorous, and practically useful framework — one that would "
                    "produce a complete, authoritative report. Consider: breadth of coverage, logical "
                    "progression, and suitability for the stated topic. "
                    "Signal SELECTED with the winning agent name and a brief rationale."
                ),
                "output_key": "winning_research_structure",
            },
        },
        {
            "key": "research_threads", "label": "Parallel Research",
            "agent": "parallel_agents", "pos": 6,
            "config": {
                "agents": [
                    {
                        "name": "background_researcher",
                        "system_prompt": (
                            "You are a research specialist assigned to investigate the historical background "
                            "and foundational concepts of the given topic. "
                            "Use web_search and web_fetch to gather information from authoritative sources. "
                            "Write a thorough, well-sourced report covering: origins, key milestones, "
                            "foundational concepts and definitions, and the intellectual trajectory. "
                            "Call submit_work with key 'findings' containing your complete findings "
                            "(aim for 800+ words with source citations)."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 20,
                    },
                    {
                        "name": "current_state_researcher",
                        "system_prompt": (
                            "You are a research specialist assigned to investigate the current state and "
                            "recent developments of the given topic. "
                            "Use web_search and web_fetch, focusing on sources from the last 2-3 years. "
                            "Write a thorough report covering: current consensus, recent advances, active "
                            "debates, and leading practitioners or institutions. "
                            "Call submit_work with key 'findings' containing your complete findings "
                            "(aim for 800+ words with source citations)."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 20,
                    },
                    {
                        "name": "critical_analyst",
                        "system_prompt": (
                            "You are a critical analyst assigned to investigate the controversies, "
                            "limitations, and open questions around the given topic. "
                            "Use web_search and web_fetch to find dissenting views, critiques, known "
                            "failures, and unresolved debates. "
                            "Write a thorough report covering: major criticisms, known limitations, "
                            "what remains unknown, and areas of active disagreement. "
                            "Call submit_work with key 'findings' containing your complete analysis "
                            "(aim for 800+ words with source citations)."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 20,
                    },
                ],
                "output_key": "research_findings",
                "max_turns": 20,
            },
        },
        {
            "key": "source_validation", "label": "Source Validation",
            "agent": "multiplier_node", "pos": 7,
            "config": {
                "n": 3,
                "collapser_mode": "vote_tally",
                "tally_strategy": "majority",
                "on_tie": "pass",
                "required_input_keys": ["research_findings"],
                "agents": [
                    {
                        "name": "accuracy_validator",
                        "system_prompt": (
                            "You are a fact-checker reviewing collected research findings (provided above). "
                            "Assess: Are sources credible? Can major factual claims be cross-referenced? "
                            "Are there unsupported assertions? "
                            "Vote ACCEPTED if research is factually sound. "
                            "Vote REJECTED if there are significant unsupported claims that would "
                            "undermine the final report."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 8,
                    },
                    {
                        "name": "coverage_validator",
                        "system_prompt": (
                            "You are a content editor reviewing research coverage (provided above). "
                            "Assess: Are all major angles of the topic addressed? Are there conspicuous "
                            "gaps in the narrative? "
                            "Vote ACCEPTED if coverage is sufficient for a complete report. "
                            "Vote REJECTED if major angles are missing."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 8,
                    },
                    {
                        "name": "recency_validator",
                        "system_prompt": (
                            "You are a research quality auditor focused on currency (provided above). "
                            "Assess: Is the research up-to-date where recency matters? Are there important "
                            "recent developments not captured? "
                            "Vote ACCEPTED if the research is current and relevant. "
                            "Vote REJECTED if it relies on outdated sources for a fast-moving topic."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 8,
                    },
                ],
                "output_key": "source_validation_result",
            },
        },
        {
            "key": "synthesis", "label": "Synthesis",
            "agent": "generic_stage", "pos": 8,
            "config": {
                "system_prompt": (
                    "You are a research synthesizer. You have access to three parallel research threads "
                    "and the agreed research structure (provided as prior stage outputs above). "
                    "Synthesize all findings into a structured report outline with populated sections:\n"
                    "1. Executive summary\n"
                    "2. Section-by-section outline with key points and supporting evidence\n"
                    "3. List of key claims with citations\n"
                    "4. Identified gaps or caveats\n"
                    "Call submit_work with key 'synthesis_outline' containing your full structured outline."
                ),
                "tool_allowlist": ["submit_work"],
                "required_input_keys": ["winning_research_structure", "research_findings"],
                "output_keys": ["synthesis_outline"],
                "max_turns": 10,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "draft", "label": "Draft",
            "agent": "generic_stage", "pos": 9,
            "config": {
                "system_prompt": (
                    "You are a research writer. Using the synthesis outline (provided as prior stage "
                    "output above), write the full research report. Requirements:\n"
                    "1. Professional prose with clear section headers\n"
                    "2. Properly cited sources inline\n"
                    "3. Executive summary at the top\n"
                    "4. Conclusion with actionable takeaways\n"
                    "The report should be comprehensive and publication-ready. "
                    "Call submit_work with key 'draft_content' containing the complete report text."
                ),
                "tool_allowlist": ["submit_work"],
                "required_input_keys": ["synthesis_outline", "topic_survey"],
                "output_keys": ["draft_content"],
                "max_turns": 15,
                "gate_type": "single_pass",
            },
        },
        {
            "key": "draft_review", "label": "Draft Review",
            "agent": "multiplier_node", "pos": 10,
            "config": {
                "n": 3,
                "collapser_mode": "judge_select",
                "required_input_keys": ["draft_content"],
                "agents": [
                    {
                        "name": "editor",
                        "system_prompt": (
                            "You are a senior editor reviewing a research report draft (provided above). "
                            "Evaluate: structure, argument flow, narrative coherence, section transitions. "
                            "If publication-ready, signal ACCEPTED with brief praise. "
                            "If revision is needed, signal REJECTED with specific, actionable editorial notes."
                        ),
                        "tools": ["submit_work"],
                        "max_turns": 8,
                    },
                    {
                        "name": "fact_checker",
                        "system_prompt": (
                            "You are a fact-checker reviewing a research report draft (provided above). "
                            "Verify: factual claims are supported, no contradictions between sections, "
                            "conclusions follow from evidence. "
                            "If factually sound, signal ACCEPTED. "
                            "If issues are found, signal REJECTED with specific corrections needed."
                        ),
                        "tools": ["web_search", "web_fetch", "submit_work"],
                        "max_turns": 8,
                    },
                    {
                        "name": "clarity_reviewer",
                        "system_prompt": (
                            "You are a readability specialist reviewing a research report draft "
                            "(provided above). Assess: clarity of writing, jargon is explained, "
                            "abstract accurately summarizes, conclusion is actionable. "
                            "If clear and accessible, signal ACCEPTED. "
                            "If clarity issues are found, signal REJECTED with specific improvements."
                        ),
                        "tools": ["submit_work"],
                        "max_turns": 8,
                    },
                ],
                "judge_system_prompt": (
                    "You are an editorial board reviewing a research report. Three reviewers have given "
                    "feedback on editorial quality, factual accuracy, and readability. "
                    "Decide: does this report advance to human review (signal ACCEPTED) or return for "
                    "revision (signal REJECTED)? Provide specific revision guidance if rejecting."
                ),
                "output_key": "draft_review_result",
            },
        },
        {
            "key": "circuit_breaker", "label": "Review Gate",
            "agent": "circuit_breaker", "pos": 11,
            "config": {
                "max_attempts": 3,
                "count_key": "draft_review_failures",
                "on_exhausted": "fail",
            },
        },
        {"key": "reflection",   "label": "Reflection",    "agent": "reflection_agent", "pos": 12},
        {"key": "human_review", "label": "Human Review",  "agent": "human_gate",       "pos": 13},
        {"key": "published",    "label": "Published",     "agent": "terminal",         "pos": 14},
    ],
    "transitions": [
        ("idea",               "intake_conflict",    "pass"),
        ("intake_conflict",    "intake_feasibility", "pass"),
        ("intake_feasibility", "intake_gate",        "pass"),
        ("intake_gate",        "topic_survey",       "pass"),
        ("intake_gate",        "idea",               "fail"),
        ("topic_survey",       "research_propose",   "pass"),
        ("topic_survey",       "topic_survey",       "fail"),
        ("research_propose",   "research_threads",   "pass"),
        ("research_threads",   "source_validation",  "pass"),
        ("source_validation",  "synthesis",          "pass"),
        ("source_validation",  "research_threads",   "fail"),
        ("synthesis",          "draft",              "pass"),
        ("draft",              "draft_review",       "pass"),
        ("draft_review",       "reflection",         "pass"),
        ("draft_review",       "circuit_breaker",    "fail"),
        ("circuit_breaker",    "draft",              "pass"),
        ("circuit_breaker",    "human_review",       "fail"),
        ("reflection",         "human_review",       "pass"),
        ("human_review",       "published",          "pass"),
    ],
    "arch_categories": [
        "Sources",
        "Key Claims",
        "Research Questions",
        "Methodology",
        "Findings",
        "Literature Review",
        "Open Questions",
        "Glossary",
    ],
}

# Original 9-stage structure (for down() rollback)
_OLD_TEMPLATE = {
    "name": "Research Report",
    "description": "Refine topic -> research -> outline -> draft -> fact-check -> format -> publish",
    "stages": [
        {"key": "idea",             "label": "Idea",             "agent": "intake_agent",   "pos": 0},
        {"key": "topic_refinement", "label": "Topic Refinement", "agent": "planning_agent", "pos": 1},
        {"key": "research",         "label": "Research",         "agent": "research_agent", "pos": 2,
         "config": {"tools": ["web_search"]}},
        {"key": "outline",          "label": "Outline",          "agent": "planning_agent", "pos": 3},
        {"key": "draft",            "label": "Draft",            "agent": "writing_agent",  "pos": 4},
        {"key": "fact_check",       "label": "Fact Check",       "agent": "custom_agent",   "pos": 5},
        {"key": "formatting",       "label": "Formatting",       "agent": "writing_agent",  "pos": 6},
        {"key": "human_review",     "label": "Human Review",     "agent": "human_gate",     "pos": 7},
        {"key": "published",        "label": "Published",        "agent": "terminal",       "pos": 8},
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
}


def _replace_template(conn, new_tpl):
    import json as _json

    name = new_tpl["name"]

    # Look up template
    res = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    )
    row = res.fetchone()
    if not row:
        print(f"  [0131] WARNING: template '{name}' not found — skipping")
        return
    tid = row["id"]

    # Delete old transitions first (FK: from_stage_id / to_stage_id)
    conn.execute(
        "DELETE FROM pipeline_transitions WHERE template_id = :tid", {"tid": tid}
    )
    # Delete old stages
    conn.execute(
        "DELETE FROM pipeline_stages WHERE template_id = :tid", {"tid": tid}
    )
    # Delete old arch categories
    conn.execute(
        "DELETE FROM pipeline_arch_categories WHERE template_id = :tid", {"tid": tid}
    )
    # Update description and bump version
    conn.execute(
        "UPDATE pipeline_templates SET description = :desc, version = version + 1 WHERE id = :tid",
        {"desc": new_tpl["description"], "tid": tid},
    )

    # Insert new stages and collect key→id map
    stage_key_to_id = {}
    for s in new_tpl["stages"]:
        config_str = _json.dumps(s["config"]) if s.get("config") else None
        if conn.is_postgres and config_str is not None:
            insert_sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, config)
                VALUES (:tid, :key, :label, :agent, :pos, CAST(:config AS jsonb))
            """
        else:
            insert_sql = """
                INSERT INTO pipeline_stages
                    (template_id, stage_key, label, agent_type, position, config)
                VALUES (:tid, :key, :label, :agent, :pos, :config)
            """
        conn.execute(insert_sql, {
            "tid": tid, "key": s["key"], "label": s["label"],
            "agent": s["agent"], "pos": s["pos"], "config": config_str,
        })
        res2 = conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": s["key"]},
        )
        stage_key_to_id[s["key"]] = res2.fetchone()["id"]

    # Insert transitions
    for (from_key, to_key, cond) in new_tpl["transitions"]:
        from_id = stage_key_to_id.get(from_key)
        to_id   = stage_key_to_id.get(to_key)
        if from_id and to_id:
            conn.execute("""
                INSERT INTO pipeline_transitions
                    (template_id, from_stage_id, to_stage_id, condition)
                VALUES (:tid, :fid, :toid, :cond)
            """, {"tid": tid, "fid": from_id, "toid": to_id, "cond": cond})

    # Insert arch categories
    for pos, label in enumerate(new_tpl.get("arch_categories", [])):
        key = label.lower().replace("/", "_").replace(" ", "_")
        conn.execute("""
            INSERT INTO pipeline_arch_categories (template_id, key, label, position)
            VALUES (:tid, :key, :label, :pos)
        """, {"tid": tid, "key": key, "label": label, "pos": pos})

    print(f"  [0131] Replaced '{name}': {len(new_tpl['stages'])} stages, "
          f"{len(new_tpl['transitions'])} transitions, "
          f"{len(new_tpl.get('arch_categories', []))} arch categories")


def up(conn):
    print("[0131] Redesigning Research Report pipeline template...")
    _replace_template(conn, _NEW_TEMPLATE)
    print("[0131] Done.")


def down(conn):
    print("[0131] Rolling back Research Report to original 9-stage template...")
    _replace_template(conn, _OLD_TEMPLATE)
    print("[0131] Done.")
