from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

import lark_oapi as lark

from .config import resolve_workspace_paths
from .db import (
    bootstrap_workspace,
    connect,
    current_workflow,
    lark_identity_is_usable,
    lark_user_status_values,
    now,
    run_lark_cli_json,
    workspace_id_for_root,
)
from .lark_board import LarkBoardClient, TEAMFLOW_APP_SCOPES


LISTENER_CONNECT_TIMEOUT = 10
LISTENER_EVENT_TIMEOUT = 10
LISTENER_PROBE_ATTEMPTS = 3


@dataclass(frozen=True)
class LarkEventContext:
    workspace_root: str
    db_path: str
    identity_id: str
    identity_name: str
    app_id: str
    app_name: str
    app_secret: str
    auth_mode: str
    user_open_id: str
    board_url: str
    file_token: str
    table_id: str
    brand: str
    workspace_name: str = ""
    workflow_key: str = ""
    board_name: str = ""
    table_name: str = ""

    def public(self) -> dict[str, Any]:
        return {
            "workspace_root": self.workspace_root,
            "db_path": self.db_path,
            "identity_id": self.identity_id,
            "identity_name": self.identity_name,
            "app_id": self.app_id,
            "app_name": self.app_name,
            "auth_mode": self.auth_mode,
            "board_url": self.board_url,
            "file_token": self.file_token,
            "table_id": self.table_id,
            "brand": self.brand,
            "workspace_name": self.workspace_name,
            "workflow_key": self.workflow_key,
            "board_name": self.board_name,
            "table_name": self.table_name,
        }


def subscribe_lark_board_events(workspace: str | None) -> dict[str, Any]:
    context = resolve_lark_event_table(lark_event_context(workspace))
    already_subscribed = ensure_lark_board_subscription(context)
    return {**context.public(), "already_subscribed": already_subscribed}


