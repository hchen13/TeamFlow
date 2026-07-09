from __future__ import annotations

import sqlite3


ID = "003_lark_app_name"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_connections)")}
    if columns and "app_name" not in columns:
        conn.execute("ALTER TABLE lark_connections ADD COLUMN app_name TEXT")
