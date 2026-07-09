from __future__ import annotations

import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import ensure_workspace_gitignore, parse_lark_base_url, resolve_workspace_paths


SCHEMA_VERSION = 1


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_workspace(workspace: str | None, display_name: str | None = None, write_gitignore: bool = False) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    paths.state_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(paths.state_dir, 0o700)

    with connect(paths.db_path) as conn:
        run_migrations(conn)
        upsert_workspace(conn, paths.root, display_name)

    os.chmod(paths.db_path, 0o600)
    gitignore_updated = ensure_workspace_gitignore(paths) if write_gitignore else False
    return {
        "ok": True,
        "workspace_root": str(paths.root),
        "state_dir": str(paths.state_dir),
        "db_path": str(paths.db_path),
        "schema_version": SCHEMA_VERSION,
        "gitignore_updated": gitignore_updated,
    }


def inspect_workspace(workspace: str | None) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return {
            "ok": True,
            "initialized": False,
            "workspace_root": str(paths.root),
            "state_dir": str(paths.state_dir),
            "db_path": str(paths.db_path),
        }

    with connect(paths.db_path) as conn:
        workspace_row = conn.execute(
            "SELECT * FROM workspaces WHERE root_path = ?",
            (str(paths.root),),
        ).fetchone()
        if workspace_row is None:
            workspace_row = conn.execute("SELECT * FROM workspaces ORDER BY created_at LIMIT 1").fetchone()

        workspace_id = workspace_row["id"] if workspace_row else None
        return {
            "ok": True,
            "initialized": True,
            "workspace_root": str(paths.root),
            "state_dir": str(paths.state_dir),
            "db_path": str(paths.db_path),
            "schema_version": current_schema_version(conn),
            "workspace": row_dict(workspace_row),
            "lark_connections": redact_rows(fetch_all(conn, "SELECT * FROM lark_connections WHERE workspace_id = ? ORDER BY updated_at DESC", workspace_id)),
        }


def configure_lark(
    workspace: str | None,
    *,
    base_url: str | None,
    base_token: str | None,
    table_id: str | None,
    view_id: str | None,
    auth_mode: str,
    app_id: str | None,
    app_secret: str | None,
    access_token: str | None,
    refresh_token: str | None,
    label: str = "default",
    write_gitignore: bool = False,
) -> dict[str, Any]:
    init_result = init_workspace(workspace, write_gitignore=write_gitignore)
    paths = resolve_workspace_paths(workspace)
    parsed = parse_lark_base_url(base_url)
    final_base_token = base_token or parsed["base_token"]
    final_table_id = table_id or parsed["table_id"]
    final_view_id = view_id or parsed["view_id"]
    auth_mode = normalize_auth_mode(auth_mode)

    with connect(paths.db_path) as conn:
        workspace_id = workspace_id_for_root(conn, paths.root)
        connection_id = upsert_lark_connection(
            conn,
            workspace_id=workspace_id,
            label=label,
            base_url=base_url,
            base_token=final_base_token,
            table_id=final_table_id,
            view_id=final_view_id,
            auth_mode=auth_mode,
            app_id=app_id,
            app_secret=app_secret,
            access_token=access_token,
            refresh_token=refresh_token,
        )

    return {
        "ok": True,
        **init_result,
        "lark_connection_id": connection_id,
        "access_status": "unverified",
        "missing": lark_missing_items(auth_mode, final_base_token, final_table_id, app_id, app_secret, access_token),
    }


def run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["version"] for row in conn.execute("SELECT version FROM schema_migrations")}
    if 1 not in applied:
        apply_schema_v1(conn)
        conn.execute(
            "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
            (1, now()),
        )


