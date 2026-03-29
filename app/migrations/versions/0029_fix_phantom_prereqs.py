description = "Repair phantom -subN prerequisite IDs left by old subdivision code"

import json
import re

_PHANTOM_RE = re.compile(r'^task-[\d.]+-sub(\d+)$')


def up(conn):
    conn.row_factory = lambda c, r: dict(zip([col[0] for col in c.description], r))

    records = conn.execute(
        "SELECT id, child_task_ids FROM subdivision_records WHERE child_task_ids IS NOT NULL"
    ).fetchall()

    total_fixed = 0
    for rec in records:
        try:
            child_ids = json.loads(rec['child_task_ids'])
        except (json.JSONDecodeError, TypeError):
            continue
        if not child_ids:
            continue

        for child_id in child_ids:
            row = conn.execute(
                "SELECT prerequisites FROM tasks WHERE id = ?", (child_id,)
            ).fetchone()
            if not row or not row['prerequisites']:
                continue

            try:
                prereqs = json.loads(row['prerequisites'])
            except (json.JSONDecodeError, TypeError):
                continue

            new_prereqs = []
            changed = False
            for p in prereqs:
                m = _PHANTOM_RE.match(p)
                if m:
                    ordinal = int(m.group(1))
                    if ordinal < len(child_ids):
                        new_prereqs.append(child_ids[ordinal])
                        changed = True
                    # else: out-of-range ordinal — drop it (was invalid anyway)
                else:
                    new_prereqs.append(p)

            if changed:
                conn.execute(
                    "UPDATE tasks SET prerequisites = ? WHERE id = ?",
                    (json.dumps(new_prereqs), child_id)
                )
                total_fixed += 1

    conn.commit()
    print(f"[0029] Fixed phantom prereqs on {total_fixed} tasks.")


def down(conn):
    # Cannot restore phantom IDs — they were never real. No-op.
    print("[0029] down(): no-op — phantom prereqs cannot be restored.")
