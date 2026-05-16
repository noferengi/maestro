description = "expand ai review group to all four review stages"


def up(conn):
    # Find the Software Development template
    res = conn.execute("SELECT id FROM pipeline_templates WHERE name = 'Software Development'")
    row = res.fetchone()
    if not row:
        print("[0074] Software Development template not found — skipping")
        return
    template_id = row['id']

    # Rename the group and expand it to cover all four review stages
    conn.execute("""
        UPDATE pipeline_stage_groups
           SET name = 'AI Review', color = '#7c3aed'
         WHERE template_id = :tid AND name = 'Optimization + Security'
    """, {"tid": template_id})

    res2 = conn.execute("""
        SELECT id FROM pipeline_stage_groups
         WHERE template_id = :tid AND name = 'AI Review'
    """, {"tid": template_id})
    grp = res2.fetchone()
    if not grp:
        print("[0074] AI Review group not found after rename — skipping stage assignment")
        return
    group_id = grp['id']

    # Assign all four review stages to the group
    for key in ['conceptual_review', 'optimization', 'security', 'final_review']:
        conn.execute("""
            UPDATE pipeline_stages
               SET group_id = :gid
             WHERE template_id = :tid AND stage_key = :key
        """, {"gid": group_id, "tid": template_id, "key": key})

    print(f"[0074] AI Review group {group_id} now covers: conceptual_review, optimization, security, final_review")


def down(conn):
    res = conn.execute("SELECT id FROM pipeline_templates WHERE name = 'Software Development'")
    row = res.fetchone()
    if not row:
        return
    template_id = row['id']

    conn.execute("""
        UPDATE pipeline_stage_groups
           SET name = 'Optimization + Security', color = '#f59e0b'
         WHERE template_id = :tid AND name = 'AI Review'
    """, {"tid": template_id})

    # Remove conceptual_review and final_review from the group
    for key in ['conceptual_review', 'final_review']:
        conn.execute("""
            UPDATE pipeline_stages
               SET group_id = NULL
             WHERE template_id = :tid AND stage_key = :key
        """, {"tid": template_id, "key": key})
