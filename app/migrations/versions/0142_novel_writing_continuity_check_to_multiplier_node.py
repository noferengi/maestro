description = "novel writing continuity_check upgraded from custom_agent to multiplier_node (3 specialist reviewers)"

_CONTINUITY_CONFIG = {
    "n": 3,
    "collapser_mode": "vote_tally",
    "tally_strategy": "majority",
    "on_tie": "reject",
    "output_key": "continuity_check_result",
    "arch_category_keys": ["characters", "timeline"],
    "agents": [
        {
            "name": "timeline_checker",
            "system_prompt": (
                "You are a continuity editor checking temporal logic. "
                "Verify that the chapter's events are consistent with the established timeline. "
                "Vote ACCEPTED if consistent, REJECTED if you find a contradiction. "
                "Call submit_work with your verdict."
            ),
        },
        {
            "name": "character_voice",
            "system_prompt": (
                "You are a character voice editor. "
                "Verify that each character's dialogue and actions are consistent with their "
                "established personality and arc from the story plan. "
                "Call submit_work with ACCEPTED or REJECTED."
            ),
        },
        {
            "name": "world_consistency",
            "system_prompt": (
                "You are a world-building consistency checker. "
                "Verify that settings, rules, and established facts are not contradicted. "
                "Call submit_work with ACCEPTED or REJECTED."
            ),
        },
    ],
}

# Stage IDs for continuity_check in Novel Writing (12) and My Novel Pipeline (13)
_STAGE_IDS = (71, 79)


def up(conn):
    import json

    config_json = json.dumps(_CONTINUITY_CONFIG)
    for stage_id in _STAGE_IDS:
        conn.execute(
            """
            UPDATE pipeline_stages
               SET agent_type = 'multiplier_node',
                   config     = CAST(:config AS jsonb)
             WHERE id = :stage_id
            """,
            {"config": config_json, "stage_id": stage_id},
        )


def down(conn):
    import json

    original_config = json.dumps({"arch_category_keys": ["characters", "timeline"]})
    for stage_id in _STAGE_IDS:
        conn.execute(
            """
            UPDATE pipeline_stages
               SET agent_type = 'custom_agent',
                   config     = CAST(:config AS jsonb)
             WHERE id = :stage_id
            """,
            {"config": original_config, "stage_id": stage_id},
        )
