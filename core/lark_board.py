from __future__ import annotations

import json
import threading
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable
from urllib.parse import parse_qs, quote, urlencode, urlparse, urlunparse

from .config import default_task_prefix, normalize_task_prefix, resolve_workspace_paths
from .db import (
    bootstrap_workspace,
    connect,
    current_workflow_key,
    lark_identity_is_usable,
    lark_user_status_values,
    now,
    post_json,
    read_json,
    run_lark_cli_json,
    workspace_id_for_root,
)
from .workflow import (
    TASK_FIELD_KEYS,
    load_workflow_definition,
    task_field_aliases,
    task_field_specs,
    task_option_aliases,
    task_option_maps,
)


TASK_NAMES = {
    "zh": {"table": "TeamFlow 任务表", "view": "看板", "primary": "任务"},
    "en": {"table": "TeamFlow Tasks", "view": "Kanban", "primary": "Title"},
}
LEGACY_TASK_TABLE_NAME = "TeamFlow Tasks"
LEGACY_TASK_VIEW_NAME = "TeamFlow Board"
TASK_KEYS = TASK_FIELD_KEYS
ACCESS_CHECKS = ("auth", "api", "collaborator", "read", "write", "cleanup")
TEAMFLOW_APP_SCOPES = (
    "bitable:app",
    "docs:event:subscribe",
    "docs:permission.member:auth",
    "docs:permission.member:create",
    "drive:drive.metadata:readonly",
)


class LarkRequestError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        code: str = "",
        missing_scopes: tuple[str, ...] = (),
        repair_url: str = "",
    ):
        super().__init__(message)
        self.code = code
        self.missing_scopes = missing_scopes
        self.repair_url = repair_url


class AccessCheckError(ValueError):
    def __init__(self, check: str, error: Exception):
        super().__init__(str(error))
        self.check = check
        self.error = error


