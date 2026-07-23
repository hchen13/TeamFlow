from __future__ import annotations

import sqlite3


ID = "018_agent_delivery_runtime"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE agents ADD COLUMN assignment_revision INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE agents ADD COLUMN context_applied_revision INTEGER NOT NULL DEFAULT 0;
        ALTER TABLE agents ADD COLUMN context_applied_at TEXT;

        ALTER TABLE task_event_deliveries RENAME TO task_event_deliveries_legacy;

        CREATE TABLE task_event_deliveries (
          event_key TEXT NOT NULL,
          agent_id TEXT NOT NULL,
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
          PRIMARY KEY (event_key, agent_id),
          FOREIGN KEY (event_key) REFERENCES task_events(event_key) ON DELETE CASCADE
        );

        INSERT INTO task_event_deliveries (
          event_key, agent_id, harness_type, session_id, prompt, status,
          attempts, last_error, created_at, delivered_at
        )
        SELECT delivery.event_key,
               delivery.agent_id,
               COALESCE(agent.harness_type, 'codex'),
               COALESCE(agent.session_id, ''),
               delivery.prompt,
               delivery.status,
               delivery.attempts,
               delivery.last_error,
               delivery.created_at,
               delivery.delivered_at
        FROM task_event_deliveries_legacy AS delivery
        LEFT JOIN agents AS agent ON agent.id = delivery.agent_id
        WHERE delivery.status != 'previewed';

        DROP TABLE task_event_deliveries_legacy;

        CREATE INDEX task_event_deliveries_pending
          ON task_event_deliveries(status, next_attempt_at, created_at);

        CREATE INDEX task_event_deliveries_session
          ON task_event_deliveries(session_id, status, created_at);
        """
    )
