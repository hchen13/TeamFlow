from __future__ import annotations

import sqlite3


ID = "001_initial"


def apply(conn: sqlite3.Connection) -> None:
    existing = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'workspaces'"
    ).fetchone()
    if existing:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(workspaces)")}
        if "enabled" not in columns:
            conn.execute("ALTER TABLE workspaces ADD COLUMN enabled INTEGER NOT NULL DEFAULT 0")
            conn.execute("UPDATE workspaces SET enabled = 1")
    else:
        conn.execute(
            """
            CREATE TABLE workspaces (
              root_path TEXT PRIMARY KEY,
              enabled INTEGER NOT NULL DEFAULT 0,
              updated_at TEXT NOT NULL
            )
            """
        )

    conn.executescript(
        """
        CREATE TABLE lark_event_inbox (
          event_id TEXT PRIMARY KEY,
          brand TEXT NOT NULL,
          app_id TEXT NOT NULL,
          event_type TEXT NOT NULL,
          file_token TEXT,
          table_id TEXT,
          source_revision TEXT,
          payload_json TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          next_attempt_at TEXT,
          last_error TEXT,
          received_at TEXT NOT NULL,
          processed_at TEXT
        );

        CREATE INDEX lark_event_inbox_pending
          ON lark_event_inbox(status, next_attempt_at, received_at);
        """
    )
