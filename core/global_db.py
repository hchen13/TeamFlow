from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

from .config import resolve_workspace_paths
from .global_migrations import MIGRATIONS


EVENT_RETRY_WINDOW = timedelta(days=1)


def teamflow_home() -> Path:
    return Path(os.environ.get("TEAMFLOW_HOME") or Path.home() / ".teamflow").expanduser().resolve()


def global_database_path() -> Path:
    return teamflow_home() / "teamflow.db"


@contextmanager
def connect_global() -> Iterator[sqlite3.Connection]:
    home = teamflow_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    database = global_database_path()
    conn = sqlite3.connect(database, timeout=30)
    os.chmod(database, 0o600)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        with conn:
            _run_migrations(conn)
            yield conn
    finally:
        conn.close()


def register_workspace(workspace: str | None, *, enabled: bool | None = None) -> str:
    root = str(resolve_workspace_paths(workspace).root)
    timestamp = _now()
    with connect_global() as conn:
        conn.execute(
            """
            INSERT INTO workspaces (root_path, enabled, updated_at) VALUES (?, ?, ?)
            ON CONFLICT(root_path) DO UPDATE SET
              enabled = CASE WHEN ? IS NULL THEN workspaces.enabled ELSE excluded.enabled END,
              updated_at = excluded.updated_at
            """,
            (root, int(bool(enabled)), timestamp, enabled),
        )
    return root


def registered_workspaces(*, enabled_only: bool = False) -> list[str]:
    if not global_database_path().exists():
        return []
    with connect_global() as conn:
        query = "SELECT root_path FROM workspaces"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY updated_at"
        return [str(row[0]) for row in conn.execute(query)]


def workspace_enabled(workspace: str | None) -> bool:
    root = str(resolve_workspace_paths(workspace).root)
    if not global_database_path().exists():
        return False
    with connect_global() as conn:
        row = conn.execute("SELECT enabled FROM workspaces WHERE root_path = ?", (root,)).fetchone()
        return bool(row and row["enabled"])


def record_lark_event(
    *,
    event_id: str,
    brand: str,
    app_id: str,
    event_type: str,
    file_token: str | None,
    table_id: str | None,
    source_revision: str | None,
    payload: dict[str, Any],
) -> bool:
    with connect_global() as conn:
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO lark_event_inbox (
              event_id, brand, app_id, event_type, file_token, table_id, source_revision,
              payload_json, received_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                event_id,
                brand,
                app_id,
                event_type,
                file_token,
                table_id,
                source_revision,
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                _now(),
            ),
        )
        return cursor.rowcount == 1


def due_lark_event_ids(limit: int = 100) -> list[str]:
    if not global_database_path().exists():
        return []
    timestamp = _now()
    with connect_global() as conn:
        return [
            str(row[0])
            for row in conn.execute(
                """
                SELECT event_id
                FROM lark_event_inbox
                WHERE status = 'pending'
                   OR (status = 'retry' AND (next_attempt_at IS NULL OR next_attempt_at <= ?))
                ORDER BY received_at
                LIMIT ?
                """,
                (timestamp, limit),
            )
        ]


def claim_lark_event(event_id: str) -> dict[str, Any] | None:
    with connect_global() as conn:
        cursor = conn.execute(
            """
            UPDATE lark_event_inbox
            SET status = 'processing', attempts = attempts + 1, next_attempt_at = NULL
            WHERE event_id = ?
              AND (status = 'pending' OR (status = 'retry' AND (next_attempt_at IS NULL OR next_attempt_at <= ?)))
            """,
            (event_id, _now()),
        )
        if cursor.rowcount != 1:
            return None
        row = conn.execute("SELECT * FROM lark_event_inbox WHERE event_id = ?", (event_id,)).fetchone()
        result = dict(row)
        result["payload"] = json.loads(result.pop("payload_json"))
        return result


def finish_lark_event(event_id: str, *, status: str = "processed", error: str | None = None) -> None:
    with connect_global() as conn:
        conn.execute(
            """
            UPDATE lark_event_inbox
            SET status = ?, last_error = ?, processed_at = ?, next_attempt_at = NULL
            WHERE event_id = ?
            """,
            (status, error, _now(), event_id),
        )


def retry_lark_event(event_id: str, error: Exception) -> str:
    with connect_global() as conn:
        row = conn.execute(
            "SELECT attempts, received_at FROM lark_event_inbox WHERE event_id = ?",
            (event_id,),
        ).fetchone()
        attempts = int(row["attempts"]) if row else 0
        received_at = (
            datetime.fromisoformat(str(row["received_at"]))
            if row
            else datetime.min.replace(tzinfo=timezone.utc)
        )
        if datetime.now(timezone.utc) - received_at >= EVENT_RETRY_WINDOW:
            status = "failed"
            next_attempt_at = None
            processed_at = _now()
        else:
            status = "retry"
            delay = min(2 ** max(attempts - 1, 0), 300)
            next_attempt_at = (datetime.now(timezone.utc) + timedelta(seconds=delay)).isoformat()
            processed_at = None
        conn.execute(
            """
            UPDATE lark_event_inbox
            SET status = ?, next_attempt_at = ?, last_error = ?, processed_at = ?
            WHERE event_id = ?
            """,
            (status, next_attempt_at, str(error), processed_at, event_id),
        )
        return status


def recover_lark_events() -> None:
    with connect_global() as conn:
        conn.execute(
            """
            UPDATE lark_event_inbox
            SET status = 'retry', next_attempt_at = NULL,
                last_error = COALESCE(last_error, 'TeamFlow daemon stopped during event processing')
            WHERE status = 'processing'
            """
        )


def lark_event_counts() -> dict[str, int]:
    if not global_database_path().exists():
        return {}
    with connect_global() as conn:
        return {str(row["status"]): int(row["count"]) for row in conn.execute(
            "SELECT status, COUNT(*) AS count FROM lark_event_inbox GROUP BY status"
        )}


def cleanup_lark_events() -> int:
    processed_cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    failed_cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    with connect_global() as conn:
        cursor = conn.execute(
            """
            DELETE FROM lark_event_inbox
            WHERE (status IN ('processed', 'ignored') AND processed_at < ?)
               OR (status = 'failed' AND processed_at < ?)
            """,
            (processed_cutoff, failed_cutoff),
        )
        return cursor.rowcount


def _run_migrations(conn: sqlite3.Connection) -> None:
    conn.execute("CREATE TABLE IF NOT EXISTS migrations (id TEXT PRIMARY KEY, applied_at TEXT NOT NULL)")
    applied = {row["id"] for row in conn.execute("SELECT id FROM migrations")}
    for migration in MIGRATIONS:
        if migration.ID in applied:
            continue
        migration.apply(conn)
        conn.execute("INSERT INTO migrations (id, applied_at) VALUES (?, ?)", (migration.ID, _now()))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
