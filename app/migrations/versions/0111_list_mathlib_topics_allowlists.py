"""
Migration 0111 — Add list_mathlib_topics to math pipeline stage allowlists.
"""

import json

description = "Add list_mathlib_topics to math pipeline stage allowlists"

_TEMPLATE_NAME = "Mathematics / Proof Exploration"

_ADDITIONS: dict[str, list[str]] = {
    "LITERATURE_SURVEY":     ["list_mathlib_topics"],
    "PROBLEM_FORMALIZATION": ["list_mathlib_topics"],
    "PROOF_STRATEGY":        ["list_mathlib_topics"],
    "PROOF_ATTEMPT":         ["list_mathlib_topics"],
}


def _get_template_id(conn) -> int | None:
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :name",
        {"name": _TEMPLATE_NAME},
    ).fetchone()
    return row[0] if row else None


def up(conn) -> None:
    tid = _get_template_id(conn)
    if tid is None:
        return  # Template not installed; skip silently

    for stage_key, tools_to_add in _ADDITIONS.items():
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if row is None:
            continue
        sid, config_json = row
        config: dict = config_json if isinstance(config_json, dict) else (json.loads(config_json) if config_json else {})
        current: list[str] = config.get("tool_allowlist", [])
        for tool in tools_to_add:
            if tool not in current:
                current.append(tool)
        config["tool_allowlist"] = current
        conn.execute(
            "UPDATE pipeline_stages SET config = :cfg WHERE id = :sid",
            {"cfg": json.dumps(config), "sid": sid},
        )


def down(conn) -> None:
    tid = _get_template_id(conn)
    if tid is None:
        return

    for stage_key, tools_to_remove in _ADDITIONS.items():
        row = conn.execute(
            "SELECT id, config FROM pipeline_stages WHERE template_id = :tid AND stage_key = :key",
            {"tid": tid, "key": stage_key},
        ).fetchone()
        if row is None:
            continue
        sid, config_json = row
        config: dict = config_json if isinstance(config_json, dict) else (json.loads(config_json) if config_json else {})
        current: list[str] = config.get("tool_allowlist", [])
        config["tool_allowlist"] = [t for t in current if t not in tools_to_remove]
        conn.execute(
            "UPDATE pipeline_stages SET config = :cfg WHERE id = :sid",
            {"cfg": json.dumps(config), "sid": sid},
        )
