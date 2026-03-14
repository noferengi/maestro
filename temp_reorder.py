"""
Test script to verify task reordering works correctly.
"""

from app.database import get_tasks_by_type, reorder_tasks, get_all_tasks

def test_reordering():
    print("=== Testing Task Reordering ===\n")
    
    # Test 1: Get planning tasks
    print("1. Getting planning tasks...")
    planning_tasks = get_tasks_by_type('planning')
    print(f"   Found {len(planning_tasks)} planning tasks")
    for task in planning_tasks:
        print(f"      - {task.id}: position={task.position}")
    
    # Test 2: Reorder first task to last position
    print("\n2. Reordering planning-1 to last position (position 3)...")
    result = reorder_tasks('planning-1', 3, 'planning')
    print(f"   Result: {result}")
    
    # Test 3: Verify reordering
    print("\n3. Verifying reordering...")
    planning_tasks = get_tasks_by_type('planning')
    for task in planning_tasks:
        print(f"      - {task.id}: position={task.position}")
    
    # Test 4: Reorder back
    print("\n4. Reordering planning-1 back to position 0...")
    result = reorder_tasks('planning-1', 0, 'planning')
    print(f"   Result: {result}")
    
    # Test 5: Verify
    print("\n5. Verifying final order...")
    planning_tasks = get_tasks_by_type('planning')
    for task in planning_tasks:
        print(f"      - {task.id}: position={task.position}")
    
    print("\n=== Test Complete ===")

if __name__ == '__main__':
    test_reordering()
