#!/usr/bin/env python3
"""
Usage: python scripts/create_migration.py <migration_name>

Creates the next numbered migration file in app/migrations/versions/.
Prints the path of the created file.

Examples:
  python scripts/create_migration.py add_clarification_status
  python scripts/create_migration.py "add acceptance criteria column"
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
    # SQLite has no DROP COLUMN before 3.35.
    # If adding columns: recreate the table(s) without them.
    # If creating tables: drop them.
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
