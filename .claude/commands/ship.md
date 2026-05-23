---
description: After a successful code change — run tests, commit, restart the server, and tail the log for unusual behavior. Never pushes.
---

You are running the **ship** workflow for Project Maestro. This seals a completed code change: tests → commit → server restart → log observation. No push — push is a deliberate, separate act.

**Do this now — no confirmation needed unless tests fail.**

---

## Step 1 — Run the full test suite

```
venv/Scripts/python.exe -m pytest app/tests/ -v
```

**If any tests fail:** stop immediately. Report the failing test names and tracebacks. Do NOT commit or restart. The change is not shippable.

**If all tests pass:** continue to Step 2.

---

## Step 2 — Stage and commit

Check what changed:

```
git status
git diff --stat HEAD
```

Stage all modified tracked files plus any new files that are part of this change. Prefer naming files explicitly over `git add -A` — do not accidentally stage `.env`, secrets, or large binaries.

Write the commit message to a temp file (Windows PowerShell EOL is unreliable for multiline `-m`):

```powershell
Set-Content -Path $env:TEMP\commit_msg.txt -Value "<message>" -Encoding UTF8
git commit -F $env:TEMP\commit_msg.txt
Remove-Item $env:TEMP\commit_msg.txt
```

Commit message rules:
- First line: `<type>: <what changed and why>` (≤72 chars). Types: `fix`, `feat`, `refactor`, `test`, `chore`.
- If context warrants it, add a blank line then a short paragraph explaining the root cause or motivation.
- No bullet lists of file names. No "Updated X to do Y" summaries of what the diff already shows.
- Do NOT push. Push is a separate, deliberate act reserved for special occasions.

Confirm the commit landed:

```
git log --oneline -3
```

---

## Step 3 — Restart the server

```
mcp__maestro__restart_server()
```

The server drains active sessions (up to 55 s) before restarting. Poll until it's back:

```bash
until curl -sf http://localhost:8000/api/projects > /dev/null 2>&1; do sleep 3; done && echo "Server up"
```

---

## Step 4 — Tail the log and report

Read the last 60 lines of `logs/maestro.log`. Look for:

| Signal | Meaning |
|---|---|
| `ERROR` lines | Exceptions in the new code path |
| `WARNING` lines that weren't there before | Unexpected state |
| Sessions closed unexpectedly (zombie sweep firing) | Regression in session lifecycle |
| Stage dispatch for any task in the first two ticks | Scheduler healthy |
| `Scheduler started` + `Session heartbeat thread started` | Clean boot |

Report:
- Whether the boot sequence looks clean
- Any ERROR or unexpected WARNING lines
- The first task dispatch seen post-restart (confirms scheduler is alive)
- One-line verdict: **clean** or **investigate: <what>**

If the log shows a regression introduced by this change, diagnose and fix it before declaring the ship complete.
