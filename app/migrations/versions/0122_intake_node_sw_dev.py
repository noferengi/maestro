description = "SW Dev: idea stage agent_type -> intake_node"


def _get_template_id(conn, name):
    row = conn.execute(
        "SELECT id FROM pipeline_templates WHERE name = :n LIMIT 1", {"n": name}
    ).fetchone()
    return row["id"] if row else None


def up(conn):
    tmpl_id = _get_template_id(conn, "Software Development")
    if not tmpl_id:
        return
    conn.execute(
        """
        UPDATE pipeline_stages
        SET agent_type = 'intake_node'
        WHERE stage_key = 'idea'
          AND template_id = :tid
        """,
        {"tid": tmpl_id},
    )


def down(conn):
    tmpl_id = _get_template_id(conn, "Software Development")
    if not tmpl_id:
        return
    conn.execute(
        """
        UPDATE pipeline_stages
        SET agent_type = 'intake_agent'
        WHERE stage_key = 'idea'
          AND template_id = :tid
        """,
        {"tid": tmpl_id},
    )
