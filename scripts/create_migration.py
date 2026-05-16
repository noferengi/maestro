#!/usr/bin/env python3
"""
Maestro Migration Creator (PostgreSQL only)
-------------------------------------------
Creates a new numbered migration file template in app/migrations/versions/.

Usage:
    python scripts/create_migration.py "<migration_description>"

Next Steps:
    1. Open the created file (path printed below).
    2. Edit the 'up(conn)' function with your PostgreSQL SQL changes.
    3. Edit the 'down(conn)' function with the rollback SQL.
    4. Run '.\migrate.bat status' to verify the new file is detected as 'pending'.
    5. Run '.\migrate.bat migrate' to apply the changes.

Example:
    python scripts/create_migration.py "add clarification status to tasks"
"""

import re
import sys
from pathlib import Path

VERSIONS_DIR = Path(__file__).parent.parent / "app" / "migrations" / "versions"

TEMPLATE = '''\
description = "{description}"


def up(conn):
    conn.executescript("""
        -- TODO: write your migration SQL here
    """)


def down(conn):
    # To undo column additions: ALTER TABLE ... DROP COLUMN ...
    # To undo table creation: DROP TABLE IF EXISTS ...
    conn.executescript("""
        -- TODO: write your rollback SQL here
    """)
'''


def slugify(name: str) -> str:
    name = name.strip().lower()
    name = re.sub(r"[^a-z0-9]+", "_", name)
    return name.strip("_")


def next_number() -> int:
    numbers = []
    for f in VERSIONS_DIR.iterdir():
        m = re.match(r"^(\d{4})_", f.name)
        if m:
            numbers.append(int(m.group(1)))
    return (max(numbers) + 1) if numbers else 1


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    raw_name = " ".join(sys.argv[1:])
    slug = slugify(raw_name)
    if not slug:
        print("Error: migration name must contain at least one alphanumeric character.")
        sys.exit(1)

    num = next_number()
    filename = f"{num:04d}_{slug}.py"
    path = VERSIONS_DIR / filename

    if path.exists():
        print(f"Error: {path} already exists.")
        sys.exit(1)

    description = slug.replace("_", " ")
    path.write_text(TEMPLATE.format(description=description), encoding="utf-8")
    print(path)


if __name__ == "__main__":
    main()