def apply_schema_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE workspaces (
          id TEXT PRIMARY KEY,
          root_path TEXT NOT NULL UNIQUE,
          display_name TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL
        );

        CREATE TABLE lark_connections (
          id TEXT PRIMARY KEY,
          workspace_id TEXT NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
          label TEXT NOT NULL,
          base_url TEXT,
          base_token TEXT,
          table_id TEXT,
          view_id TEXT,
          auth_mode TEXT NOT NULL CHECK (auth_mode IN ('bot', 'user')),
          app_id TEXT,
          app_secret TEXT,
          access_token TEXT,
          refresh_token TEXT,
          access_status TEXT NOT NULL DEFAULT 'unverified',
          last_verified_at TEXT,
          last_error TEXT,
          created_at TEXT NOT NULL,
          updated_at TEXT NOT NULL,
          UNIQUE(workspace_id, label)
        );
        """
    )


def upsert_workspace(conn: sqlite3.Connection, root: Path, display_name: str | None) -> str:
    existing = conn.execute("SELECT id FROM workspaces WHERE root_path = ?", (str(root),)).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            "UPDATE workspaces SET display_name = COALESCE(?, display_name), updated_at = ? WHERE id = ?",
            (display_name, timestamp, existing["id"]),
        )
        return existing["id"]

    workspace_id = f"ws_{uuid.uuid4().hex}"
    conn.execute(
        "INSERT INTO workspaces (id, root_path, display_name, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
        (workspace_id, str(root), display_name or root.name, timestamp, timestamp),
    )
    return workspace_id


def upsert_lark_connection(
    conn: sqlite3.Connection,
    *,
    workspace_id: str,
    label: str,
    base_url: str | None,
    base_token: str | None,
    table_id: str | None,
    view_id: str | None,
    auth_mode: str,
    app_id: str | None,
    app_secret: str | None,
    access_token: str | None,
    refresh_token: str | None,
) -> str:
    existing = conn.execute(
        "SELECT id FROM lark_connections WHERE workspace_id = ? AND label = ?",
        (workspace_id, label),
    ).fetchone()
    timestamp = now()
    if existing:
        conn.execute(
            """
            UPDATE lark_connections
            SET base_url = ?, base_token = ?, table_id = ?, view_id = ?,
                auth_mode = ?, app_id = ?, app_secret = ?, access_token = ?, refresh_token = ?,
                access_status = 'unverified', last_verified_at = NULL, last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (base_url, base_token, table_id, view_id, auth_mode, app_id, app_secret, access_token, refresh_token, timestamp, existing["id"]),
        )
        return existing["id"]

    connection_id = f"lark_{uuid.uuid4().hex}"
    conn.execute(
        """
        INSERT INTO lark_connections
          (id, workspace_id, label, base_url, base_token, table_id, view_id, auth_mode, app_id, app_secret, access_token, refresh_token, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (connection_id, workspace_id, label, base_url, base_token, table_id, view_id, auth_mode, app_id, app_secret, access_token, refresh_token, timestamp, timestamp),
    )
    return connection_id


def workspace_id_for_root(conn: sqlite3.Connection, root: Path) -> str:
    row = conn.execute("SELECT id FROM workspaces WHERE root_path = ?", (str(root),)).fetchone()
    if row is None:
        raise RuntimeError(f"Workspace is not initialized: {root}")
    return row["id"]


def current_schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT MAX(version) AS version FROM schema_migrations").fetchone()
    return int(row["version"] or 0)


def normalize_auth_mode(value: str) -> str:
    if value not in {"bot", "user"}:
        raise ValueError("auth_mode must be bot or user")
    return value


def lark_missing_items(auth_mode: str, base_token: str | None, table_id: str | None, app_id: str | None, app_secret: str | None, access_token: str | None) -> list[str]:
    missing = []
    if not base_token:
        missing.append("base_token")
    if not table_id:
        missing.append("table_id")
    if auth_mode == "bot":
        if not app_id:
            missing.append("app_id")
        if not app_secret:
            missing.append("app_secret")
    if auth_mode == "user" and not access_token:
        missing.append("access_token")
    return missing


def fetch_all(conn: sqlite3.Connection, sql: str, *params: Any) -> list[dict[str, Any]]:
    if params == (None,):
        return []
    return [dict(row) for row in conn.execute(sql, params)]


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    return dict(row) if row is not None else None


def redact_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    redacted = []
    for row in rows:
        item = dict(row)
        for key in ("app_secret", "access_token", "refresh_token"):
            if item.get(key):
                item[key] = "<stored>"
        redacted.append(item)
    return redacted
