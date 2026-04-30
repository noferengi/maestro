"""
Migration Test Runner CLI for TheMaestro Kanban DB.

Provides command-line interface for testing individual migrations in isolation.

Usage:
    python app/migrations/test_runner.py test <migration_id> <migration_file> [--db <path>]
    python app/migrations/test_runner.py test-rollback <migration_id> <migration_file> [--db <path>]
    python app/migrations/test_runner.py test-creates <migration_file> --tables <table1> <table2> ... [--db <path>]
    python app/migrations/test_runner.py status <db_path>
    python app/migrations/test_runner.py list
    python app/migrations/test_runner.py help
"""

import argparse
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from app.migrations.test_framework import (
    load_migration,
    create_fresh_db,
    execute_migration,
    get_migration_status,
    test_migration_isolation,
    test_migration_creates_expected_tables,
    test_migration_rollback_safe,
    get_applied_migrations,
)
from app.migrations.runner import get_all_migrations, MIGRATIONS_DIR


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Test migration scripts in isolation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Test a single migration in isolation
  python test_runner.py test 0001 app/migrations/versions/0001_initial_schema.py

  # Test with custom database path
  python test_runner.py test 0001 app/migrations/versions/0001_initial_schema.py --db test.db

  # Test that a migration creates specific tables
  python test_runner.py test-creates app/migrations/versions/0001_initial_schema.py --tables tasks

  # Test migration and rollback
  python test_runner.py test-rollback 0001 app/migrations/versions/0001_initial_schema.py --tables tasks --after-rollback
        """
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Test command
    test_parser = subparsers.add_parser("test", help="Test a single migration in isolation")
    test_parser.add_argument("migration_id", help="Migration ID (NNNN prefix)")
    test_parser.add_argument("migration_file", help="Path to migration file")
    test_parser.add_argument("--db", default=None, help="Database file path (default: temp)")
    test_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # Test rollback command
    rollback_parser = subparsers.add_parser("test-rollback", help="Test migration and rollback")
    rollback_parser.add_argument("migration_id", help="Migration ID (NNNN prefix)")
    rollback_parser.add_argument("migration_file", help="Path to migration file")
    rollback_parser.add_argument("--db", default=None, help="Database file path (default: temp)")
    rollback_parser.add_argument("--before-tables", nargs="+", help="Tables that should exist before migration")
    rollback_parser.add_argument("--after-tables", nargs="+", help="Tables that should exist after rollback")
    rollback_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # Test creates command
    creates_parser = subparsers.add_parser("test-creates", help="Test that migration creates expected tables")
    creates_parser.add_argument("migration_file", help="Path to migration file")
    creates_parser.add_argument("--db", default=None, help="Database file path (default: temp)")
    creates_parser.add_argument("--tables", nargs="+", required=True, help="Expected table names")
    creates_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show migration status in a database")
    status_parser.add_argument("db_path", help="Database file path")
    status_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    # List command
    list_parser = subparsers.add_parser("list", help="List all available migrations")
    list_parser.add_argument("--verbose", "-v", action="store_true", help="Verbose output")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    try:
        if args.command == "test":
            run_test(args)
        elif args.command == "test-rollback":
            run_test_rollback(args)
        elif args.command == "test-creates":
            run_test_creates(args)
        elif args.command == "status":
            run_status(args)
        elif args.command == "list":
            run_list(args)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


# ---------------------------------------------------------------------------
# Test commands
# ---------------------------------------------------------------------------

def run_test(args):
    """Run isolation test on a single migration."""
    db_path = Path(args.db) if args.db else None

    if db_path is None:
        # Create temp database
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        import os
        os.close(fd)
        db_path = Path(path)
        print(f"Using temporary database: {db_path}")

    print(f"Testing migration: {args.migration_id}")
    print(f"Migration file: {args.migration_file}")

    try:
        migration = load_migration(args.migration_file)

        # Verify migration ID
        expected_id = args.migration_file.stem.split("_")[0]
        if expected_id != args.migration_id:
            print(f"ERROR: Migration ID mismatch: expected {args.migration_id}, got {expected_id}")
            sys.exit(1)

        # Create fresh database
        conn = create_fresh_db(db_path)

        try:
            # Apply migration
            print("Applying migration...")
            execute_migration(conn, migration, "up")
            print("✓ Migration applied successfully")

            # Verify status
            status = get_migration_status(conn)
            applied_ids = [s["migration_id"] for s in status]
            if args.migration_id not in applied_ids:
                print(f"ERROR: Migration {args.migration_id} was not applied")
                sys.exit(1)

            if args.verbose:
                print(f"  Applied migrations: {applied_ids}")

            # Rollback
            print("Rolling back migration...")
            execute_migration(conn, migration, "down")
            print("✓ Migration rolled back successfully")

            # Verify rollback
            status = get_migration_status(conn)
            applied_ids = [s["migration_id"] for s in status]
            if args.migration_id in applied_ids:
                print(f"ERROR: Migration {args.migration_id} was not rolled back")
                sys.exit(1)

            print("✓ Isolation test PASSED")

        finally:
            conn.close()

    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Migration test failed: {e}")
        sys.exit(1)


def run_test_rollback(args):
    """Test migration and rollback with table state verification."""
    db_path = Path(args.db) if args.db else None

    if db_path is None:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        import os
        os.close(fd)
        db_path = Path(path)
        print(f"Using temporary database: {db_path}")

    print(f"Testing migration and rollback: {args.migration_id}")
    print(f"Migration file: {args.migration_file}")

    try:
        migration = load_migration(args.migration_file)

        # Verify migration ID
        expected_id = args.migration_file.stem.split("_")[0]
        if expected_id != args.migration_id:
            print(f"ERROR: Migration ID mismatch: expected {args.migration_id}, got {expected_id}")
            sys.exit(1)

        # Create fresh database
        conn = create_fresh_db(db_path)

        try:
            # Apply migration
            print("Applying migration...")
            execute_migration(conn, migration, "up")
            print("✓ Migration applied successfully")

            # Check state after migration
            if args.before_tables:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                created_tables = {row["name"] for row in tables}

                for table in args.before_tables:
                    if table not in created_tables:
                        print(f"ERROR: Expected table '{table}' missing after migration")
                        sys.exit(1)

                if args.verbose:
                    print(f"  Tables after migration: {sorted(created_tables)}")

            # Rollback
            print("Rolling back migration...")
            execute_migration(conn, migration, "down")
            print("✓ Migration rolled back successfully")

            # Check state after rollback
            if args.after_tables:
                tables = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
                ).fetchall()
                created_tables = {row["name"] for row in tables}

                for table in args.after_tables:
                    if table not in created_tables:
                        print(f"ERROR: Expected table '{table}' missing after rollback")
                        sys.exit(1)

                if args.verbose:
                    print(f"  Tables after rollback: {sorted(created_tables)}")

            print("✓ Rollback test PASSED")

        finally:
            conn.close()

    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Migration test failed: {e}")
        sys.exit(1)


def run_test_creates(args):
    """Test that a migration creates expected tables."""
    db_path = Path(args.db) if args.db else None

    if db_path is None:
        import tempfile
        fd, path = tempfile.mkstemp(suffix=".db")
        import os
        os.close(fd)
        db_path = Path(path)
        print(f"Using temporary database: {db_path}")

    print(f"Testing table creation: {args.migration_file}")
    print(f"Expected tables: {args.tables}")

    try:
        migration = load_migration(args.migration_file)

        # Create fresh database
        conn = create_fresh_db(db_path)

        try:
            # Apply migration
            print("Applying migration...")
            execute_migration(conn, migration, "up")
            print("✓ Migration applied successfully")

            # Check for expected tables
            tables = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
            created_tables = {row["name"] for row in tables}

            for table in args.tables:
                if table not in created_tables:
                    print(f"ERROR: Expected table '{table}' was not created")
                    sys.exit(1)

            if args.verbose:
                print(f"  All tables after migration: {sorted(created_tables)}")

            print("✓ Table creation test PASSED")

        finally:
            conn.close()

    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Migration test failed: {e}")
        sys.exit(1)


# ---------------------------------------------------------------------------
# Status and list commands
# ---------------------------------------------------------------------------

def run_status(args):
    """Show migration status in a database."""
    db_path = Path(args.db_path)

    if not db_path.exists():
        print(f"ERROR: Database file not found: {db_path}")
        sys.exit(1)

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        # Get applied migrations
        applied = get_applied_migrations(conn)

        # Get all known migrations
        all_migrations = get_all_migrations()
        all_ids = {mid for mid, _ in all_migrations}

        # Print status
        print(f"{'ID':<8}  {'Status':<10}  {'Applied At':<26}  Description")
        print("-" * 72)

        for migration_id, mod in all_migrations:
            desc = getattr(mod, "description", "")
            if migration_id in applied:
                state = "applied"
                applied_at = applied[migration_id]
            else:
                state = "pending"
                applied_at = ""

            print(f"{migration_id:<8}  {state:<10}  {applied_at:<26}  {desc}")

        # Warn about orphaned entries
        known_ids = {mid for mid, _ in all_migrations}
        orphans = [mid for mid in applied if mid not in known_ids]
        if orphans:
            print()
            print("WARNING: The following applied migrations have no matching file:")
            for oid in orphans:
                print(f"  {oid}")

    finally:
        conn.close()


def run_list(args):
    """List all available migrations."""
    migrations = get_all_migrations()

    if not migrations:
        print("No migrations found in app/migrations/versions/")
        return

    print(f"Found {len(migrations)} migration(s):")
    print()

    for migration_id, mod in migrations:
        desc = getattr(mod, "description", "")
        if not desc:
            desc = migration_id

        print(f"  {migration_id}: {desc}")

    if args.verbose:
        print()
        print("Migration files:")
        for migration_id, mod in migrations:
            path = MIGRATIONS_DIR / f"{migration_id}.py"
            print(f"  {path}")


if __name__ == "__main__":
    main()
