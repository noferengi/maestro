description = "add system settings table"


def up(conn):
    conn.executescript("""
        CREATE TABLE system_settings (
            key VARCHAR PRIMARY KEY,
            value JSON,
            description VARCHAR,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        );
    """)


def down(conn):
    conn.executescript("""
        DROP TABLE system_settings;
    """)
