# TheMaestro - Agent Guide

## Quick Start

### 1. Activate Virtual Environment
```bash
D:\workspace\TheMaestro\venv\Scripts\activate
```

### 2. Install Dependencies (if needed)
```bash
D:\workspace\TheMaestro\venv\Scripts\pip.exe install -r requirements.txt
```

---

## Running the Application

### Start the FastAPI Server
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

The server will start on: **http://localhost:8000**

### Verify Server is Running
```bash
curl http://localhost:8000/api/tasks
```

Expected response: List of tasks (or empty array `[]` if no tasks exist)

### Access the Kanban Board UI
Open your browser to: **http://localhost:8000/kanban.html**

---

## Stopping the Server

### Find the Server Process
```bash
tasklist | findstr uvicorn
```

Example output:
```
uvicorn.exe                   12345   Console
```

### Stop the Server Properly
```bash
taskkill /F /PID 12345
```

Replace `12345` with the actual PID from the tasklist output.

---

## Testing the API

### Create a Task
```bash
curl -X POST http://localhost:8000/api/tasks ^
  -H "Content-Type: application/json" ^
  -d "{\"title\":\"My Task\",\"type\":\"planning\",\"description\":\"Task description\",\"owner\":\"user\",\"tags\":[\"tag1\",\"tag2\"]}"
```

### Get All Tasks
```bash
curl http://localhost:8000/api/tasks
```

### Get a Specific Task
```bash
curl http://localhost:8000/api/tasks/task-1773406739.744445
```

### Update a Task
```bash
curl -X PUT http://localhost:8000/api/tasks/task-1773406739.744445 ^
  -H "Content-Type: application/json" ^
  -d "{\"title\":\"Updated Title\",\"type\":\"development\"}"
```

### Move a Task to Completed
```bash
curl -X PUT http://localhost:8000/api/tasks/task-1773406739.744445 ^
  -H "Content-Type: application/json" ^
  -d "{\"type\":\"completed\"}"
```

### Get Task History
```bash
curl http://localhost:8000/api/tasks/task-1773406739.744445/history
```

### Delete a Task
```bash
curl -X DELETE http://localhost:8000/api/tasks/task-1773406739.744445
```

---

## Running Tests

### Run Persistence Tests
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe test_persistence.py
```

### Run Configuration Tests
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe test_config.py
```

### Run REPL
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe repl.py
```

---

## Database Information

### Database Location
```
D:\workspace\TheMaestro\data\kanban.db
```

### View Database Contents
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -c "import sqlite3; conn = sqlite3.connect('data/kanban.db'); cursor = conn.cursor(); cursor.execute('SELECT * FROM tasks'); print(cursor.fetchall())"
```

---

## File Structure

```
D:\workspace\TheMaestro\
├── app/
│   ├── main.py              # FastAPI application
│   ├── database.py          # Database layer
│   └── web/
│       ├── index.html       # Kanban UI
│       ├── style.css        # Styles
│       └── kanban.js        # Frontend logic
├── data/
│   └── kanban.db            # SQLite database
├── venv/                    # Virtual environment
├── requirements.txt         # Dependencies
└── test_*.py               # Test scripts
```

---

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/tasks` | Get all tasks |
| GET | `/api/tasks/{task_id}` | Get specific task |
| GET | `/api/tasks/by-type/{type}` | Get tasks by column type |
| POST | `/api/tasks` | Create new task |
| PUT | `/api/tasks/{task_id}` | Update task |
| DELETE | `/api/tasks/{task_id}` | Delete task |
| GET | `/api/tasks/{task_id}/history` | Get task history |

---

## Task Types (Columns)

- `planning` - PLANNING column
- `development` - IN PROGRESS column
- `review` - IN REVIEW column
- `completed` - COMPLETED column
- `architecture` - ARCHITECTURE column

---

## Common Commands

### Start Server (Background)
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

### Check Server Status
```bash
curl http://localhost:8000/api/tasks
```

### Stop Server
1. Find PID: `tasklist | findstr uvicorn`
2. Kill: `taskkill /F /PID <PID>`

### Reset Database (Delete and recreate)
```bash
del D:\workspace\TheMaestro\data\kanban.db
D:\workspace\TheMaestro\venv\Scripts\python.exe -c "from app.database import init_db; init_db()"
```

---

## Running the Server

### Background Mode (Recommended for Normal Use)
Starts the server in the background so you can continue using your terminal:

```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

- Server runs in background (PID shown)
- Terminal remains usable for other commands
- Access at: http://localhost:8000/kanban.html

### Foreground Mode (For Debugging Errors)
Starts the server in the foreground to see any errors immediately:

```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -m uvicorn app.main:app --port 8000
```

- Any errors appear directly in the terminal
- Useful when troubleshooting startup issues
- Press `Ctrl+C` to stop the server

### Stopping the Server

**Background Mode:**
```bash
tasklist | findstr uvicorn
taskkill /F /PID <PID>
```

**Foreground Mode:**
Press `Ctrl+C` in the terminal where the server is running

---

## Troubleshooting

### Port Already in Use
Change the port number:
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -m uvicorn app.main:app --port 8002
```

### Import Errors
Ensure you're using the venv Python:
```bash
D:\workspace\TheMaestro\venv\Scripts\activate
```

### Database Not Found
The database is auto-created on first server start. If missing:
```bash
D:\workspace\TheMaestro\venv\Scripts\python.exe -c "from app.database import init_db; init_db()"
```
