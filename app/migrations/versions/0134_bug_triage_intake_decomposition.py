"""Replace the monolithic bug_report / intake_agent stage in the Bug Triage template
with four decomposed intake sub-stages:
  intake_scope → intake_conflict → intake_feasibility → intake_gate

Remaining stages are shifted +3. Fail on any intake stage loops back to intake_scope.
Gate passes to the existing 'reproduce' stage.
"""

import json

description = "bug triage intake decomposition"

_BUG_INTAKE_SCOPE_PROMPT = """\
You are a bug triage analyst assessing a reported defect for the Maestro agentic platform.

Analyse:
1. SEVERITY — Critical (data loss / crash), High (feature broken), Medium (degraded behaviour), Low (cosmetic)
2. IMPACT RADIUS — How many users / workflows are affected?
3. REPRODUCTION CLARITY — Is the report specific enough to reproduce? What is missing?
4. SCOPE — Is this a Maestro backend bug (Python), a frontend bug (JS/HTML), or a configuration issue?

Output: CLEAR, UNCLEAR, or OUT_OF_SCOPE, followed by a severity rating and rationale.
"""

_BUG_INTAKE_CONFLICT_PROMPT = """\
You are a bug deduplication analyst.

You will receive the bug report and a list of all currently open, non-resolved bug tasks.

Detect:
- EXACT DUPLICATE: Same root cause and symptom.
- RELATED: Different symptom but likely same root cause (note the related task ID).
- UNIQUE: No match found.

Output a structured conflict report. If duplicate, output the task ID of the existing bug.
"""

_BUG_INTAKE_FEASIBILITY_PROMPT = """\
You are a bug feasibility assessor for the Maestro platform.

Assess:
1. REPRODUCIBLE — Can this bug be reproduced from the information given?
2. LOCATABLE — Can a developer find the likely fault location in the codebase?
3. FIXABLE — Is the fix within the scope of Maestro's automated fix pipeline (Python/JS)?
4. INFORMATION COMPLETE — Is the stack trace, steps to reproduce, and environment specified?

Output: FEASIBLE, INFEASIBLE, or NEEDS_MORE_INFO, with a rationale.
If NEEDS_MORE_INFO, list exactly what information is missing.
"""


def up(conn):
    # ── 1. Resolve template ────────────────────────────────────────────────
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Bug Triage'"
    ).fetchone()
    if not row:
        print("[0134] Bug Triage template not found — skipping.")
        return
    tmpl_id = row[0]

    legacy = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'bug_report'",
        {"t": tmpl_id},
    ).fetchone()
    if not legacy:
        print("[0134] bug_report stage already removed — skipping.")
        return
    bug_report_id = legacy[0]

    # ── 2. Delete transitions touching bug_report ──────────────────────────
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
        {"t": tmpl_id, "s": bug_report_id},
    )

    # ── 3. Delete the legacy stage ─────────────────────────────────────────
    conn.execute(
        "DELETE FROM pipeline_stages WHERE id = :s", {"s": bug_report_id}
    )

    # ── 4. Shift remaining stages +3 ──────────────────────────────────────
    conn.execute(
        "UPDATE pipeline_stages SET position = position + 3 WHERE template_id = :t",
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

    s_scope    = ins("intake_scope",       "Intake: Scope",       "intake_scope",       0,
                     {"system_prompt": _BUG_INTAKE_SCOPE_PROMPT})
    s_conflict = ins("intake_conflict",    "Intake: Conflict",    "intake_conflict",    1,
                     {"system_prompt": _BUG_INTAKE_CONFLICT_PROMPT})
    s_feasib   = ins("intake_feasibility", "Intake: Feasibility", "intake_feasibility", 2,
                     {"system_prompt": _BUG_INTAKE_FEASIBILITY_PROMPT})
    s_igate    = ins("intake_gate",        "Intake: Gate",        "intake_gate",        3, {})

    # ── 6. Find reproduce stage ────────────────────────────────────────────
    reproduce_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'reproduce'",
        {"t": tmpl_id},
    ).fetchone()[0]

    # ── 7. Wire transitions ────────────────────────────────────────────────
    edges = [
        (s_scope,    s_scope,       "fail"),
        (s_scope,    s_conflict,    "pass"),
        (s_conflict, s_scope,       "fail"),
        (s_conflict, s_feasib,      "pass"),
        (s_feasib,   s_scope,       "fail"),
        (s_feasib,   s_igate,       "pass"),
        (s_igate,    s_scope,       "fail"),
        (s_igate,    reproduce_id,  "pass"),
    ]
    for (fr, to, cond) in edges:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )

    print(f"[0134] Inserted 4 intake stages + {len(edges)} transitions; remaining stages shifted +3.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Bug Triage'"
    ).fetchone()
    if not row:
        return
    tmpl_id = row[0]

    new_keys = ["intake_scope", "intake_conflict", "intake_feasibility", "intake_gate"]
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

    # Shift remaining stages back -3
    conn.execute(
        "UPDATE pipeline_stages SET position = position - 3 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # Re-insert legacy bug_report stage at position 0
    bug_report_id = conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'bug_report', 'Bug Report', 'intake_agent', 0, CAST('{}' AS jsonb)) "
        "RETURNING id",
        {"t": tmpl_id},
    ).fetchone()[0]

    reproduce_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'reproduce'",
        {"t": tmpl_id},
    ).fetchone()[0]

    conn.execute(
        "INSERT INTO pipeline_transitions "
        "(template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:t, :f, :to, 'pass', 0)",
        {"t": tmpl_id, "f": bug_report_id, "to": reproduce_id},
    )
    conn.execute(
        "INSERT INTO pipeline_transitions "
        "(template_id, from_stage_id, to_stage_id, condition, priority) "
        "VALUES (:t, :f, :to, 'fail', 0)",
        {"t": tmpl_id, "f": bug_report_id, "to": bug_report_id},
    )
