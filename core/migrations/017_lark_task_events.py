from __future__ import annotations

import sqlite3


ID = "017_lark_task_events"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE lark_task_state (
          board_id TEXT NOT NULL,
          table_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          status TEXT,
          source_revision TEXT,
          snapshot_json TEXT NOT NULL,
          snapshot_hash TEXT NOT NULL,
          last_event_id TEXT,
          updated_at TEXT NOT NULL,
          PRIMARY KEY (board_id, table_id, record_id),
          FOREIGN KEY (board_id) REFERENCES lark_boards(id) ON DELETE CASCADE
        );

        CREATE TABLE task_events (
          event_key TEXT PRIMARY KEY,
          board_id TEXT NOT NULL,
          workflow_id TEXT NOT NULL,
          table_id TEXT NOT NULL,
          record_id TEXT NOT NULL,
          source_event_id TEXT,
          source_revision TEXT NOT NULL,
          event_type TEXT NOT NULL,
          before_json TEXT,
          after_json TEXT,
          routing_status TEXT NOT NULL DEFAULT 'pending',
          routing_note TEXT,
          routed_at TEXT,
          created_at TEXT NOT NULL,
          FOREIGN KEY (board_id) REFERENCES lark_boards(id) ON DELETE CASCADE,
          FOREIGN KEY (workflow_id) REFERENCES workflows(id)
        );

        CREATE INDEX task_events_record
          ON task_events(board_id, table_id, record_id, created_at);

        CREATE INDEX task_events_pending
          ON task_events(routing_status, created_at);

        CREATE TABLE task_event_deliveries (
          event_key TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          prompt TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          last_error TEXT,
          created_at TEXT NOT NULL,
          delivered_at TEXT,
          PRIMARY KEY (event_key, agent_id),
          FOREIGN KEY (event_key) REFERENCES task_events(event_key) ON DELETE CASCADE,
          FOREIGN KEY (agent_id) REFERENCES agents(id) ON DELETE CASCADE
        );

        CREATE INDEX task_event_deliveries_pending
          ON task_event_deliveries(status, created_at);
        """
    )
