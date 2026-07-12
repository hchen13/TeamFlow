from __future__ import annotations

import sqlite3


ID = "013_lark_board_identity_access"


def apply(conn: sqlite3.Connection) -> None:
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(lark_boards)")}
    if "identity_id" in columns and "primary_identity_id" not in columns:
        conn.execute("ALTER TABLE lark_boards RENAME COLUMN identity_id TO primary_identity_id")
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS lark_board_identity_access (
          board_id TEXT NOT NULL REFERENCES lark_boards(id) ON DELETE CASCADE,
          identity_id TEXT NOT NULL REFERENCES lark_identities(id) ON DELETE CASCADE,
          status TEXT NOT NULL DEFAULT 'unverified',
          auth_status TEXT NOT NULL DEFAULT 'unverified',
          api_status TEXT NOT NULL DEFAULT 'unverified',
          collaborator_status TEXT NOT NULL DEFAULT 'unverified',
          read_status TEXT NOT NULL DEFAULT 'unverified',
          write_status TEXT NOT NULL DEFAULT 'unverified',
          cleanup_status TEXT NOT NULL DEFAULT 'unverified',
          failure_kind TEXT,
          missing_scopes TEXT,
          repair_url TEXT,
          last_error TEXT,
          last_verified_at TEXT,
          PRIMARY KEY (board_id, identity_id)
        )
        """
    )
    conn.execute(
        "UPDATE lark_boards SET access_status = 'unverified', last_verified_at = NULL, last_error = NULL"
    )
