"""
Test script for Kanban persistence
"""
import requests
import json

BASE_URL = "http://localhost:8001"

def test_create_task():
    """Test creating a new task"""
    task_data = {
        "title": "Test Task",
        "type": "planning",
        "description": "This is a test task",
        "owner": "user",
        "tags": ["test"]
    }
    
    response = requests.post(f"{BASE_URL}/api/tasks", json=task_data)
    print(f"Create task status: {response.status_code}")
    
    if response.status_code == 200:
        task = response.json()
        print(f"Created task: {task['title']} (ID: {task['id']})")
        return task['id']
    else:
        print(f"Error: {response.text}")
        return None

def test_get_tasks():
    """Test getting all tasks"""
    response = requests.get(f"{BASE_URL}/api/tasks")
    print(f"Get tasks status: {response.status_code}")
    
    if response.status_code == 200:
        tasks = response.json()
        print(f"Total tasks: {len(tasks)}")
        for task in tasks:
            print(f"  - {task['title']} ({task['type']})")
    else:
        print(f"Error: {response.text}")

def test_update_task(task_id):
    """Test updating a task"""
    task_data = {
        "title": "Updated Test Task",
        "description": "This task has been updated"
    }
    
    response = requests.put(f"{BASE_URL}/api/tasks/{task_id}", json=task_data)
    print(f"Update task status: {response.status_code}")
    
    if response.status_code == 200:
        task = response.json()
        print(f"Updated task: {task['title']}")
    else:
        print(f"Error: {response.text}")

def test_move_task(task_id):
    """Test moving a task to a different status"""
    response = requests.put(f"{BASE_URL}/api/tasks/{task_id}", json={"type": "completed"})
    print(f"Move task status: {response.status_code}")
    
    if response.status_code == 200:
        task = response.json()
        print(f"Moved task to: {task['type']}")
    else:
        print(f"Error: {response.text}")

def test_get_task_history(task_id):
    """Test getting task history"""
    response = requests.get(f"{BASE_URL}/api/tasks/{task_id}/history")
    print(f"Get history status: {response.status_code}")
    
    if response.status_code == 200:
        data = response.json()
        print(f"Task history for {task_id}:")
        for entry in data['history']:
            print(f"  - {entry['status']} at {entry['timestamp']}")
    else:
        print(f"Error: {response.text}")

if __name__ == "__main__":
    print("=" * 60)
    print("Testing Kanban Board Persistence")
    print("=" * 60)
    
    # Test 1: Create a task
    print("\n1. Creating a new task...")
    task_id = test_create_task()
    
    if task_id:
        # Test 2: Get all tasks
        print("\n2. Getting all tasks...")
        test_get_tasks()
        
        # Test 3: Update the task
        print("\n3. Updating the task...")
        test_update_task(task_id)
        
        # Test 4: Get all tasks again
        print("\n4. Getting all tasks after update...")
        test_get_tasks()
        
        # Test 5: Move the task to completed
        print("\n5. Moving task to COMPLETED...")
        test_move_task(task_id)
        
        # Test 6: Get task history
        print("\n6. Getting task history...")
        test_get_task_history(task_id)
        
        # Test 7: Get all tasks one more time
        print("\n7. Final task list...")
        test_get_tasks()
        
        print("\n" + "=" * 60)
        print("All tests completed!")
        print("=" * 60)
    else:
        print("\nFailed to create task. Exiting.")
