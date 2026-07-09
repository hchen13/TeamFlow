from __future__ import annotations

import sqlite3


ID = "005_lark_default_identity"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_connections)")}
    if not columns:
        return
    if "is_default" not in columns:
        conn.execute("ALTER TABLE lark_connections ADD COLUMN is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1))")
    for row in conn.execute("""
        SELECT workspace_id, id
        FROM lark_connections
        WHERE auth_mode = 'bot' AND app_id IS NOT NULL AND is_default = 0
        ORDER BY updated_at DESC
    """):
        existing = conn.execute(
            "SELECT id FROM lark_connections WHERE workspace_id = ? AND is_default = 1",
            (row["workspace_id"],),
        ).fetchone()
        if not existing:
            conn.execute("UPDATE lark_connections SET is_default = 1 WHERE id = ?", (row["id"],))
