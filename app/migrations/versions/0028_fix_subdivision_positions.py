"""
Migration 0027 - Fix subdivision children positions

This migration updates all subdivision children (tasks with parent_task_id set)
to have position=i based on their creation order within each parent group.

Before: All children of the same parent had position=0
After:  Children have position=0, 1, 2, ... based on created_at order

This fixes the scheduler starvation bug where children with same position
and same depth all had identical priority scores.
"""

from datetime import datetime


description = "Fix subdivision children positions to prevent scheduler starvation"


def up(conn):
    """Apply the fix: set position=i for children within each parent group."""
    cursor = conn.cursor()
    
    # Get all parents that have children
    cursor.execute("""
        SELECT DISTINCT parent_task_id
        FROM tasks
        WHERE parent_task_id IS NOT NULL
        AND type = 'idea'
    """)
    
    parents = [row[0] for row in cursor.fetchall()]
    
    for parent_id in parents:
        # Get children ordered by created_at
        cursor.execute("""
            SELECT id
            FROM tasks
            WHERE parent_task_id = ?
            AND type = 'idea'
            ORDER BY created_at, id
        """, (parent_id,))
        
        children = [row[0] for row in cursor.fetchall()]
        
        # Update each child with its index as position
        for i, child_id in enumerate(children):
            cursor.execute("""
                UPDATE tasks
                SET position = ?
                WHERE id = ?
            """, (i, child_id))
        
        print(f"Fixed {len(children)} children of {parent_id}")
    
    conn.commit()


def down(conn):
    """Revert: set all children back to position=0."""
    cursor = conn.cursor()
    
    cursor.execute("""
        UPDATE tasks
        SET position = 0
        WHERE parent_task_id IS NOT NULL
        AND type = 'idea'
    """)
    
    count = cursor.rowcount
    conn.commit()
    print(f"Reverted {count} children to position=0")
