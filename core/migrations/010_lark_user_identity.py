from __future__ import annotations

import sqlite3


ID = "010_lark_user_identity"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_identities)")}
    if "user_open_id" not in columns:
        conn.execute("ALTER TABLE lark_identities ADD COLUMN user_open_id TEXT")
    if "user_name" not in columns:
        conn.execute("ALTER TABLE lark_identities ADD COLUMN user_name TEXT")
