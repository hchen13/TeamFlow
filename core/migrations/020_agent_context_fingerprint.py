from __future__ import annotations

import sqlite3


ID = "020_agent_context_fingerprint"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        ALTER TABLE agents DROP COLUMN context_applied_revision;
        ALTER TABLE agents RENAME COLUMN context_applied_at TO context_injected_at;
        ALTER TABLE agents ADD COLUMN context_fingerprint TEXT;

        UPDATE agents SET context_injected_at = NULL;
        """
    )