class LarkBoardClient:
    def __init__(self, identity: dict[str, Any], board: dict[str, Any]):
        self.identity = identity
        self.base_token = str(board.get("base_token") or "")
        host = urlparse(str(board.get("base_url") or "")).hostname or ""
        self.origin = "https://open.larksuite.com" if host.endswith("larksuite.com") else "https://open.feishu.cn"
        self.user = identity.get("auth_mode") == "user"
        self.token: str | None = None
        if self.user:
            status = run_lark_cli_json(["auth", "status", "--verify"])
            app_id, _, _ = lark_user_status_values(status, expected_user_open_id=identity.get("user_open_id"))
            if identity.get("app_id") and identity["app_id"] != app_id:
                raise ValueError(f"lark-cli is configured for {app_id}, but this identity uses {identity['app_id']}")
        elif not identity.get("app_id") or not identity.get("app_secret"):
            raise ValueError("the configured Lark bot identity is incomplete")

    def authenticate(self) -> None:
        if not self.user:
            self._tenant_token()

    def check_document_permission(self, action: str) -> bool:
        query = {"token": self.base_token, "type": "bitable", "action": action}
        if self.user:
            try:
                payload = run_lark_cli_json([
                    "drive",
                    "permission.members",
                    "auth",
                    "--as",
                    "user",
                    "--params",
                    _json(query),
                ])
            except ValueError as error:
                raise _lark_request_error({}, str(error)) from error
            data = _data(payload)
        else:
            data = self._bot(
                "GET",
                f"/open-apis/drive/v1/permissions/{self._id(self.base_token)}/members/auth",
                query={"type": "bitable", "action": action},
            )
        return bool(data.get("auth_result"))

    def add_collaborator(self, open_id: str) -> None:
        params = {"token": self.base_token, "type": "bitable"}
        data = {"member_type": "openid", "member_id": open_id, "perm": "edit", "type": "user"}
        if self.user:
            run_lark_cli_json([
                "drive",
                "permission.members",
                "create",
                "--as",
                "user",
                "--params",
                _json(params),
                "--data",
                _json(data),
            ])
        else:
            self._bot(
                "POST",
                f"/open-apis/drive/v1/permissions/{self._id(self.base_token)}/members",
                query={"type": "bitable"},
                body=data,
            )

    def get_bot_info(self) -> dict[str, Any]:
        if self.user:
            raise ValueError("bot information requires an application identity")
        data = self._bot("GET", "/open-apis/bot/v3/info")
        return dict(data.get("bot") or data)

    def identity_open_id(self) -> str:
        if self.user:
            return str(self.identity.get("user_open_id") or "")
        return str(self.get_bot_info().get("open_id") or "")

    def get_file_metadata(self) -> dict[str, Any]:
        query = {"user_id_type": "open_id"}
        body = {"request_docs": [{"doc_token": self.base_token, "doc_type": "bitable"}], "with_url": True}
        if self.user:
            data = self._user_api("POST", "/open-apis/drive/v1/metas/batch_query", query=query, body=body)
        else:
            data = self._bot("POST", "/open-apis/drive/v1/metas/batch_query", query=query, body=body)
        metas = data.get("metas") or []
        if not metas:
            raise ValueError("Lark did not return Bitable file metadata")
        return dict(metas[0])

    def file_events_subscribed(self) -> bool:
        path = f"/open-apis/drive/v1/files/{self._id(self.base_token)}/get_subscribe"
        query = {"file_type": "bitable"}
        data = self._user_api("GET", path, query=query) if self.user else self._bot("GET", path, query=query)
        return bool(data.get("is_subscribe"))

    def subscribe_file_events(self) -> None:
        path = f"/open-apis/drive/v1/files/{self._id(self.base_token)}/subscribe"
        query = {"file_type": "bitable"}
        if self.user:
            self._user_api("POST", path, query=query)
        else:
            self._bot("POST", path, query=query)

    def get_base(self) -> dict[str, Any]:
        if self.user:
            data = self._user("+base-get")
        else:
            data = self._bot("GET", f"/open-apis/base/v3/bases/{self._id(self.base_token)}")
        return dict(data.get("base") or data)

    def list_tables(self) -> list[dict[str, Any]]:
        if self.user:
            data = self._user("+table-list", "--limit", "100", "--offset", "0")
        else:
            data = self._bot("GET", f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables", query={"limit": 100, "offset": 0})
        return [_table(item) for item in _items(data)]

    def get_table(self, table_id: str) -> dict[str, Any]:
        if self.user:
            data = self._user("+table-get", "--table-id", table_id)
            table = _table(data.get("table") or {})
            fields = [_field(item) for item in data.get("fields") or []]
            views = [_view(item) for item in data.get("views") or []]
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}"
            data = self._bot("GET", path)
            table = _table(data.get("table") or data)
            fields = self.list_fields(table_id)
            views = self.list_views(table_id)
        if not table.get("table_id"):
            table["table_id"] = table_id
        if not table.get("primary_field"):
            primary = next((field for field in fields if field.get("is_primary")), None)
            if primary:
                table["primary_field"] = primary["field_id"]
        return {"table": table, "fields": fields, "views": views}

    def create_table(self, name: str, primary_field_name: str) -> dict[str, Any]:
        if self.user:
            data = self._user(
                "+table-create",
                "--name",
                name,
                "--fields",
                _json([{"name": primary_field_name, "type": "text"}]),
            )
        else:
            data = self._bot("POST", f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables", body={"name": name})
        table = _table(data.get("table") or data)
        if not table.get("table_id"):
            raise ValueError("Lark did not return the created table ID")
        return table

    def update_table(self, table_id: str, name: str) -> dict[str, Any]:
        if self.user:
            data = self._user("+table-update", "--table-id", table_id, "--name", name)
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}"
            data = self._bot("PATCH", path, body={"name": name})
        return _table(data.get("table") or data)

    def list_fields(self, table_id: str) -> list[dict[str, Any]]:
        if self.user:
            data = self._user("+field-list", "--table-id", table_id, "--limit", "200", "--offset", "0")
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/fields"
            data = self._bot("GET", path, query={"limit": 200, "offset": 0})
        return [_field(item) for item in _items(data)]

    def get_field(self, table_id: str, field_id: str) -> dict[str, Any]:
        if self.user:
            data = self._user("+field-get", "--table-id", table_id, "--field-id", field_id)
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/fields/{self._id(field_id)}"
            data = self._bot("GET", path)
        return _field(data.get("field") or data)

    def create_field(self, table_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        if self.user:
            data = self._user("+field-create", "--table-id", table_id, "--json", _json(spec))
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/fields"
            data = self._bot("POST", path, body=spec)
        field = _field(data.get("field") or data)
        if not field.get("field_id"):
            raise ValueError(f"Lark did not return the created field ID for {spec['name']}")
        return field

    def update_field(self, table_id: str, field_id: str, spec: dict[str, Any]) -> dict[str, Any]:
        if self.user:
            data = self._user("+field-update", "--table-id", table_id, "--field-id", field_id, "--json", _json(spec))
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/fields/{self._id(field_id)}"
            data = self._bot("PUT", path, body=spec)
        return _field(data.get("field") or data)

    def list_views(self, table_id: str) -> list[dict[str, Any]]:
        if self.user:
            data = self._user("+view-list", "--table-id", table_id, "--limit", "100", "--offset", "0")
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/views"
            data = self._bot("GET", path, query={"limit": 100, "offset": 0})
        return [_view(item) for item in _items(data)]

    def create_view(self, table_id: str, name: str) -> dict[str, Any]:
        spec = {"name": name, "type": "kanban"}
        if self.user:
            data = self._user("+view-create", "--table-id", table_id, "--json", _json(spec))
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/views"
            data = self._bot("POST", path, body=spec)
        views = data.get("views") if isinstance(data.get("views"), list) else []
        view = _view(data.get("view") or (views[0] if views else data))
        if not view.get("view_id"):
            raise ValueError("Lark did not return the created view ID")
        return view

    def rename_view(self, table_id: str, view_id: str, name: str) -> dict[str, Any]:
        if self.user:
            data = self._user("+view-rename", "--table-id", table_id, "--view-id", view_id, "--name", name)
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/views/{self._id(view_id)}"
            data = self._bot("PATCH", path, body={"name": name})
        return _view(data.get("view") or data)

    def set_view_group(self, table_id: str, view_id: str, field_id: str) -> None:
        group = [{"field": field_id, "desc": False}]
        if self.user:
            self._user("+view-set-group", "--table-id", table_id, "--view-id", view_id, "--json", _json(group))
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/views/{self._id(view_id)}/group"
            self._bot("PUT", path, body={"group_config": group})

    def list_records(self, table_id: str, *, view_id: str | None = None, limit: int = 100, offset: int = 0) -> dict[str, Any]:
        if self.user:
            args = ["--table-id", table_id, "--limit", str(limit), "--offset", str(offset)]
            if view_id:
                args.extend(["--view-id", view_id])
            data = self._user("+record-list", *args)
        else:
            query: dict[str, Any] = {"limit": limit, "offset": offset}
            if view_id:
                query["view_id"] = view_id
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/records"
            data = self._bot("GET", path, query=query)
        return _record_page(data)

    def get_record(self, table_id: str, record_id: str) -> dict[str, Any]:
        if self.user:
            data = self._user("+record-get", "--table-id", table_id, "--record-id", record_id)
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/records/{self._id(record_id)}"
            data = self._bot("GET", path)
        return _single_record(data.get("record") or data, record_id)

    def upsert_record(self, table_id: str, fields: dict[str, Any], *, record_id: str | None = None) -> dict[str, Any]:
        if self.user:
            args = ["--table-id", table_id, "--json", _json(fields)]
            if record_id:
                args.extend(["--record-id", record_id])
            data = self._user("+record-upsert", *args)
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/records"
            method = "POST"
            if record_id:
                path += f"/{self._id(record_id)}"
                method = "PATCH"
            data = self._bot(method, path, body=fields)
        return _single_record(data.get("record") or data, record_id)

    def delete_record(self, table_id: str, record_id: str) -> None:
        if self.user:
            self._user("+record-delete", "--table-id", table_id, "--record-id", record_id, "--yes")
        else:
            path = f"/open-apis/base/v3/bases/{self._id(self.base_token)}/tables/{self._id(table_id)}/records/{self._id(record_id)}"
            self._bot("DELETE", path)

    def _user(self, command: str, *args: str) -> dict[str, Any]:
        payload = run_lark_cli_json(["base", command, "--as", "user", "--base-token", self.base_token, *args])
        if payload.get("ok") is False:
            raise ValueError(payload.get("error") or payload.get("message") or "Lark CLI request failed")
        return _data(payload)

    def _user_api(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        args = ["api", method, path, "--as", "user"]
        if query:
            args.extend(["--params", _json(query)])
        if body is not None:
            args.extend(["--data", _json(body)])
        try:
            return _data(run_lark_cli_json(args))
        except ValueError as error:
            raise _lark_request_error({}, str(error)) from error

    def _bot(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.origin}{path}"
        if query:
            url += f"?{urlencode(query)}"
        data = _json(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={"Authorization": f"Bearer {self._tenant_token()}", "Content-Type": "application/json; charset=utf-8"},
        )
        payload, error = read_json(request)
        if error:
            raise _lark_request_error(payload, error)
        return _data(payload)

    def _tenant_token(self) -> str:
        if self.token:
            return self.token
        payload, error = post_json(
            f"{self.origin}/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.identity["app_id"], "app_secret": self.identity["app_secret"]},
            {},
        )
        self.token = str(payload.get("tenant_access_token") or "")
        if error or not self.token:
            raise ValueError(error or payload.get("msg") or "failed to get tenant access token")
        return self.token

    @staticmethod
    def _id(value: str) -> str:
        return quote(str(value), safe="")


def verify_lark_board(
    workspace: str | None,
    *,
    identity_id: str | None = None,
    emit: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    paths, board, identities = _board_verification_context(workspace, identity_id)
    write_lock = threading.Lock()
    emit_lock = threading.Lock()

    def send(event: dict[str, Any]) -> None:
        if emit:
            with emit_lock:
                emit(event)

    send({"type": "verification_started", "identity_ids": [identity["id"] for identity in identities]})
    results = []
    with ThreadPoolExecutor(max_workers=min(4, len(identities))) as executor:
        futures = {
            executor.submit(_verify_lark_identity, board, identity, write_lock, send): identity["id"]
            for identity in identities
        }
        for future in as_completed(futures):
            result = future.result()
            if not _save_identity_access(paths, board, result):
                raise ValueError("the configured Bitable changed during access verification; verify the current URL again")
            results.append(result)
            send({"type": "identity_completed", "result": result})

    order = {identity["id"]: index for index, identity in enumerate(identities)}
    results.sort(key=lambda result: order[result["identity_id"]])
    summary = _update_board_access_summary(paths, board, results)
    response = {
        "ok": summary["verified"] > 0,
        "access_status": summary["status"],
        "checked": len(results),
        "summary": summary,
        "identities": results,
    }
    send({"type": "verification_completed", **response})
    return response


def grant_lark_board_access(workspace: str | None, *, identity_id: str) -> dict[str, Any]:
    paths = resolve_workspace_paths(workspace)
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        board_row = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        identity_row = conn.execute(
            "SELECT * FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, identity_id),
        ).fetchone()
        grantor_rows = [] if board_row is None else conn.execute(
            """
            SELECT identity.*
            FROM lark_identities AS identity
            JOIN lark_board_identity_access AS access ON access.identity_id = identity.id
            WHERE identity.workspace_id = ? AND access.board_id = ?
              AND access.status = 'verified' AND identity.id != ?
            ORDER BY CASE WHEN identity.id = ? THEN 0 ELSE 1 END,
                     identity.updated_at DESC
            """,
            (workspace_id, board_row["id"], identity_id, board_row["primary_identity_id"]),
        ).fetchall()
    if board_row is None or not board_row["base_token"]:
        raise ValueError("configure a Lark Bitable before granting access")
    if identity_row is None:
        raise ValueError("lark identity not found")
    board = dict(board_row)
    identity = dict(identity_row)
    if identity["auth_mode"] == "user":
        open_id = str(identity.get("user_open_id") or "").strip()
    else:
        bot = LarkBoardClient(identity, board).get_bot_info()
        open_id = str(bot.get("open_id") or "").strip()
    if not open_id:
        raise ValueError("the identity does not have an Open ID that can be added as a collaborator")

    last_error = None
    grantors = [dict(row) for row in grantor_rows]
    grantors.append({"id": None, "auth_mode": "user"})
    for grantor in grantors:
        try:
            LarkBoardClient(grantor, board).add_collaborator(open_id)
        except Exception as error:
            last_error = error
            continue
        return {
            "ok": True,
            "identity_id": identity_id,
            "grantor_identity_id": grantor.get("id"),
            "verification": verify_lark_board(workspace, identity_id=identity_id),
        }
    raise ValueError(str(last_error or "no available identity can grant document access"))


def _board_verification_context(
    workspace: str | None,
    identity_id: str | None,
) -> tuple[Any, dict[str, Any], list[dict[str, Any]]]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        raise ValueError("TeamFlow workspace is not initialized")
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        workflow_key = current_workflow_key(conn, workspace_id)
        board = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if board is None or not board["base_token"]:
            raise ValueError("configure a Lark Bitable before verifying access")
        query = "SELECT * FROM lark_identities WHERE workspace_id = ?"
        params: tuple[Any, ...] = (workspace_id,)
        if identity_id:
            query += " AND id = ?"
            params += (identity_id,)
        query += " ORDER BY updated_at DESC"
        identities = conn.execute(query, params).fetchall()
    if identity_id and not identities:
        raise ValueError("lark identity not found")
    if not identities:
        raise ValueError("configure at least one Lark identity before verifying access")
    board_data = dict(board)
    board_data["_workflow_key"] = workflow_key
    return paths, board_data, [dict(identity) for identity in identities]


def _verify_lark_identity(
    board: dict[str, Any],
    identity: dict[str, Any],
    write_lock: threading.Lock,
    send: Callable[[dict[str, Any]], None],
) -> dict[str, Any]:
    identity_id = identity["id"]
    result = {
        "identity_id": identity_id,
        "status": "failed",
        "checks": {check: "unverified" for check in ACCESS_CHECKS},
        "failure_kind": None,
        "missing_scopes": [],
        "repair_url": None,
        "last_error": None,
        "last_verified_at": None,
        "table_id": None,
        "view_id": None,
        "initialized": False,
        "is_owner": None,
        "owner_error": None,
    }

    def mark(check: str, status: str) -> None:
        result["checks"][check] = status
        send({"type": "check_updated", "identity_id": identity_id, "check": check, "status": status})

    send({"type": "identity_started", "identity_id": identity_id})
    stage = "auth"
    try:
        mark("auth", "running")
        client = LarkBoardClient(identity, board)
        client.authenticate()
        mark("auth", "passed")

        stage = "api"
        mark("api", "running")
        client.get_base()
        tables = client.list_tables()
        mark("api", "passed")
        try:
            metadata = client.get_file_metadata()
            result["is_owner"] = bool(metadata.get("owner_id")) and metadata.get("owner_id") == client.identity_open_id()
        except Exception as error:
            result["owner_error"] = str(error)

        stage = "collaborator"
        mark("collaborator", "running")
        try:
            can_view = client.check_document_permission("view")
            can_edit = client.check_document_permission("edit")
            mark("collaborator", "passed" if can_view and can_edit else "failed")
        except Exception:
            mark("collaborator", "blocked")

        stage = "read"
        mark("read", "running")
        metadata = _read_board_access(client, board, tables)
        result.update(metadata)
        mark("read", "passed")

        stage = "write"
        mark("write", "waiting")
        write_error = None
        cleanup_error = None
        record_id = ""
        with write_lock:
            mark("write", "running")
            try:
                created = client.upsert_record(metadata["probe_table_id"], {})
                record_id = str(created.get("record_id") or created.get("id") or "")
                if not record_id:
                    raise ValueError("Lark did not return the verification record ID")
                record = _read_new_record(client, metadata["probe_table_id"], record_id)
                if str(record.get("record_id") or record.get("id") or "") != record_id:
                    raise ValueError("Lark did not return the verification record")
                mark("write", "passed")
                if result["checks"]["collaborator"] != "passed":
                    mark("collaborator", "passed")
            except Exception as error:
                write_error = error
                mark("write", "failed")
            finally:
                if record_id:
                    mark("cleanup", "running")
                    try:
                        client.delete_record(metadata["probe_table_id"], record_id)
                        mark("cleanup", "passed")
                    except Exception as error:
                        cleanup_error = error
                        mark("cleanup", "failed")
                else:
                    mark("cleanup", "blocked")
        if write_error:
            raise AccessCheckError("write", write_error)
        if cleanup_error:
            raise AccessCheckError("cleanup", cleanup_error)

        result["status"] = "verified"
    except Exception as error:
        if isinstance(error, AccessCheckError):
            stage = error.check
            error = error.error
        details = _access_error_details(error, identity, board, stage)
        if result["checks"].get(stage) not in {"failed", "passed"}:
            mark(stage, "failed")
        if details["failure_kind"] == "missing_scope" and result["checks"]["api"] != "failed":
            mark("api", "failed")
        for check in ACCESS_CHECKS:
            if result["checks"][check] in {"unverified", "running", "waiting"}:
                mark(check, "blocked")
        result.update(details)
    result.pop("probe_table_id", None)
    result["last_verified_at"] = now()
    return result


def _read_board_access(
    client: LarkBoardClient,
    board: dict[str, Any],
    tables: list[dict[str, Any]],
) -> dict[str, Any]:
    table = _selected_table(board, tables)
    probe_table = table or (tables[0] if tables else None)
    if probe_table is None:
        raise ValueError("the Bitable has no data table for access verification")
    bundle = client.get_table(probe_table["table_id"])
    view_id = None
    initialized = False
    if table:
        configured_view_id = board.get("view_id")
        configured_view = next((view for view in bundle["views"] if view["view_id"] == configured_view_id), None)
        if configured_view_id and not configured_view:
            raise ValueError("the configured Lark view no longer exists")
        client.list_records(table["table_id"], view_id=configured_view_id, limit=1)
        task_view = configured_view if configured_view and _is_task_view(configured_view) else next(
            (view for view in bundle["views"] if _is_task_view(view)),
            None,
        )
        try:
            definition = load_workflow_definition(str(board["_workflow_key"]))
        except ValueError:
            definition = None
        initialized = bool(
            definition
            and _task_field_map(
                bundle,
                task_field_specs(definition, _board_locale(board)),
                task_field_aliases(),
                strict=False,
            )
            and task_view
        )
        view_id = task_view["view_id"] if initialized else None
    else:
        client.list_records(probe_table["table_id"], limit=1)
    return {
        "table_id": table["table_id"] if table else None,
        "view_id": view_id,
        "probe_table_id": probe_table["table_id"],
        "initialized": initialized,
    }


def _read_new_record(client: LarkBoardClient, table_id: str, record_id: str) -> dict[str, Any]:
    for attempt in range(4):
        try:
            return client.get_record(table_id, record_id)
        except Exception as error:
            if "not_found" not in str(error).lower() or attempt == 3:
                raise
            time.sleep(0.25)
    raise AssertionError("unreachable")


def _save_identity_access(paths: Any, board: dict[str, Any], result: dict[str, Any]) -> bool:
    checks = result["checks"]
    with connect(paths.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _same_board_resource(conn, board):
            return False
        conn.execute(
            """
            INSERT INTO lark_board_identity_access
              (board_id, identity_id, status, auth_status, api_status, collaborator_status,
               read_status, write_status, cleanup_status, failure_kind, missing_scopes,
               repair_url, last_error, last_verified_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(board_id, identity_id) DO UPDATE SET
              status = excluded.status,
              auth_status = excluded.auth_status,
              api_status = excluded.api_status,
              collaborator_status = excluded.collaborator_status,
              read_status = excluded.read_status,
              write_status = excluded.write_status,
              cleanup_status = excluded.cleanup_status,
              failure_kind = excluded.failure_kind,
              missing_scopes = excluded.missing_scopes,
              repair_url = excluded.repair_url,
              last_error = excluded.last_error,
              last_verified_at = excluded.last_verified_at
            """,
            (
                board["id"],
                result["identity_id"],
                result["status"],
                checks["auth"],
                checks["api"],
                checks["collaborator"],
                checks["read"],
                checks["write"],
                checks["cleanup"],
                result["failure_kind"],
                json.dumps(result["missing_scopes"], separators=(",", ":")),
                result["repair_url"],
                result["last_error"],
                result["last_verified_at"],
            ),
        )
    return True


def _update_board_access_summary(
    paths: Any,
    board: dict[str, Any],
    results: list[dict[str, Any]],
) -> dict[str, Any]:
    with connect(paths.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if not _same_board_resource(conn, board):
            raise ValueError("the configured Bitable changed during access verification; verify the current URL again")
        rows = conn.execute(
            """
            SELECT identity.id, access.status, access.last_error
            FROM lark_identities AS identity
            LEFT JOIN lark_board_identity_access AS access
              ON access.identity_id = identity.id AND access.board_id = ?
            WHERE identity.workspace_id = ?
            """,
            (board["id"], board["workspace_id"]),
        ).fetchall()
        total = len(rows)
        verified = sum(row["status"] == "verified" for row in rows)
        failed = sum(row["status"] == "failed" for row in rows)
        pending = total - verified - failed
        if total and verified == total:
            status = "verified"
        elif verified:
            status = "partial"
        elif total and failed == total:
            status = "failed"
        else:
            status = "unverified"
        error = next((row["last_error"] for row in rows if row["last_error"]), None)
        metadata = next(
            (result for result in results if result["status"] == "verified" and result["identity_id"] == board.get("primary_identity_id")),
            None,
        ) or next((result for result in results if result["status"] == "verified"), None)
        owner = next((result for result in results if result["status"] == "verified" and result["is_owner"] is True), None)
        primary_identity_id = owner["identity_id"] if owner else board.get("primary_identity_id")
        timestamp = now()
        conn.execute(
            """
            UPDATE lark_boards
            SET listener_status = CASE
                  WHEN COALESCE(primary_identity_id, '') != COALESCE(?, '') THEN 'unverified'
                  ELSE listener_status
                END,
                listener_last_verified_at = CASE
                  WHEN COALESCE(primary_identity_id, '') != COALESCE(?, '') THEN NULL
                  ELSE listener_last_verified_at
                END,
                listener_failure_kind = CASE
                  WHEN COALESCE(primary_identity_id, '') != COALESCE(?, '') THEN NULL
                  ELSE listener_failure_kind
                END,
                listener_last_error = CASE
                  WHEN COALESCE(primary_identity_id, '') != COALESCE(?, '') THEN NULL
                  ELSE listener_last_error
                END,
                primary_identity_id = ?,
                table_id = COALESCE(?, table_id), view_id = COALESCE(?, view_id),
                access_status = ?, last_verified_at = ?, last_error = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                primary_identity_id,
                primary_identity_id,
                primary_identity_id,
                primary_identity_id,
                primary_identity_id,
                metadata.get("table_id") if metadata else None,
                metadata.get("view_id") if metadata else None,
                status,
                timestamp,
                error,
                timestamp,
                board["id"],
            ),
        )
    return {"status": status, "total": total, "verified": verified, "failed": failed, "pending": pending}


def _same_board_resource(conn: Any, board: dict[str, Any]) -> bool:
    current = conn.execute(
        "SELECT base_token, table_id, view_id FROM lark_boards WHERE id = ?",
        (board["id"],),
    ).fetchone()
    return current is not None and all(
        (current[key] or None) == (board.get(key) or None)
        for key in ("base_token", "table_id", "view_id")
    )


def _access_error_details(
    error: Exception,
    identity: dict[str, Any],
    board: dict[str, Any],
    stage: str,
) -> dict[str, Any]:
    message = str(error)
    lowered = message.lower()
    code = str(getattr(error, "code", ""))
    scopes = list(getattr(error, "missing_scopes", ()))
    repair_url = str(getattr(error, "repair_url", "") or "")
    if scopes or code in {"99991672", "99991679", "20027"} or "required one of" in lowered or "scope" in lowered:
        failure_kind = "missing_scope"
        if not scopes:
            scopes = ["docs:permission.member:auth" if stage == "collaborator" else "bitable:app"]
    elif code in {"91403", "1063002"} or any(value in lowered for value in ("91403", "1063002", "not a bitable collaborator", "not collaborator")):
        failure_kind = "not_collaborator"
    elif identity.get("auth_mode") == "user" and any(word in lowered for word in ("expired", "authorization", "token status")):
        failure_kind = "auth_expired"
    else:
        failure_kind = f"{stage}_failed"
    if failure_kind == "missing_scope":
        scopes = sorted(set(scopes).union(TEAMFLOW_APP_SCOPES))
        repair_url = _permission_url(identity, board, scopes)
    elif not repair_url:
        if failure_kind == "not_collaborator":
            repair_url = str(board.get("base_url") or "")
    return {
        "failure_kind": failure_kind,
        "missing_scopes": sorted(set(scopes)),
        "repair_url": repair_url or None,
        "last_error": message,
    }


def _permission_url(identity: dict[str, Any], board: dict[str, Any], scopes: list[str]) -> str:
    app_id = str(identity.get("app_id") or "").strip()
    if not app_id:
        return ""
    host = urlparse(str(board.get("base_url") or "")).hostname or ""
    origin = "https://open.larksuite.com" if host.endswith("larksuite.com") else "https://open.feishu.cn"
    query = urlencode({
        "q": ",".join(scopes),
        "op_from": "openapi",
        "token_type": "user" if identity.get("auth_mode") == "user" else "tenant",
    })
    return f"{origin}/app/{quote(app_id, safe='')}/auth?{query}"


def _lark_request_error(payload: dict[str, Any], message: str) -> LarkRequestError:
    if not payload and message.lstrip().startswith("{"):
        try:
            parsed = json.loads(message)
            payload = parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            pass
    error_data = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    violations = error_data.get("permission_violations") or payload.get("permission_violations") or []
    scopes = []
    repair_url = ""
    for violation in violations if isinstance(violations, list) else []:
        if not isinstance(violation, dict):
            continue
        scope = violation.get("scope") or violation.get("subject")
        if scope:
            scopes.append(str(scope))
        repair_url = repair_url or str(violation.get("url") or "")
    helps = error_data.get("helps") or payload.get("helps") or []
    for help_item in helps if isinstance(helps, list) else []:
        if isinstance(help_item, dict) and help_item.get("url"):
            repair_url = repair_url or str(help_item["url"])
    code = payload.get("code") or error_data.get("code") or ""
    detail = payload.get("msg") or error_data.get("message") or message
    return LarkRequestError(
        str(detail),
        code=str(code),
        missing_scopes=tuple(scopes),
        repair_url=repair_url,
    )


def initialize_lark_board(
    workspace: str | None,
    *,
    task_prefix: str | None = None,
    emit: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    def send(message: str) -> None:
        if emit:
            emit(message)

    paths, board, identity = _board_context(workspace)
    workflow_key = str(board["_workflow_key"])
    definition = load_workflow_definition(workflow_key)
    locale = _board_locale(board)
    prefix = normalize_task_prefix(task_prefix) if task_prefix is not None else default_task_prefix(
        board.get("_workspace_display_name"),
        paths.root,
    )
    field_specs = task_field_specs(definition, locale, task_prefix=prefix)
    field_aliases = task_field_aliases()
    try:
        send("正在读取飞书多维表格与数据表结构...")
        client = LarkBoardClient(identity, board)
        base = client.get_base()
        tables = client.list_tables()
        names = _task_names(board)
        send("正在选择可复用的数据表...")
        table, bundle, empty_records, created = _initialization_table(
            client,
            board,
            tables,
            names,
            field_specs,
            field_aliases,
        )
        adopted_empty_table = empty_records is not None
        if created:
            send(f"已创建数据表：{names['table']}")
        elif adopted_empty_table:
            send(f"正在使用空白数据表：{table.get('table_name') or table['table_id']}")
        else:
            send(f"正在使用已有数据表：{table.get('table_name') or table['table_id']}")
        if adopted_empty_table:
            if empty_records:
                send(f"正在删除 {len(empty_records)} 条默认空记录...")
            for record in empty_records:
                client.delete_record(table["table_id"], record["record_id"])
            if bundle["table"].get("table_name") != names["table"]:
                client.update_table(table["table_id"], names["table"])
                send(f"已将数据表命名为：{names['table']}")
                table["table_name"] = names["table"]
                bundle["table"]["table_name"] = names["table"]
        elif bundle["table"].get("table_name") == LEGACY_TASK_TABLE_NAME:
            client.update_table(table["table_id"], names["table"])
            send(f"已将数据表命名为：{names['table']}")
            table["table_name"] = names["table"]
            bundle["table"]["table_name"] = names["table"]

        primary = _primary_field(bundle)
        if adopted_empty_table and primary["field_name"] != names["primary"]:
            updated = client.update_field(table["table_id"], primary["field_id"], {"name": names["primary"], "type": "text"})
            primary.update(updated or {"field_name": names["primary"], "type": "text"})
            primary["field_name"] = names["primary"]
            send(f"已将主字段命名为：{names['primary']}")

        send("正在配置任务字段...")
        task_fields = _ensure_task_fields(
            client,
            table["table_id"],
            bundle,
            field_specs,
            field_aliases,
            emit=send,
        )
        configured_prefix = _task_prefix(task_fields["task_id"])
        if task_prefix is not None and configured_prefix and configured_prefix != prefix:
            raise ValueError(f"task prefix is already {configured_prefix}; TeamFlow will not change it automatically")
        prefix = configured_prefix or prefix

        views = bundle["views"] or client.list_views(table["table_id"])
        view = next((item for item in views if _is_task_view(item)), None)
        created_view = view is None
        if view is None:
            view = client.create_view(table["table_id"], names["view"])
            send(f"已创建看板视图：{names['view']}")
        elif view["view_name"] != names["view"]:
            renamed = client.rename_view(table["table_id"], view["view_id"], names["view"])
            view.update(renamed or {"view_name": names["view"]})
            view["view_name"] = names["view"]
            send(f"已将看板视图命名为：{names['view']}")
        for attempt in range(3 if created_view else 1):
            try:
                client.set_view_group(table["table_id"], view["view_id"], task_fields["status"]["field_id"])
                break
            except ValueError as error:
                if attempt == 2 or not created_view or "not_found" not in str(error).lower():
                    raise
                send("看板视图尚未就绪，正在重试...")
                time.sleep(0.5 * (attempt + 1))
        send("已按状态配置看板分组")
        base_url = _board_url(str(board.get("base_url") or base.get("url") or ""), table["table_id"], view["view_id"])
        _save_board_success(
            paths,
            board["id"],
            table_id=table["table_id"],
            view_id=view["view_id"],
            base_url=base_url,
        )
        send("多维表格初始化完成")
        return {
            "ok": True,
            "access_status": "verified",
            "base": base,
            "table": bundle["table"],
            "view": view,
            "fields": bundle["fields"],
            "board_url": base_url,
            "workflow_key": workflow_key,
            "task_prefix": prefix,
            "created_table": created,
            "reused_empty_table": adopted_empty_table and not created,
            "deleted_empty_records": len(empty_records or []),
        }
    except Exception as error:
        _save_board_failure(paths, board["id"], error)
        raise


def list_lark_tasks(workspace: str | None, *, limit: int = 100, offset: int = 0) -> dict[str, Any]:
    if not 1 <= limit <= 200:
        raise ValueError("limit must be between 1 and 200")
    if offset < 0:
        raise ValueError("offset must be zero or greater")
    paths, board, identity, client, fields, _, option_aliases = _task_context(workspace)
    try:
        data = client.list_records(board["table_id"], view_id=board.get("view_id"), limit=limit, offset=offset)
        _save_board_success(paths, board["id"], table_id=board["table_id"], view_id=board.get("view_id"))
        return {
            "ok": True,
            "tasks": [_task(record, fields, option_aliases) for record in _record_items(data)],
            "limit": limit,
            "offset": offset,
            "has_more": bool(data.get("has_more")),
        }
    except Exception as error:
        _save_board_failure(paths, board["id"], error)
        raise


def get_lark_task(workspace: str | None, *, record_id: str) -> dict[str, Any]:
    paths, board, identity, client, fields, _, option_aliases = _task_context(workspace)
    try:
        record = client.get_record(board["table_id"], record_id)
        _save_board_success(paths, board["id"], table_id=board["table_id"], view_id=board.get("view_id"))
        return {"ok": True, "task": _task(record, fields, option_aliases)}
    except Exception as error:
        _save_board_failure(paths, board["id"], error)
        raise


def upsert_lark_task(workspace: str | None, *, task: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
    unknown = set(task) - TASK_KEYS
    if unknown:
        raise ValueError(f"unsupported task fields: {', '.join(sorted(unknown))}")
    if not record_id and not str(task.get("title") or "").strip():
        raise ValueError("title is required when creating a task")
    if not task:
        raise ValueError("task fields are required")
    if "task_id" in task:
        raise ValueError("task_id is generated by Lark and cannot be written")

    paths, board, identity, client, fields, option_maps, option_aliases = _task_context(workspace)
    payload = {}
    for key, value in task.items():
        payload[fields[key]] = _task_write_value(key, value, option_maps, option_aliases)
    try:
        result = client.upsert_record(board["table_id"], payload, record_id=record_id)
        saved_id = str(result.get("record_id") or result.get("id") or record_id or "")
        if not saved_id:
            raise ValueError("Lark did not return the task record ID")
        record = _read_new_record(client, board["table_id"], saved_id)
        _save_board_success(paths, board["id"], table_id=board["table_id"], view_id=board.get("view_id"))
        return {"ok": True, "created": record_id is None, "task": _task(record, fields, option_aliases)}
    except Exception as error:
        _save_board_failure(paths, board["id"], error)
        raise


def _board_context(workspace: str | None) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    paths = resolve_workspace_paths(workspace)
    if not paths.db_path.exists():
        raise ValueError("TeamFlow workspace is not initialized")
    with connect(paths.db_path) as conn:
        bootstrap_workspace(conn)
        workspace_id = workspace_id_for_root(conn, paths.root)
        workspace_row = conn.execute("SELECT display_name FROM workspaces WHERE id = ?", (workspace_id,)).fetchone()
        board = conn.execute("SELECT * FROM lark_boards WHERE workspace_id = ?", (workspace_id,)).fetchone()
        if board is None or not board["base_token"]:
            raise ValueError("configure a Lark Bitable before accessing the board")
        snapshot_count = conn.execute(
            "SELECT COUNT(*) FROM lark_board_identity_access WHERE board_id = ?",
            (board["id"],),
        ).fetchone()[0]
        if not board["primary_identity_id"]:
            raise ValueError("the configured Bitable owner or manager identity has not been selected")
        identity = conn.execute(
            "SELECT * FROM lark_identities WHERE workspace_id = ? AND id = ?",
            (workspace_id, board["primary_identity_id"]),
        ).fetchone()
        access = conn.execute(
            "SELECT status FROM lark_board_identity_access WHERE board_id = ? AND identity_id = ?",
            (board["id"], board["primary_identity_id"]),
        ).fetchone()
        if identity is None or not lark_identity_is_usable(identity):
            raise ValueError("the Bitable primary identity is unavailable")
        if snapshot_count and (access is None or access["status"] != "verified"):
            raise ValueError("the Bitable primary identity does not have verified access")
        board_data = dict(board)
        board_data["_workflow_key"] = current_workflow_key(conn, workspace_id)
        board_data["_workspace_display_name"] = workspace_row["display_name"] if workspace_row else None
        return paths, board_data, dict(identity)


def _task_context(
    workspace: str | None,
) -> tuple[
    Any,
    dict[str, Any],
    dict[str, Any],
    LarkBoardClient,
    dict[str, str],
    dict[str, dict[str, str]],
    dict[str, dict[str, str]],
]:
    paths, board, identity = _board_context(workspace)
    if not board.get("table_id"):
        raise ValueError("initialize the TeamFlow board before accessing tasks")
    definition = load_workflow_definition(str(board["_workflow_key"]))
    locale = _board_locale(board)
    field_specs = task_field_specs(definition, locale)
    client = LarkBoardClient(identity, board)
    bundle = client.get_table(board["table_id"])
    return (
        paths,
        board,
        identity,
        client,
        _task_field_map(bundle, field_specs, task_field_aliases()),
        task_option_maps(definition, locale),
        task_option_aliases(definition),
    )


def _selected_table(board: dict[str, Any], tables: list[dict[str, Any]]) -> dict[str, Any] | None:
    if board.get("table_id"):
        return next((table for table in tables if table["table_id"] == board["table_id"]), {"table_id": board["table_id"], "table_name": ""})
    table_names = {_task_names(board)["table"], LEGACY_TASK_TABLE_NAME}
    return next((table for table in tables if table["table_name"] in table_names), None)


def _initialization_table(
    client: LarkBoardClient,
    board: dict[str, Any],
    tables: list[dict[str, Any]],
    names: dict[str, str],
    field_specs: dict[str, dict[str, Any]],
    field_aliases: dict[str, tuple[str, ...]],
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]] | None, bool]:
    selected = _selected_table(board, tables)
    if board.get("table_id"):
        bundle = client.get_table(selected["table_id"])
        return selected, bundle, _reusable_empty_records(client, bundle), False

    reusable = None
    for table in tables:
        bundle = client.get_table(table["table_id"])
        if _task_field_map(bundle, field_specs, field_aliases, strict=False):
            return table, bundle, None, False
        empty_records = _reusable_empty_records(client, bundle)
        if reusable is None and empty_records is not None:
            reusable = (table, bundle, empty_records, False)
    if reusable:
        return reusable

    table = client.create_table(names["table"], names["primary"])
    bundle = client.get_table(table["table_id"])
    return table, bundle, _reusable_empty_records(client, bundle), True


def _ensure_task_fields(
    client: LarkBoardClient,
    table_id: str,
    bundle: dict[str, Any],
    field_specs: dict[str, dict[str, Any]],
    field_aliases: dict[str, tuple[str, ...]],
    emit: Callable[[str], None] | None = None,
) -> dict[str, dict[str, Any]]:
    resolved = {}
    for key, spec in field_specs.items():
        field = _find_task_field(bundle["fields"], spec, field_aliases[key], strict=True)
        if field is None:
            field = client.create_field(table_id, spec)
            bundle["fields"].append(field)
            if emit:
                emit(f"已创建字段：{spec['name']}")
        elif spec["type"] == "select" and field["field_name"] == spec["name"]:
            if not field.get("options"):
                field.update(client.get_field(table_id, field["field_id"]))
            options = _merged_options(field.get("options") or [], spec["options"])
            if options is not None:
                updated = client.update_field(table_id, field["field_id"], {**spec, "options": options})
                field.update(updated)
                field["options"] = options
                if emit:
                    emit(f"已补充字段选项：{spec['name']}")
        resolved[key] = field
    return resolved


def _merged_options(existing: list[dict[str, Any]], desired: list[dict[str, Any]]) -> list[dict[str, Any]] | None:
    merged = []
    names = set()
    for option in existing:
        name = str(option.get("name") or "").strip()
        if not name:
            continue
        normalized = {"name": name}
        for key in ("hue", "lightness"):
            if option.get(key):
                normalized[key] = option[key]
        merged.append(normalized)
        names.add(name)
    missing = [option for option in desired if option["name"] not in names]
    return [*merged, *missing] if missing else None


def _task_prefix(field: dict[str, Any]) -> str | None:
    rules = (field.get("style") or {}).get("rules") or []
    text = "".join(str(rule.get("text") or "") for rule in rules if rule.get("type") == "text")
    return text[:-1] if text.endswith("-") else text or None


def _reusable_empty_records(client: LarkBoardClient, bundle: dict[str, Any]) -> list[dict[str, Any]] | None:
    if len(bundle["fields"]) != 1:
        return None
    primary = _primary_field(bundle)
    if primary["type"] != "text":
        return None

    records = []
    offset = 0
    while True:
        page = client.list_records(bundle["table"]["table_id"], limit=200, offset=offset)
        batch = _record_items(page)
        if any(not _record_is_empty(record) for record in batch):
            return None
        records.extend(batch)
        if not page.get("has_more"):
            return records
        if not batch:
            raise ValueError("Lark returned an invalid empty record page")
        offset += len(batch)


def _record_is_empty(record: dict[str, Any]) -> bool:
    fields = record.get("fields")
    return isinstance(fields, dict) and not any(_has_content(value) for value in fields.values())


def _has_content(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, set)):
        return any(_has_content(item) for item in value)
    if isinstance(value, dict):
        return any(_has_content(item) for item in value.values())
    return True


def _task_names(board: dict[str, Any]) -> dict[str, str]:
    return TASK_NAMES["zh" if _board_locale(board) == "zh-CN" else "en"]


def _board_locale(board: dict[str, Any]) -> str:
    host = urlparse(str(board.get("base_url") or "")).hostname or ""
    return "en" if host.endswith("larksuite.com") else "zh-CN"


def _is_task_view(view: dict[str, Any]) -> bool:
    return view.get("view_type") == "kanban" and view.get("view_name") in {
        TASK_NAMES["zh"]["view"],
        TASK_NAMES["en"]["view"],
        LEGACY_TASK_VIEW_NAME,
    }


def _task_field_map(
    bundle: dict[str, Any],
    field_specs: dict[str, dict[str, Any]],
    field_aliases: dict[str, tuple[str, ...]],
    *,
    strict: bool = True,
) -> dict[str, str] | None:
    try:
        primary = _primary_field(bundle)
    except ValueError:
        if strict:
            raise
        return None
    fields = {"title": primary["field_name"]}
    for key, spec in field_specs.items():
        field = _find_task_field(bundle["fields"], spec, field_aliases[key], strict=strict)
        if field is None:
            if strict:
                raise ValueError("initialize the TeamFlow board before accessing tasks")
            return None
        fields[key] = field["field_name"]
    return fields


def _find_task_field(
    fields: list[dict[str, Any]],
    spec: dict[str, Any],
    aliases: tuple[str, ...],
    *,
    strict: bool,
) -> dict[str, Any] | None:
    exact = next((field for field in fields if field["field_name"] == spec["name"]), None)
    if exact:
        if exact["type"] == spec["type"]:
            return exact
        if strict:
            raise ValueError(f"field {spec['name']} must be {spec['type']}, found {exact['type']}")
        return None
    return next(
        (field for field in fields if field["field_name"] in aliases and field["type"] == spec["type"]),
        None,
    )


def _primary_field(bundle: dict[str, Any]) -> dict[str, Any]:
    primary_id = bundle["table"].get("primary_field")
    primary = next((field for field in bundle["fields"] if field["field_id"] == primary_id), None)
    primary = primary or next((field for field in bundle["fields"] if field.get("is_primary")), None)
    primary = primary or next((field for field in bundle["fields"] if field["type"] == "text"), None)
    if primary is None:
        raise ValueError("the selected Lark table has no usable primary text field")
    return primary


def _task(
    record: dict[str, Any],
    fields: dict[str, str],
    option_aliases: dict[str, dict[str, str]],
) -> dict[str, Any]:
    values = record.get("fields") if isinstance(record.get("fields"), dict) else record
    task = {"record_id": record.get("record_id") or record.get("id")}
    for key, field_name in fields.items():
        value = _cell(values.get(field_name))
        aliases = option_aliases.get(key)
        if aliases and isinstance(value, list):
            value = value[0] if value else None
        task[key] = aliases.get(value, value) if aliases and isinstance(value, str) else value
    return task


def _task_write_value(
    key: str,
    value: Any,
    option_maps: dict[str, dict[str, str]],
    option_aliases: dict[str, dict[str, str]],
) -> Any:
    options = option_maps.get(key)
    if options is None or value is None or value == "":
        return value
    if not isinstance(value, str):
        raise ValueError(f"{key} must be a string")
    if value in options:
        return options[value]
    stable = option_aliases[key].get(value)
    if stable:
        return options[stable]
    raise ValueError(f"unsupported {key}: {value}. Supported values: {', '.join(options)}")


def _cell(value: Any) -> Any:
    if isinstance(value, dict) and "text" in value:
        return value["text"]
    if isinstance(value, list) and value and all(isinstance(item, dict) and "text" in item for item in value):
        return "".join(str(item["text"]) for item in value)
    return value


def _save_board_success(
    paths: Any,
    board_id: str,
    *,
    table_id: str | None,
    view_id: str | None,
    base_url: str | None = None,
) -> None:
    timestamp = now()
    with connect(paths.db_path) as conn:
        conn.execute(
            """
            UPDATE lark_boards
            SET table_id = ?, view_id = ?, base_url = COALESCE(?, base_url),
                last_error = NULL, updated_at = ?
            WHERE id = ?
            """,
            (table_id, view_id, base_url, timestamp, board_id),
        )


def _save_board_failure(paths: Any, board_id: str, error: Exception) -> None:
    timestamp = now()
    with connect(paths.db_path) as conn:
        conn.execute(
            "UPDATE lark_boards SET last_error = ?, updated_at = ? WHERE id = ?",
            (str(error), timestamp, board_id),
        )


def _board_url(base_url: str, table_id: str, view_id: str) -> str:
    parsed = urlparse(base_url)
    query = parse_qs(parsed.query)
    query["table"] = [table_id]
    query["view"] = [view_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("data")
    return data if isinstance(data, dict) else payload


def _record_page(data: dict[str, Any]) -> dict[str, Any]:
    rows = data.get("data")
    record_ids = data.get("record_id_list")
    field_names = data.get("fields")
    if isinstance(rows, list) and isinstance(record_ids, list) and isinstance(field_names, list):
        records = []
        for index, record_id in enumerate(record_ids):
            row = rows[index] if index < len(rows) else []
            if isinstance(row, list):
                fields = dict(zip(field_names, row))
            elif isinstance(row, dict):
                fields = dict(row.get("fields") or row)
            else:
                fields = {}
            records.append({"record_id": record_id, "fields": fields})
    else:
        records = _items(data)
    return {"records": records, "has_more": bool(data.get("has_more"))}


def _single_record(data: dict[str, Any], record_id: str | None = None) -> dict[str, Any]:
    if isinstance(data.get("record_id_list"), list):
        records = _record_page(data)["records"]
        if records:
            return records[0]
    if isinstance(data.get("fields"), dict):
        record = dict(data)
        if record_id:
            record.setdefault("record_id", record_id)
        return record
    if data.get("record_id") or data.get("id"):
        return dict(data)
    return {"record_id": record_id, "fields": dict(data)}


def _record_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    records = data.get("records")
    return [record for record in records if isinstance(record, dict)] if isinstance(records, list) else _items(data)


def _items(data: dict[str, Any]) -> list[dict[str, Any]]:
    for key in ("items", "tables", "fields", "views", "data", "records"):
        value = data.get(key)
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
    return []


def _table(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "table_id": item.get("table_id") or item.get("id"),
        "table_name": item.get("table_name") or item.get("name") or "",
        "primary_field": item.get("primary_field"),
        "revision": item.get("revision"),
    }


def _field(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "field_id": item.get("field_id") or item.get("id"),
        "field_name": item.get("field_name") or item.get("name") or "",
        "type": item.get("type") or "",
        "multiple": item.get("multiple"),
        "options": item.get("options") or [],
        "style": item.get("style") or {},
        "is_primary": bool(item.get("is_primary")),
    }


def _view(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "view_id": item.get("view_id") or item.get("id"),
        "view_name": item.get("view_name") or item.get("name") or "",
        "view_type": item.get("view_type") or item.get("type") or "",
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
