from __future__ import annotations

import sqlite3


ID = "004_lark_app_name_synced_at"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_connections)")}
    if columns and "app_name_synced_at" not in columns:
        conn.execute("ALTER TABLE lark_connections ADD COLUMN app_name_synced_at TEXT")
