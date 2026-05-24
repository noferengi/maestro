description = "Seed system_prompt into pipeline_stages.config for built-in SW Dev template stages"

import json as _json

# Prompts are copied verbatim from the agent Python defaults so the pipeline
# editor's system_prompt textarea shows the live default rather than a blank.
# Only updates rows where system_prompt is NULL or empty, so user-edited
# values are never overwritten.

_IDEA_PROMPT = """\
You are a senior analyst performing scope analysis on a proposed task.

Analyze the task and determine:
- Overall scope (small / medium / large / epic)
- Complexity rating (1-10)
- Whether the task should be decomposed into subtasks
- Key areas of the project or system likely affected by this task
- Estimated effort category (trivial, minor, moderate, significant, major)

To complete your analysis, call the submit_work tool with:
payload={
  "scope": "small" | "medium" | "large" | "epic",
  "complexity": <integer 1-10>,
  "decomposition_needed": <boolean>,
  "subtasks": [<string>, ...],
  "affected_areas": [<string>, ...],
  "effort": "trivial" | "minor" | "moderate" | "significant" | "major",
  "vote": {
    "verdict": "POSSIBLE" | "LIKELY" | "NOT_SUITABLE" | "REJECTED" | "NEEDS_RESEARCH" | "SUBDIVIDE_IDEA",
    "confidence": <float 0.0-1.0>,
    "justification": "<one-paragraph explanation>"
  }
}

Verdict guidelines:
- LIKELY: Task is well-defined, reasonable scope, clearly feasible.
- POSSIBLE: Task is feasible but has some ambiguity or moderate complexity.
- NEEDS_RESEARCH: Task is too vague to assess — needs clarification before proceeding.
- NOT_SUITABLE: Task is poorly scoped, too large without decomposition, or fundamentally malformed.
- REJECTED: Reserve for tasks that are LOGICALLY IMPOSSIBLE, HARMFUL, or illegal. Extremely rare.
- SUBDIVIDE_IDEA: Task is fundamentally sound but too large for a single context window.

No prose after calling submit_work.\
"""

# Seeds the planning designer's role description. The format/submit_work
# instructions and optional spec block are always appended by the agent at
# runtime; this override replaces only the role + persona prefix.
_PLANNING_PROMPT = """\
You are a software architect. Design a detailed, actionable implementation plan for the task.

Consider correctness, security, code clarity, and performance when making design decisions.
Match the naming conventions, file layout, and idioms already present in the codebase.\
"""

_CONCEPTUAL_REVIEW_PROMPT = """\
You are a code reviewer. Your session ends when you call submit_work. \
Read what you need, reach a verdict, then call submit_work — \
do not loop back to re-check things you have already seen.\
"""

_OPTIMIZATION_PROMPT = """\
You are an optimization expert. Use submit_work to output your proposals when ready.\
"""

_SECURITY_PROMPT = """\
You are a security expert. Use submit_work to output your verdict when ready.\
"""

_FINAL_REVIEW_PROMPT = """\
You are a code reviewer. Your session ends when you call submit_work. \
Read what you need, reach a verdict, then call submit_work — \
do not loop back to re-check things you have already seen.\
"""

# stage_key -> prompt string for each template
_SW_DEV_SEEDS = {
    "idea":              _IDEA_PROMPT,
    "planning":          _PLANNING_PROMPT,
    "conceptual_review": _CONCEPTUAL_REVIEW_PROMPT,
    "optimization":      _OPTIMIZATION_PROMPT,
    "security":          _SECURITY_PROMPT,
    "final_review":      _FINAL_REVIEW_PROMPT,
}

# Math template: idea/planning share the same intake/planning agents
_MATH_SEEDS = {
    "idea":     _IDEA_PROMPT,
    "planning": _PLANNING_PROMPT,
}


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name", {"name": name}
    ).fetchone()
    return row["id"] if row else None


def _apply_seeds(conn, tid, seeds, label):
    updated = 0
    for stage_key, prompt in seeds.items():
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if not row:
            print(f"[0115] WARNING: stage '{stage_key}' not found in '{label}' — skipping.")
            continue
        raw = row["config"]
        cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
        if cfg.get("system_prompt"):
            print(f"[0115] Stage '{stage_key}' already has system_prompt — skipping.")
            continue
        cfg["system_prompt"] = prompt
        conn.execute(
            "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
            {"config": _json.dumps(cfg), "sid": row["id"]},
        )
        updated += 1
        print(f"[0115] Seeded system_prompt for '{stage_key}' in '{label}'.")
    return updated


def up(conn):
    sw_tid = _get_template_id(conn, "Software Development")
    if sw_tid:
        n = _apply_seeds(conn, sw_tid, _SW_DEV_SEEDS, "Software Development")
        print(f"[0115] Software Development: {n} stages seeded.")
    else:
        print("[0115] WARNING: 'Software Development' template not found — skipping.")

    math_tid = _get_template_id(conn, "Mathematics / Proof Exploration")
    if math_tid:
        n = _apply_seeds(conn, math_tid, _MATH_SEEDS, "Mathematics / Proof Exploration")
        print(f"[0115] Mathematics / Proof Exploration: {n} stages seeded.")
    else:
        print("[0115] WARNING: 'Mathematics / Proof Exploration' template not found — skipping.")


def down(conn):
    for template_name, seeds in [
        ("Software Development", _SW_DEV_SEEDS),
        ("Mathematics / Proof Exploration", _MATH_SEEDS),
    ]:
        tid = _get_template_id(conn, template_name)
        if not tid:
            continue
        for stage_key, prompt in seeds.items():
            row = conn.execute(
                "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
                {"tid": tid, "key": stage_key},
            ).fetchone()
            if not row:
                continue
            raw = row["config"]
            cfg = raw if isinstance(raw, dict) else (_json.loads(raw) if raw else {})
            if cfg.get("system_prompt") == prompt:
                cfg.pop("system_prompt", None)
                conn.execute(
                    "UPDATE pipeline_stages SET config = CAST(:config AS jsonb) WHERE id = :sid",
                    {"config": _json.dumps(cfg), "sid": row["id"]},
                )
                print(f"[0115] Removed system_prompt from '{stage_key}' in '{template_name}'.")
