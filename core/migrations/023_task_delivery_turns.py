from __future__ import annotations

import sqlite3


ID = "023_task_delivery_turns"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE task_delivery_turns (
          delivery_id INTEGER NOT NULL,
          turn_id TEXT NOT NULL,
          created_at TEXT NOT NULL,
          PRIMARY KEY (delivery_id, turn_id),
          FOREIGN KEY (delivery_id)
            REFERENCES task_event_deliveries(id) ON DELETE CASCADE
        );

        INSERT INTO task_delivery_turns (delivery_id, turn_id, created_at)
        SELECT id, turn_id, COALESCE(started_at, created_at)
        FROM task_event_deliveries
        WHERE turn_id IS NOT NULL;

        CREATE INDEX task_delivery_turns_turn
          ON task_delivery_turns(turn_id, delivery_id);
        """
    )
