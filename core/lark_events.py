from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any, Callable
from urllib.parse import quote, urlencode, urlparse

import lark_oapi as lark

from .config import resolve_workspace_paths
from .db import (
    bootstrap_workspace,
    connect,
    lark_identity_is_usable,
    lark_user_status_values,
    now,
    run_lark_cli_json,
    workspace_id_for_root,
)
from .lark_board import LarkBoardClient


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
        "q": "bitable:app,docs:event:subscribe,drive:drive.metadata:readonly",
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
            SET primary_identity_id = CASE WHEN ? THEN COALESCE(?, primary_identity_id) ELSE primary_identity_id END,
                listener_status = ?, listener_last_verified_at = ?,
                listener_failure_kind = ?, listener_last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(bool(result.get("ok"))),
                identity_id,
                result["status"],
                result["last_verified_at"],
                result.get("failure_kind"),
                result.get("last_error"),
                now(),
                board["id"],
            ),
        )
