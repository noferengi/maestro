---
description: Scaffold, check, and apply Maestro DB migrations
---

Two modes depending on whether arguments are provided:

---

## Mode A — Scaffold a new migration (arguments provided)

Arguments: `$ARGUMENTS`

If `$ARGUMENTS` is non-empty, the user wants to create a new migration file. Do this:

1. Run the scaffolder with the provided name:
   ```
   venv/Scripts/python.exe scripts/create_migration.py "$ARGUMENTS"
   ```

2. Print the path it outputs (e.g. `app/migrations/versions/0086_your_name.py`).

3. Stop here. Tell the user to open the file, fill in `up(conn)` and `down(conn)` with PostgreSQL SQL, then run `/migrate` (no args) to apply it.

Do NOT open or edit the file yourself unless the user explicitly asks.

---

## Mode B — Check status and apply pending migrations (no arguments)

### Step 1 — Run status against both DBs

```
venv/Scripts/python.exe app/migrations/runner.py status
```

This is equivalent to `migrate.bat status`. The runner reports two sections: `=== Test DB ===` and `=== Production DB ===`. Both must be clean before the schema is considered up to date.

### Step 2 — Interpret the output

**TAMPERED migrations** (`TAMPERED` in the Status column):
- A migration file was edited after being applied — its current checksum does not match what was recorded.
- Do NOT attempt to re-apply or auto-fix.
- Surface the migration IDs to the user and stop. Tell them: applied migrations must never be edited; add a new corrective migration instead.

**Orphan entries** (applied in DB but no matching file):
- Surface the IDs to the user and stop. The file may have been deleted or renamed. They need to investigate before proceeding.

**All applied, no warnings** — report the count per DB and confirm the schema is up to date. Done.

**Pending migrations exist** — list them (ID + description) for both DBs, then continue to Step 3.

### Step 3 — Apply pending migrations

```
venv/Scripts/python.exe app/migrations/runner.py migrate
```

The runner applies to the test DB first, then the production DB. Both are shown in output.

### Step 4 — Confirm

Run status again:
```
venv/Scripts/python.exe app/migrations/runner.py status
```

Confirm both `=== Test DB ===` and `=== Production DB ===` show all migrations applied with no TAMPERED or orphan warnings. Report what was applied and the final count.

### Step 5 — On error

If the migrate command errors on any DB:
- Show the full error output.
- Do NOT retry or run reset.
- Mention that `rollback` is available to revert the last migration on both DBs if a partial apply needs to be undone:
  ```
  venv/Scripts/python.exe app/migrations/runner.py rollback
  ```
- Surface the error to the user and stop.
