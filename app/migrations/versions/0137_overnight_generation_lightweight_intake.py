"""Replace the monolithic seed_prompt / intake_agent stage in the Overnight Generation template
with a lightweight 2-stage intake (scope + gate only — no conflict/feasibility, factory pipeline):
  intake_scope → intake_gate

Remaining stages are shifted +1. Fail loops back to intake_scope. Gate passes to 'story_bible'.
"""

import json

description = "overnight generation lightweight intake"

_OG_INTAKE_SCOPE_PROMPT = """\
You are a story concept validator for an overnight batch writing pipeline.

The pipeline will run unattended. Assess the seed prompt:
1. GENRE — Clear?
2. PROTAGONIST — Named or clearly implied?
3. CONFLICT — Central dramatic question stated?
4. TONE — Dark, light, comedic, thriller?

If all four are present: output READY.
If one or two are missing but inferable: output READY with a note on what was inferred.
If the prompt is too vague to proceed: output NOT_READY with a list of what must be specified.
"""


def up(conn):
    # ── 1. Resolve template ────────────────────────────────────────────────
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Overnight Generation'"
    ).fetchone()
    if not row:
        print("[0137] Overnight Generation template not found — skipping.")
        return
    tmpl_id = row[0]

    legacy = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'seed_prompt'",
        {"t": tmpl_id},
    ).fetchone()
    if not legacy:
        print("[0137] seed_prompt stage already removed — skipping.")
        return
    seed_id = legacy[0]

    # ── 2. Delete transitions touching seed_prompt ─────────────────────────
    conn.execute(
        "DELETE FROM pipeline_transitions "
        "WHERE template_id = :t AND (from_stage_id = :s OR to_stage_id = :s)",
        {"t": tmpl_id, "s": seed_id},
    )

    # ── 3. Delete the legacy stage ─────────────────────────────────────────
    conn.execute(
        "DELETE FROM pipeline_stages WHERE id = :s", {"s": seed_id}
    )

    # ── 4. Shift remaining stages +1 (2 new stages replace 1) ─────────────
    conn.execute(
        "UPDATE pipeline_stages SET position = position + 1 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # ── 5. Insert lightweight intake stages ───────────────────────────────
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

    s_scope = ins("intake_scope", "Intake: Scope", "intake_scope", 0,
                  {"system_prompt": _OG_INTAKE_SCOPE_PROMPT})
    s_igate = ins("intake_gate",  "Intake: Gate",  "intake_gate",  1, {})

    # ── 6. Find story_bible stage (gate passes here) ───────────────────────
    bible_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'story_bible'",
        {"t": tmpl_id},
    ).fetchone()[0]

    # ── 7. Wire transitions ────────────────────────────────────────────────
    edges = [
        (s_scope, s_scope,   "fail"),
        (s_scope, s_igate,   "pass"),
        (s_igate, s_scope,   "fail"),
        (s_igate, bible_id,  "pass"),
    ]
    for (fr, to, cond) in edges:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": fr, "to": to, "c": cond},
        )

    print(f"[0137] Inserted 2 intake stages + {len(edges)} transitions; remaining stages shifted +1.")


def down(conn):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = 'Overnight Generation'"
    ).fetchone()
    if not row:
        return
    tmpl_id = row[0]

    new_keys = ["intake_scope", "intake_gate"]
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

    # Shift remaining stages back -1
    conn.execute(
        "UPDATE pipeline_stages SET position = position - 1 WHERE template_id = :t",
        {"t": tmpl_id},
    )

    # Re-insert legacy seed_prompt stage at position 0
    seed_id = conn.execute(
        "INSERT INTO pipeline_stages "
        "(template_id, stage_key, label, agent_type, position, config) "
        "VALUES (:t, 'seed_prompt', 'Seed Prompt', 'intake_agent', 0, CAST('{}' AS jsonb)) "
        "RETURNING id",
        {"t": tmpl_id},
    ).fetchone()[0]

    bible_id = conn.execute(
        "SELECT id FROM pipeline_stages WHERE template_id = :t AND stage_key = 'story_bible'",
        {"t": tmpl_id},
    ).fetchone()[0]

    for cond, to in [("pass", bible_id), ("fail", seed_id)]:
        conn.execute(
            "INSERT INTO pipeline_transitions "
            "(template_id, from_stage_id, to_stage_id, condition, priority) "
            "VALUES (:t, :f, :to, :c, 0)",
            {"t": tmpl_id, "f": seed_id, "to": to, "c": cond},
        )
