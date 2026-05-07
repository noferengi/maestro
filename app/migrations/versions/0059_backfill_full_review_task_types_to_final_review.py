description = "backfill full_review task types to final_review"


def up(conn):
    # Migration 0056 renamed the results table but did not update tasks.type values.
    # Tasks that completed security before the code change have type='full_review';
    # the scheduler only dispatches type='final_review', making them permanently invisible.
    conn.execute("UPDATE tasks SET type = 'final_review' WHERE type = 'full_review'")
    conn.commit()


def down(conn):
    conn.execute("UPDATE tasks SET type = 'full_review' WHERE type = 'final_review'")
    conn.commit()
