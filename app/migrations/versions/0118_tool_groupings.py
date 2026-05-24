description = "tool_groupings table with 4 builtin rows"

import json as _json


_READONLY_CODE_TOOLS = [
    "read_file", "read_file_metadata", "read_last_output",
    "list_directory", "find_in_files", "find_files", "find_symbol",
    "submit_work",
]

_SECURITY_REVIEW_TOOLS = _READONLY_CODE_TOOLS + [
    "run_audit_bandit", "run_audit_pip", "run_audit_semgrep", "run_audit_npm",
    "read_git_diff", "read_diff_stat",
]

_CODE_REVIEW_TOOLS = [
    "read_file", "read_file_metadata", "list_directory",
    "find_in_files", "find_files", "find_symbol",
    "read_git_diff", "read_diff_stat",
    "submit_work",
]


def up(conn):
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_groupings (
            id          SERIAL PRIMARY KEY,
            name        VARCHAR(120) NOT NULL,
            description TEXT,
            tools       JSONB NOT NULL DEFAULT '[]',
            is_builtin  BOOLEAN NOT NULL DEFAULT FALSE,
            created_at  TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT uq_tool_grouping_name UNIQUE (name)
        )
    """)

    from app.agent.config import INDEV_AGENT_TOOLS

    builtins = [
        {
            "name": "Read-Only Code",
            "description": "Read source files, search for symbols, and submit results. No writes.",
            "tools": _READONLY_CODE_TOOLS,
        },
        {
            "name": "Security Review",
            "description": "Read-only plus security audit tools (bandit, pip-audit, semgrep, npm audit) and git diff.",
            "tools": _SECURITY_REVIEW_TOOLS,
        },
        {
            "name": "Code Review",
            "description": "Read source, search, read git diff and diff stats, submit results.",
            "tools": _CODE_REVIEW_TOOLS,
        },
        {
            "name": "Full Indev",
            "description": "Full implementation agent tool set (same as MaestroLoop indev stage).",
            "tools": list(INDEV_AGENT_TOOLS),
        },
    ]

    for row in builtins:
        existing = conn.execute(
            "SELECT id FROM tool_groupings WHERE name = :name",
            {"name": row["name"]},
        ).fetchone()
        if existing:
            continue
        conn.execute(
            "INSERT INTO tool_groupings (name, description, tools, is_builtin) "
            "VALUES (:name, :desc, :tools, TRUE)",
            {
                "name": row["name"],
                "desc": row["description"],
                "tools": _json.dumps(row["tools"]),
            },
        )
        print(f"[0118] Inserted builtin tool grouping: {row['name']!r}")

    print("[0118] tool_groupings table ready.")


def down(conn):
    conn.execute("DROP TABLE IF EXISTS tool_groupings")
