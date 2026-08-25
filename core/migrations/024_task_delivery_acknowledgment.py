from __future__ import annotations

import sqlite3


ID = "024_task_delivery_acknowledgment"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "ALTER TABLE task_delivery_turns ADD COLUMN acknowledged_at TEXT"
    )
