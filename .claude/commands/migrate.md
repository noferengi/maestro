---
description: Scaffold, check, and apply Maestro DB migrations
---

Three modes depending on whether arguments are provided:

---

## Mode A — Scaffold a new migration (name argument provided)

Arguments: `$ARGUMENTS`

If `$ARGUMENTS` is non-empty and does **not** start with `rehash`, the user wants to create a new migration file. Do this:

1. Run the scaffolder with the provided name:
   ```
   venv/Scripts/python.exe scripts/create_migration.py "$ARGUMENTS"
   ```

2. Print the path it outputs (e.g. `app/migrations/versions/0086_your_name.py`).

3. Stop here. Tell the user to open the file, fill in `up(conn)` and `down(conn)` with PostgreSQL SQL, then run `/migrate` (no args) to apply it.

Do NOT open or edit the file yourself unless the user explicitly asks.

---

## Mode B — Rehash stored checksums (`rehash NNNN [NNNN...]`)

If `$ARGUMENTS` starts with `rehash`, the user wants to re-stamp the stored checksum(s) for one or more already-applied migrations.

**When to use:** Only when a migration file was edited for cosmetic reasons only — adding or fixing a `description` variable, fixing a comment — and the `up()` and `down()` functions are **byte-for-byte identical** to what was applied. Rehash after any behavioural change is falsifying history and must be refused.

**Before running rehash, you MUST:**

1. Read the listed migration file(s) and verify the diff is truly cosmetic: no change to SQL statements, no new or removed operations in `up()` or `down()`. If you cannot confirm this, refuse and tell the user to add a new corrective migration instead.

2. Extract the migration IDs from the arguments (e.g. `rehash 0079 0080` → IDs `0079 0080`).

3. Run:
   ```
   venv/Scripts/python.exe app/migrations/runner.py rehash <ID> [<ID> ...]
   ```
   This re-stamps both the test DB and the production DB in one command.

4. Report which IDs were rehashed and what the old → new checksum transition was (the runner prints this). Confirm both DBs are now clean by running status.

If the user asks to rehash a migration whose `up()` or `down()` was changed, refuse and explain: the correct fix is a new corrective migration, not falsifying the stored checksum.

---

## Mode C — Check status and apply pending migrations (no arguments)

### Step 1 — Run status against both DBs

```
venv/Scripts/python.exe app/migrations/runner.py status
```

This is equivalent to `migrate.bat status`. The runner reports two sections: `=== Test DB ===` and `=== Production DB ===`. Both must be clean before the schema is considered up to date.

### Step 2 — Interpret the output

**TAMPERED migrations** (`TAMPERED` in the Status column):
- A migration file was edited after being applied — its current checksum does not match what was recorded.
- Read the affected file(s) and check whether the change is purely cosmetic (description variable, a comment) with **no change** to `up()` or `down()`.
  - **Cosmetic only:** inform the user and offer to run `rehash` (Mode B above) to re-stamp the checksum. Wait for explicit approval before doing so.
  - **Behavioural change detected (up/down modified):** Do NOT rehash. Surface the IDs and tell the user: applied migrations must never be edited for behaviour; add a new corrective migration instead.
- Never rehash without first confirming the diff is cosmetic.

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
