"""
Fix planning stage position collision in templates modified by migration 0109.

After 0109 inserted planning at position 0.5 (which PostgreSQL rounded to 1),
the planning stage ends up at the same position as the first work stage.
This migration shifts all non-system work stages (position >= 1, excluding
'idea' and 'planning') up by 1 to open a clean gap:
  idea(0) -> planning(1) -> first_work_stage(2) -> ...
"""

description = "Fix planning stage positions after 0109 integer rounding"


# Templates that received a planning stage from migration 0109
_AFFECTED_TEMPLATE_IDS = [7, 8, 9, 12, 13]


def up(conn):
    for tid in _AFFECTED_TEMPLATE_IDS:
        # Increment position of all stages except idea and planning
        conn.execute(
            """
            UPDATE pipeline_stages
            SET position = position + 1
            WHERE template_id = :tid
              AND stage_key NOT IN ('idea', 'planning')
            """,
            {"tid": tid},
        )


def down(conn):
    for tid in _AFFECTED_TEMPLATE_IDS:
        conn.execute(
            """
            UPDATE pipeline_stages
            SET position = position - 1
            WHERE template_id = :tid
              AND stage_key NOT IN ('idea', 'planning')
            """,
            {"tid": tid},
        )
