from __future__ import annotations

import sqlite3


ID = "021_task_executions"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE task_executions (
          record_id TEXT PRIMARY KEY,
          agent_id TEXT NOT NULL,
          session_id TEXT NOT NULL,
          turn_id TEXT,
          state TEXT NOT NULL,
          stop_status TEXT,
          stopped_by_agent_id TEXT,
          stop_reason TEXT,
          updated_at TEXT NOT NULL,
          stopped_at TEXT
        );
        """
    )
