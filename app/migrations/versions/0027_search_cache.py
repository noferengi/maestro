description = "Create search_cache table for web search results"


def up(conn):
    conn.execute("""
        CREATE TABLE search_cache (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            query        TEXT NOT NULL UNIQUE,
            result_json  TEXT NOT NULL,
            provider     TEXT NOT NULL DEFAULT 'brave',
            created_at   DATETIME NOT NULL DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX idx_search_cache_query ON search_cache(query)")
    conn.commit()


def down(conn):
    conn.execute("DROP TABLE search_cache")
    conn.commit()