def verify_lark_board_listener(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    from .daemon import verify_daemon_workspace

    return verify_daemon_workspace(workspace, identity_id=identity_id)


def listen_lark_board_events(
    workspace: str | None,
    *,
    emit: Callable[[dict[str, Any]], None],
    ready: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    from .daemon import stream_daemon_events

    stream_daemon_events(workspace, emit=emit, ready=ready)


def lark_listener_details(context: LarkEventContext) -> dict[str, Any]:
    client = context_client(context)
    bitable = client.get_base()
    table = next((item for item in client.list_tables() if item["table_id"] == context.table_id), None)
    return {
        "workspace_root": context.workspace_root,
        "board": {
            "name": bitable.get("name") or context.file_token,
            "url": context.board_url,
            "file_token": context.file_token,
            "table_id": context.table_id,
            "table_name": (table or {}).get("table_name") or context.table_id or "-",
        },
        "identity": {
            "id": context.identity_id,
            "name": context.identity_name,
            "auth_mode": context.auth_mode,
        },
        "app": {"id": context.app_id, "name": context.app_name},
    }


def lark_event_context(workspace: str | None, *, identity_id: str | None = None) -> LarkEventContext:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        raise ValueError("TeamFlow workspace is not initialized")

    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        workspace_row = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        workflow = current_workflow(conn, workspace_row)
        board = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if board is None or not board["base_token"]:
            raise ValueError("configure a Lark Bitable before listening for events")
        selected_identity_id = identity_id or board["primary_identity_id"]
        if not selected_identity_id:
            raise ValueError("the configured Bitable has no owner or manager identity")
        identity = conn.execute(
            "SELECT * FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, selected_identity_id),
        ).fetchone()
        if identity is None:
            raise ValueError("lark identity not found")
        if not lark_identity_is_usable(identity) or not identity["app_id"]:
            raise ValueError("the Bitable owner or manager identity is unavailable")
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM lark_board_identity_access WHERE board_id = ?",
            (board["id"],),
        ).fetchone()[0]
        access = conn.execute(
            "SELECT status FROM lark_board_identity_access WHERE board_id = ? AND identity_id = ?",
            (board["id"], selected_identity_id),
        ).fetchone()
        if snapshot_count and (access is None or access["status"] != "verified"):
            raise ValueError("verify this identity's Bitable access before testing board listening")
        credential = identity if identity["app_secret"] else conn.execute(
            """
            SELECT * FROM lark_identities
            WHERE workspace_id = ? AND app_id = ? AND app_secret IS NOT NULL
            ORDER BY updated_at DESC LIMIT 1
            """,
            (workspace_id, identity["app_id"]),
        ).fetchone()
        auth_mode = str(identity["auth_mode"])
        app_id = str(identity["app_id"])
        app_secret = str(credential["app_secret"] or "") if credential else ""
        app_name = str((credential["app_name"] if credential else None) or identity["app_name"] or app_id)
        identity_name = str(identity["user_name"] or identity["app_name"] or identity["id"])
        user_open_id = str(identity["user_open_id"] or "")
        board_url = str(board["base_url"] or "")
        file_token = str(board["base_token"])
        table_id = str(board["table_id"] or "")
        workspace_name = str((workspace_row["display_name"] if workspace_row else None) or paths.root.name)
        workflow_key = str(workflow["key"] if workflow else "")

    brand = "larksuite" if (urlparse(board_url).hostname or "").endswith("larksuite.com") else "feishu"
    if auth_mode == "user":
        config = run_lark_cli_json(["config", "show"])
        if config.get("appId") != app_id:
            raise ValueError(f"lark-cli is configured for {config.get('appId') or 'no app'}, but this identity uses {app_id}")
        if config.get("brand") != brand:
            raise ValueError(f"lark-cli is configured for {config.get('brand') or 'an unknown domain'}, but this Bitable uses {brand}")
        status = run_lark_cli_json(["auth", "status", "--verify"])
        lark_user_status_values(status, expected_user_open_id=user_open_id)
        app_secret = app_secret or str(config.get("appSecret") or "")
    if not app_secret:
        raise ValueError("the Bitable app credentials are not configured")

    return LarkEventContext(
        workspace_root=str(paths.root),
        db_path=str(paths.db_path),
        identity_id=str(selected_identity_id),
        identity_name=identity_name,
        app_id=app_id,
        app_name=app_name,
        app_secret=app_secret,
        auth_mode=auth_mode,
        user_open_id=user_open_id,
        board_url=board_url,
        file_token=file_token,
        table_id=table_id,
        brand=brand,
        workspace_name=workspace_name,
        workflow_key=workflow_key,
    )


def event_matches_board(payload: dict[str, Any], context: dict[str, Any]) -> bool:
    event = payload.get("event") or payload
    if event.get("file_token") != context["file_token"]:
        return False
    return not context["table_id"] or not event.get("table_id") or event.get("table_id") == context["table_id"]


def event_record_ids(payload: dict[str, Any]) -> set[str]:
    event = payload.get("event") or payload
    record_ids = {str(event["record_id"])} if event.get("record_id") else set()
    for action in event.get("action_list") or []:
        if isinstance(action, dict) and action.get("record_id"):
            record_ids.add(str(action["record_id"]))
    return record_ids


def lark_event_metadata(payload: dict[str, Any]) -> dict[str, str | None]:
    header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    event_id = str(header.get("event_id") or "").strip()
    event_type = str(header.get("event_type") or "").strip()
    if not event_id or not event_type:
        raise ValueError("Lark event is missing header.event_id or header.event_type")
    revision = event.get("revision")
    return {
        "event_id": event_id,
        "event_type": event_type,
        "file_token": str(event.get("file_token") or "") or None,
        "table_id": str(event.get("table_id") or "") or None,
        "source_revision": str(revision) if revision is not None else None,
    }


def event_record_actions(payload: dict[str, Any]) -> dict[str, str]:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    actions = {}
    for item in event.get("action_list") or []:
        if not isinstance(item, dict) or not item.get("record_id"):
            continue
        actions[str(item["record_id"])] = str(item.get("action") or item.get("action_type") or "")
    if event.get("record_id"):
        actions.setdefault(str(event["record_id"]), str(event.get("action") or ""))
    return actions


def save_task_snapshot(
    context: LarkEventContext,
    *,
    record_id: str,
    task: dict[str, Any] | None,
    source_event_id: str | None,
    source_revision: str | None,
) -> list[str]:
    timestamp = now()
    after_json = _snapshot_json(task) if task is not None else None
    after_hash = _snapshot_hash(after_json) if after_json is not None else None
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        board_id, workflow_id = _task_event_scope(conn, context)
        previous = conn.execute(
            """
            SELECT * FROM lark_task_state
            WHERE board_id = ? AND table_id = ? AND record_id = ?
            """,
            (board_id, context.table_id, record_id),
        ).fetchone()
        if previous and _older_revision(source_revision, previous["source_revision"]):
            return []
        before_json = str(previous["snapshot_json"]) if previous else None
        if previous and after_hash == previous["snapshot_hash"]:
            conn.execute(
                """
                UPDATE lark_task_state
                SET source_revision = COALESCE(?, source_revision),
                    last_event_id = COALESCE(?, last_event_id), updated_at = ?
                WHERE board_id = ? AND table_id = ? AND record_id = ?
                """,
                (source_revision, source_event_id, timestamp, board_id, context.table_id, record_id),
            )
            return []

        before = json.loads(before_json) if before_json else None
        event_types = _task_event_types(before, task)
        revision_key = str(
            source_revision
            or f"snapshot-{(after_hash or _snapshot_hash(before_json or 'null'))[:16]}"
        )
        inserted = []
        for event_type in event_types:
            event_key = f"{context.table_id}:{revision_key}:{record_id}:{event_type}"
            cursor = conn.execute(
                """
                INSERT OR IGNORE INTO task_events (
                  event_key, board_id, workflow_id, table_id, record_id, source_event_id,
                  source_revision, event_type, before_json, after_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event_key,
                    board_id,
                    workflow_id,
                    context.table_id,
                    record_id,
                    source_event_id,
                    revision_key,
                    event_type,
                    before_json,
                    after_json,
                    timestamp,
                ),
            )
            if cursor.rowcount == 1:
                inserted.append(event_type)

        if task is None:
            conn.execute(
                "DELETE FROM lark_task_state WHERE board_id = ? AND table_id = ? AND record_id = ?",
                (board_id, context.table_id, record_id),
            )
        else:
            conn.execute(
                """
                INSERT INTO lark_task_state (
                  board_id, table_id, record_id, status, source_revision,
                  snapshot_json, snapshot_hash, last_event_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(board_id, table_id, record_id) DO UPDATE SET
                  status = excluded.status,
                  source_revision = excluded.source_revision,
                  snapshot_json = excluded.snapshot_json,
                  snapshot_hash = excluded.snapshot_hash,
                  last_event_id = excluded.last_event_id,
                  updated_at = excluded.updated_at
                """,
                (
                    board_id,
                    context.table_id,
                    record_id,
                    task.get("status"),
                    source_revision,
                    after_json,
                    after_hash,
                    source_event_id,
                    timestamp,
                ),
            )
        return inserted


def saved_task_record_ids(context: LarkEventContext) -> set[str]:
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        board = conn.execute("SELECT id FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if board is None:
            return set()
        return {
            str(row[0])
            for row in conn.execute(
                "SELECT record_id FROM lark_task_state WHERE board_id = ? AND table_id = ?",
                (board["id"], context.table_id),
            )
        }


def saved_task_snapshot(context: LarkEventContext, record_id: str) -> dict[str, Any] | None:
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, context.workspace_root)
        row = conn.execute(
            """
            SELECT state.snapshot_json
            FROM lark_task_state AS state
            JOIN lark_boards AS board ON board.id = state.board_id
            WHERE board.workspace_id = ? AND state.table_id = ? AND state.record_id = ?
            """,
            (workspace_id, context.table_id, record_id),
        ).fetchone()
    return json.loads(row["snapshot_json"]) if row else None


def save_board_schema_event(
    context: LarkEventContext,
    *,
    source_event_id: str,
    source_revision: str | None,
) -> bool:
    timestamp = now()
    revision_key = source_revision or source_event_id
    event_key = f"{context.table_id}:{revision_key}::board_schema_changed"
    with connect(context.db_path) as conn:
        bootstrap_workspace(conn)
        board_id, workflow_id = _task_event_scope(conn, context)
        cursor = conn.execute(
            """
            INSERT OR IGNORE INTO task_events (
              event_key, board_id, workflow_id, table_id, record_id, source_event_id,
              source_revision, event_type, created_at
            ) VALUES (?, ?, ?, ?, '', ?, ?, 'board_schema_changed', ?)
            """,
            (event_key, board_id, workflow_id, context.table_id, source_event_id, revision_key, timestamp),
        )
        return cursor.rowcount == 1


def _task_event_scope(conn: Any, context: LarkEventContext) -> tuple[str, str]:
    workspace_id = workspace_id_for_root(conn, context.workspace_root)
    workspace = conn.execute("SELECT * FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
    workflow = current_workflow(conn, workspace)
    board = conn.execute("SELECT id FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
    if board is None:
        raise ValueError("the TeamFlow workspace has no configured Lark Bitable")
    if workflow is None:
        raise ValueError("the TeamFlow workspace has no configured workflow")
    return str(board["id"]), str(workflow["id"])


def _task_event_types(before: dict[str, Any] | None, after: dict[str, Any] | None) -> list[str]:
    if before is None and after is None:
        return []
    if before is None:
        result = ["task_created"]
        if after and after.get("status"):
            result.append(f"{after['status']}_entered")
        return result
    if after is None:
        return ["task_deleted"]
    before_status = before.get("status")
    after_status = after.get("status")
    if before_status != after_status:
        result = []
        if before_status:
            result.append(f"{before_status}_left")
        if after_status:
            result.append(f"{after_status}_entered")
        return result or ["task_updated"]
    return [f"{after_status}_updated" if after_status else "task_updated"]


def _snapshot_json(task: dict[str, Any]) -> str:
    return json.dumps(task, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _snapshot_hash(snapshot: str) -> str:
    return hashlib.sha256(snapshot.encode()).hexdigest()


def _older_revision(candidate: str | None, current: str | None) -> bool:
    try:
        return candidate is not None and current is not None and int(candidate) < int(current)
    except ValueError:
        return False


def context_client(context: LarkEventContext) -> LarkBoardClient:
    return LarkBoardClient(
        {
            "auth_mode": context.auth_mode,
            "app_id": context.app_id,
            "app_secret": context.app_secret,
            "user_open_id": context.user_open_id,
        },
        {"base_token": context.file_token, "base_url": context.board_url},
    )


def ensure_lark_board_subscription(context: LarkEventContext) -> bool:
    client = context_client(context)
    subscribed = client.file_events_subscribed()
    if not subscribed:
        client.subscribe_file_events()
    return subscribed


def resolve_lark_event_table(context: LarkEventContext) -> LarkEventContext:
    if context.table_id:
        return context
    tables = context_client(context).list_tables()
    if not tables:
        raise ValueError("the Bitable has no data table for listener verification")
    return replace(context, table_id=str(tables[0]["table_id"]))


def run_lark_app_worker(
    context: LarkEventContext,
    *,
    emit: Callable[[dict[str, Any]], None],
    ready: Callable[[], None],
) -> None:
    ready_sent = False

    def handle(data: Any) -> None:
        emit(json.loads(lark.JSON.marshal(data)))

    class ManagedClient(lark.ws.Client):
        async def _receive_message_loop(self) -> None:
            nonlocal ready_sent
            if not ready_sent:
                ready_sent = True
                ready()
            await super()._receive_message_loop()

    handler = (
        lark.EventDispatcherHandler.builder("", "")
        .register_p2_drive_file_bitable_record_changed_v1(handle)
        .register_p2_drive_file_bitable_field_changed_v1(handle)
        .build()
    )
    ManagedClient(
        context.app_id,
        context.app_secret,
        log_level=lark.LogLevel.WARNING,
        event_handler=handler,
        domain=lark.LARK_DOMAIN if context.brand == "larksuite" else lark.FEISHU_DOMAIN,
    ).start()


def listener_failure(error: Exception, context: LarkEventContext | None) -> dict[str, Any]:
    message = str(error)
    lowered = message.lower()
    code = str(getattr(error, "code", ""))
    if "no owner or manager identity" in lowered:
        failure_kind = "primary_identity_missing"
    elif code == "1069603" or "1069603" in lowered or "manage permission" in lowered or "forbidden" in lowered:
        failure_kind = "not_manager"
    elif code in {"99991672", "99991679", "20027"} or "required one of" in lowered or "scope" in lowered:
        failure_kind = "missing_scope"
    elif "did not receive" in lowered:
        failure_kind = "event_not_received"
    elif "authorization" in lowered or "token has expired" in lowered:
        failure_kind = "auth_expired"
    elif "cleanup failed" in lowered:
        failure_kind = "cleanup_failed"
    else:
        failure_kind = "connection_failed"
    repair_url = None
    if context:
        if failure_kind == "not_manager":
            repair_url = context.board_url
        elif failure_kind == "missing_scope":
            repair_url = event_permission_url(context)
        elif failure_kind == "event_not_received":
            repair_url = event_configuration_url(context)
    return {"failure_kind": failure_kind, "last_error": message, "repair_url": repair_url}


def event_permission_url(context: LarkEventContext) -> str:
    origin = "https://open.larksuite.com" if context.brand == "larksuite" else "https://open.feishu.cn"
    query = urlencode({
        "q": ",".join(TEAMFLOW_APP_SCOPES),
        "op_from": "openapi",
        "token_type": "user" if context.auth_mode == "user" else "tenant",
    })
    return f"{origin}/app/{quote(context.app_id, safe='')}/auth?{query}"


def event_configuration_url(context: LarkEventContext) -> str:
    origin = "https://open.larksuite.com" if context.brand == "larksuite" else "https://open.feishu.cn"
    return f"{origin}/app/{quote(context.app_id, safe='')}"


def save_listener_result(workspace: str | None, identity_id: str | None, result: dict[str, Any]) -> None:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        return
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        board = conn.execute("SELECT id FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if board is None:
            return
        conn.execute(
            """
            UPDATE lark_boards
            SET primary_identity_id = COALESCE(?, primary_identity_id),
                listener_status = ?, listener_last_verified_at = ?,
                listener_failure_kind = ?, listener_last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                identity_id,
                result["status"],
                result["last_verified_at"],
                result.get("failure_kind"),
                result.get("last_error"),
                now(),
                board["id"],
            ),
        )
