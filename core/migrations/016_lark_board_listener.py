from __future__ import annotations

import sqlite3


ID = "016_lark_board_listener"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_boards)")}
    additions = {
        "listener_status": "TEXT NOT NULL DEFAULT 'unverified'",
        "listener_last_verified_at": "TEXT",
        "listener_failure_kind": "TEXT",
        "listener_last_error": "TEXT",
    }
    for column, definition in additions.items():
        if column not in columns:
            conn.execute(f"ALTER TABLE lark_boards ADD COLUMN {column} {definition}")
