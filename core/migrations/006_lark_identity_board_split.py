from __future__ import annotations

import sqlite3


ID = "006_lark_identity_board_split"


def apply(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS lark_identities (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          auth_mode TEXT NOT NULL CHECK (auth_mode IN ('bot', 'user')),
          app_id TEXT,
          app_name TEXT,
          app_avatar_url TEXT,
          app_name_synced_at TEXT,
          is_default INTEGER NOT NULL DEFAULT 0 CHECK (is_default IN (0, 1)),
          app_secret TEXT,
          access_token TEXT,
          refresh_token TEXT,
          access_status TEXT NOT NULL DEFAULT 'unverified',
          last_verified_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, auth_mode, app_id)
        );

        CREATE TABLE IF NOT EXISTS lark_boards (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL UNIQUE REFERENCES workspaces(id) ON DELETE CASCADE,
          identity_id TEXT REFERENCES lark_identities(id) ON DELETE SET NULL,
          base_url TEXT,
          base_token TEXT,
          table_id TEXT,
          view_id TEXT,
          access_status TEXT NOT NULL DEFAULT 'unverified',
          last_verified_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );
        """
    )
    if not {row["name"] for row in conn.execute("PRAGMA table_info(lark_connections)")}:
        return
    conn.executescript(
        """
        INSERT OR IGNORE INTO lark_identities
          (id, workspace_id, auth_mode, app_id, app_name, app_avatar_url, app_name_synced_at, is_default, app_secret, access_token, refresh_token, access_status, last_verified_at, last_error, created_at, updated_at)
        SELECT id, workspace_id, auth_mode, app_id, app_name, NULL, app_name_synced_at, is_default, app_secret, access_token, refresh_token, access_status, last_verified_at, last_error, created_at, updated_at
        FROM lark_connections
        WHERE label != 'board';

        INSERT OR REPLACE INTO lark_boards
          (id, workspace_id, identity_id, base_url, base_token, table_id, view_id, access_status, last_verified_at, last_error, created_at, updated_at)
        SELECT
          board.id,
          board.workspace_id,
          identity.id,
          board.base_url,
          board.base_token,
          board.table_id,
          board.view_id,
          board.access_status,
          board.last_verified_at,
          board.last_error,
          board.created_at,
          board.updated_at
        FROM lark_connections AS board
        LEFT JOIN lark_identities AS identity
          ON identity.workspace_id = board.workspace_id
         AND identity.auth_mode = board.auth_mode
         AND identity.app_id = board.app_id
        WHERE board.label = 'board' OR board.base_url IS NOT NULL OR board.base_token IS NOT NULL;

        DROP TABLE lark_connections;
        """
    )
