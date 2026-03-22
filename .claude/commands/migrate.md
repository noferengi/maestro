---
description: Check Maestro DB migration status and apply any pending migrations
---

Check migration status and apply any pending migrations for the Maestro database.

**Do this now — no confirmation needed:**

1. Run the status command and capture the output:
   ```
   venv/Scripts/python.exe app/migrations/runner.py status
   ```

2. Parse the output to identify any migrations with status `pending`.

3. If **all migrations are applied** — report the count of applied migrations and confirm the schema is up to date. Done.

4. If **pending migrations exist** — list them clearly (ID + description), then run:
   ```
   venv/Scripts/python.exe app/migrations/runner.py migrate
   ```

5. After running migrate, run status again to confirm all migrations now show `applied`. Report what was applied and the final state.

6. If the migrate command errors — show the full error output and do NOT attempt to retry or run reset. Surface the error to the user and stop.

Keep output concise: a short table of what was pending, what ran, and a final "all N migrations applied" confirmation.
