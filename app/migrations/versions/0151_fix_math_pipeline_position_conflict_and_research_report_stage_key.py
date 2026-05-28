description = "fix math pipeline position conflict and research report stage key"

# Math pipeline (template_id=9): REFLECTION and FORMAL_VERIFICATION both at position 20.
# Correct order: FORMAL_VERIFICATION=20, REFLECTION=21, WRITEUP=22, accepted=23.
#
# Research Report pipeline (template_id=7): position-0 stage has stage_key='idea'
# instead of the canonical 'intake_scope' used by the intake router.


def up(conn):
    # Fix Math pipeline position conflict: shift REFLECTION, WRITEUP, accepted up by one.
    conn.execute(
        "UPDATE pipeline_stages SET position = 21 WHERE stage_key = 'REFLECTION' AND template_id = 9"
    )
    conn.execute(
        "UPDATE pipeline_stages SET position = 22 WHERE stage_key = 'WRITEUP' AND template_id = 9"
    )
    conn.execute(
        "UPDATE pipeline_stages SET position = 23 WHERE stage_key = 'accepted' AND template_id = 9"
    )

    # Fix Research Report stage_key at position 0: 'idea' -> 'intake_scope'
    conn.execute(
        "UPDATE pipeline_stages SET stage_key = 'intake_scope' WHERE stage_key = 'idea' AND template_id = 7"
    )


def down(conn):
    # Restore Math pipeline positions.
    conn.execute(
        "UPDATE pipeline_stages SET position = 20 WHERE stage_key = 'REFLECTION' AND template_id = 9"
    )
    conn.execute(
        "UPDATE pipeline_stages SET position = 21 WHERE stage_key = 'WRITEUP' AND template_id = 9"
    )
    conn.execute(
        "UPDATE pipeline_stages SET position = 22 WHERE stage_key = 'accepted' AND template_id = 9"
    )

    # Restore Research Report stage_key.
    conn.execute(
        "UPDATE pipeline_stages SET stage_key = 'idea' WHERE stage_key = 'intake_scope' AND position = 0 AND template_id = 7"
    )
