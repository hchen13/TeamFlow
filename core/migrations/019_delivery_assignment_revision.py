from __future__ import annotations

import sqlite3


ID = "019_delivery_assignment_revision"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE task_event_deliveries RENAME TO task_event_deliveries_legacy;

        CREATE TABLE task_event_deliveries (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          event_key TEXT NOT NULL,
          agent_id TEXT NOT NULL,
          assignment_revision INTEGER NOT NULL,
          harness_type TEXT NOT NULL,
          session_id TEXT NOT NULL,
          prompt TEXT NOT NULL,
          status TEXT NOT NULL DEFAULT 'pending',
          attempts INTEGER NOT NULL DEFAULT 0,
          turn_id TEXT,
          turn_status TEXT,
          last_error TEXT,
          next_attempt_at TEXT,
          created_at TEXT NOT NULL,
          started_at TEXT,
          completed_at TEXT,
          delivered_at TEXT,
          UNIQUE (event_key, agent_id, assignment_revision),
          FOREIGN KEY (event_key) REFERENCES task_events(event_key) ON DELETE CASCADE
        );

        INSERT INTO task_event_deliveries (
          event_key, agent_id, assignment_revision, harness_type, session_id,
          prompt, status, attempts, turn_id, turn_status, last_error,
          next_attempt_at, created_at, started_at, completed_at, delivered_at
        )
        SELECT delivery.event_key,
               delivery.agent_id,
               COALESCE(agent.assignment_revision, 1),
               delivery.harness_type,
               delivery.session_id,
               delivery.prompt,
               delivery.status,
               delivery.attempts,
               delivery.turn_id,
               delivery.turn_status,
               delivery.last_error,
               delivery.next_attempt_at,
               delivery.created_at,
               delivery.started_at,
               delivery.completed_at,
               delivery.delivered_at
        FROM task_event_deliveries_legacy AS delivery
        LEFT JOIN agents AS agent ON agent.id = delivery.agent_id;

        DROP TABLE task_event_deliveries_legacy;

        CREATE INDEX task_event_deliveries_pending
          ON task_event_deliveries(status, next_attempt_at, created_at);

        CREATE INDEX task_event_deliveries_session
          ON task_event_deliveries(session_id, status, created_at);
        """
    )
