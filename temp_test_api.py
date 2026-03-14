import requests
import json

# Test the reorder API
response = requests.post(
    "http://localhost:8000/api/tasks/planning-1/reorder",
    json={"position": 2, "type": "planning"}
)

print(f"Status: {response.status_code}")
print(f"Response: {response.json()}")
