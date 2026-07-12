from __future__ import annotations

import sqlite3


ID = "012_agent_runtime_ephemeral"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(agents)")}
    for column in ("status", "last_verified_at", "last_error", "session_name"):
        if column in columns:
            conn.execute(f"ALTER TABLE agents DROP COLUMN {column}")
    conn.execute("DELETE FROM migrations WHERE id IN ('012_agent_health', '013_agent_session_name')")
