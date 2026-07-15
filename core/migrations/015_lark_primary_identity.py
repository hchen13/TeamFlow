from __future__ import annotations

import sqlite3


ID = "015_lark_primary_identity"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_identities)")}
    if "is_default" in columns:
        conn.execute("ALTER TABLE lark_identities DROP COLUMN is_default")
