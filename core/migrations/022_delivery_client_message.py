from __future__ import annotations

import sqlite3


ID = "022_delivery_client_message"


def apply(conn: sqlite3.Connection) -> None:
    conn.execute(
        "ALTER TABLE task_event_deliveries ADD COLUMN client_message_id TEXT"
    )
