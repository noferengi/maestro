import json

description = "seed autopilot system settings"


def up(conn):
    rows = [
        ("maestro_autopilot",    "off",  "Global autopilot switch: on|off"),
        ("autopilot_start_hour", 0,      "Hour (0-23) when autopilot schedule activates"),
        ("autopilot_stop_hour",  24,     "Hour (0-24) when autopilot schedule deactivates; 24 = always active"),
    ]
    for key, value, desc in rows:
        json_value = json.dumps(value)
        if conn.is_postgres:
            # Use CAST() to avoid :: operator conflicts with named param parser
            conn.execute(
                f"""
                INSERT INTO system_settings (key, value, description)
                VALUES ('{key}', CAST('{json_value}' AS jsonb), '{desc}')
                ON CONFLICT (key) DO NOTHING
                """,
            )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO system_settings (key, value, description) VALUES (?, ?, ?)",
                [key, json_value, desc],
            )


def down(conn):
    keys = ["maestro_autopilot", "autopilot_start_hour", "autopilot_stop_hour"]
    for key in keys:
        conn.execute(
            f"DELETE FROM system_settings WHERE key = '{key}'" if conn.is_postgres
            else "DELETE FROM system_settings WHERE key = ?",
            [] if conn.is_postgres else [key],
        )
