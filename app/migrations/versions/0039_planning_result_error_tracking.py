"""
0039_planning_result_error_tracking

Add ``error_message`` (TEXT) to ``planning_results`` so that when a planning
pipeline run raises an exception the failure reason is stored in the row
instead of being lost.  The ``/planning-result`` API returns this to the UI so
the Stage Journal can show "run failed: <reason>" instead of displaying the
previous (now-superseded) stale result.
"""

description = "Add error_message to planning_results"


def up(conn):
    conn.execute(
        "ALTER TABLE planning_results ADD COLUMN error_message TEXT"
    )


def down(conn):
    # SQLite < 3.35 cannot DROP COLUMN; this migration is forward-only.
    pass
