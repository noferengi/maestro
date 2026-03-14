"""
Test script to simulate frontend drag-and-drop reordering.
"""

from app.database import get_tasks_by_type, reorder_tasks, get_all_tasks

def simulate_drag_drop(dragged_id, target_id, task_type):
    """
    Simulates dragging one task and dropping it onto another.
    
    Args:
        dragged_id: ID of the task being dragged
        target_id: ID of the task we're dropping onto
        task_type: The type of column (planning, development, etc.)
    
    Returns:
        New order of tasks
    """
    print(f"\n=== Simulating: Drag {dragged_id} onto {target_id} ===")
    
    # Get all tasks of this type
    tasks = get_tasks_by_type(task_type)
    print(f"Tasks before: {[t.id for t in tasks]}")
    
    # Find indices
    dragged_index = None
    target_index = None
    
    for i, task in enumerate(tasks):
        if task.id == dragged_id:
            dragged_index = i
        if task.id == target_id:
            target_index = i
    
    print(f"Dragged index: {dragged_index}")
    print(f"Target index: {target_index}")
    
    # Calculate the new position (same as frontend)
    if dragged_index < target_index:
        # Moving down - insert AFTER target, new position = targetIndex + 1
        new_position = target_index + 1
    else:
        # Moving up - insert BEFORE target, new position = targetIndex
        new_position = target_index
    
    print(f"Calculated new position: {new_position}")
    
    # Reorder
    result = reorder_tasks(dragged_id, new_position, task_type)
    print(f"Reorder result: {result}")
    
    # Verify
    tasks = get_tasks_by_type(task_type)
    print(f"Tasks after: {[t.id for t in tasks]}")
    print(f"Positions: {[t.position for t in tasks]}")
    
    return tasks

if __name__ == '__main__':
    # Reset to initial state
    from app.database import seed_sample_tasks
    from app.database import init_db, init_db_tables
    is_fresh = init_db()
    if is_fresh:
        init_db_tables()
        seed_sample_tasks()
    
    # Test 1: Drag planning-1 (index 0) onto planning-2 (index 1)
    simulate_drag_drop('planning-1', 'planning-2', 'planning')
    
    # Test 2: Drag planning-1 (now at index 1) onto planning-3 (index 2)
    simulate_drag_drop('planning-1', 'planning-3', 'planning')
    
    # Test 3: Drag planning-3 (index 2) onto planning-1 (index 0)
    simulate_drag_drop('planning-3', 'planning-1', 'planning')
