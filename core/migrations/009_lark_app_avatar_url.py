from __future__ import annotations

import sqlite3


ID = "009_lark_app_avatar_url"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_identities)")}
    if "app_avatar_url" not in columns:
        conn.execute("ALTER TABLE lark_identities ADD COLUMN app_avatar_url TEXT")
