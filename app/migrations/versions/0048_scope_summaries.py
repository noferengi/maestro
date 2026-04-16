"""
0048_scope_summaries.py
-----------------------
Create table for hierarchical project scope summaries.
"""

description = "Create scope_summaries table"

def up(conn):
    conn.execute("""
        CREATE TABLE scope_summaries (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            project_name    TEXT NOT NULL,
            scope_type      TEXT NOT NULL,              -- 'directory' | 'module' | 'collection' | 'project'
            scope_key       TEXT NOT NULL,              -- rel_dir for directories, assigned name for modules, '__ROOT__' for project
            parent_scope_key TEXT,                      -- enables hierarchy navigation
            depth           INTEGER NOT NULL DEFAULT 0, -- 0=directory/module, N=collection depth
            summary         TEXT NOT NULL,
            short_summary   TEXT,                       -- 2-sentence version for use as input to next level
            file_paths      TEXT,                       -- JSON array of absolute paths in this scope
            file_count      INTEGER NOT NULL DEFAULT 0,
            content_hash    TEXT,                       -- SHA1 of sorted file content hashes (staleness key)
            git_commit      TEXT,                       -- HEAD at generation time
            staleness_state TEXT NOT NULL DEFAULT 'fresh', -- 'fresh' | 'stale' | 'checking'
            llm_id          INTEGER,
            budget_id       INTEGER,
            created_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at      DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(project_name, scope_type, scope_key)
        )
    """)
    conn.commit()

def down(conn):
    conn.execute("DROP TABLE scope_summaries")
    conn.commit()
