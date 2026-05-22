"""
Add planning stage and transitions to all pipeline templates that use 'idea' as
their entry stage but lack a visible 'planning' column.

Without this, tasks that enter the system planning phase (after intake passes)
become invisible on the kanban board, and advance_stage() silently fails because
there is no planning->first_work_stage edge — leaving tasks looping in planning
forever via the legacy SW-Dev fallback.

Affected templates (id: entry -> first work stage):
  7  Research Report:          idea -> topic_refinement
  8  Data Analysis:            idea -> question_refinement
  9  Mathematics/Proof:        idea -> LITERATURE_SURVEY
  12 Novel Writing:            idea -> outline
  13 My Novel Pipeline:        idea -> outline

For each template this migration:
  1. Inserts a 'planning' stage at position 0.5
  2. Removes the direct idea -> first_work_stage (pass) transition
  3. Adds idea -> planning (pass)
  4. Adds planning -> first_work_stage (pass)
  5. Adds planning -> idea (fail)
"""

description = "Add planning stage and transitions to templates missing it"


def up(conn):
    # Template config: (template_id, first_work_stage_key)
    TEMPLATES = [
        (7,  "topic_refinement"),
        (8,  "question_refinement"),
        (9,  "LITERATURE_SURVEY"),
        (12, "outline"),
        (13, "outline"),
    ]

    for template_id, first_work_key in TEMPLATES:
        # Skip if planning stage already exists
        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'planning'",
            {"tid": template_id},
        )
        if conn.fetchone():
            continue

        # Fetch idea stage id
        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'idea'",
            {"tid": template_id},
        )
        row = conn.fetchone()
        if not row:
            continue
        idea_id = row[0]

        # Fetch first work stage id
        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :sk",
            {"tid": template_id, "sk": first_work_key},
        )
        row = conn.fetchone()
        if not row:
            continue
        first_work_id = row[0]

        # Insert planning stage at position 0.5
        conn.execute(
            """
            INSERT INTO pipeline_stages (template_id, stage_key, label, position, agent_type, config)
            VALUES (:tid, 'planning', 'Planning', 0.5, 'planning_agent', NULL)
            RETURNING id
            """,
            {"tid": template_id},
        )
        row = conn.fetchone()
        planning_id = row[0]

        # Remove old idea -> first_work_stage (pass) transition
        conn.execute(
            """
            DELETE FROM pipeline_transitions
            WHERE template_id = :tid
              AND from_stage_id = :from_id
              AND to_stage_id = :to_id
              AND condition = 'pass'
            """,
            {"tid": template_id, "from_id": idea_id, "to_id": first_work_id},
        )

        # Add idea -> planning (pass)
        conn.execute(
            """
            INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority)
            VALUES (:tid, :from_id, :to_id, 'pass', 0)
            """,
            {"tid": template_id, "from_id": idea_id, "to_id": planning_id},
        )

        # Add planning -> first_work_stage (pass)
        conn.execute(
            """
            INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority)
            VALUES (:tid, :from_id, :to_id, 'pass', 0)
            """,
            {"tid": template_id, "from_id": planning_id, "to_id": first_work_id},
        )

        # Add planning -> idea (fail)
        conn.execute(
            """
            INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority)
            VALUES (:tid, :from_id, :to_id, 'fail', 0)
            """,
            {"tid": template_id, "from_id": planning_id, "to_id": idea_id},
        )


def down(conn):
    TEMPLATES = [
        (7,  "topic_refinement"),
        (8,  "question_refinement"),
        (9,  "LITERATURE_SURVEY"),
        (12, "outline"),
        (13, "outline"),
    ]

    for template_id, first_work_key in TEMPLATES:
        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'planning'",
            {"tid": template_id},
        )
        row = conn.fetchone()
        if not row:
            continue
        planning_id = row[0]

        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = 'idea'",
            {"tid": template_id},
        )
        row = conn.fetchone()
        if not row:
            continue
        idea_id = row[0]

        conn.execute(
            "SELECT id FROM pipeline_stages WHERE template_id = :tid AND stage_key = :sk",
            {"tid": template_id, "sk": first_work_key},
        )
        row = conn.fetchone()
        if not row:
            continue
        first_work_id = row[0]

        # Remove planning-era transitions
        conn.execute(
            "DELETE FROM pipeline_transitions WHERE template_id = :tid AND (from_stage_id = :pid OR to_stage_id = :pid)",
            {"tid": template_id, "pid": planning_id},
        )

        # Restore idea -> first_work_stage (pass)
        conn.execute(
            """
            INSERT INTO pipeline_transitions (template_id, from_stage_id, to_stage_id, condition, priority)
            VALUES (:tid, :from_id, :to_id, 'pass', 0)
            """,
            {"tid": template_id, "from_id": idea_id, "to_id": first_work_id},
        )

        # Remove planning stage
        conn.execute(
            "DELETE FROM pipeline_stages WHERE id = :pid",
            {"pid": planning_id},
        )
