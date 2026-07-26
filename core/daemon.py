from __future__ import annotations

import fcntl
import hashlib
import json
import multiprocessing
import os
import queue
import secrets
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from .agent_runtime import (
    confirm_agent_context,
    find_agent_assignment,
    mark_agent_context_recovery_pending,
)
from .codex import (
    codex_thread_is_permanently_unavailable,
    codex_turn,
    codex_turn_unresolved_teamflow_mcp_failures,
    read_codex_thread,
    run_codex_turn,
    stop_codex_turn,
)
from .config import resolve_workspace_paths
from .db import bootstrap_workspace, connect, now
from .global_db import (
    claim_lark_event,
    cleanup_lark_events,
    due_lark_event_ids,
    finish_lark_event,
    lark_event_counts,
    record_lark_event,
    recover_lark_events,
    register_workspace,
    registered_workspaces,
    retry_lark_event,
    teamflow_home,
    workspace_enabled,
)
from .lark_board import get_lark_task, list_lark_tasks
from .lark_events import (
    LISTENER_CONNECT_TIMEOUT,
    LISTENER_EVENT_TIMEOUT,
    LISTENER_PROBE_ATTEMPTS,
    LarkEventContext,
    context_client,
    ensure_lark_board_subscription,
    event_matches_board,
    event_record_actions,
    event_record_ids,
    lark_event_context,
    lark_event_metadata,
    lark_listener_details,
    listener_failure,
    resolve_lark_event_table,
    run_lark_app_worker,
    save_board_schema_event,
    save_listener_result,
    save_task_snapshot,
    saved_task_record_ids,
    saved_task_snapshot,
)
from .task_dispatch import (
    cancel_task_delivery,
    claim_task_deliveries,
    defer_task_delivery_reconciliation,
    due_processing_task_deliveries,
    finish_task_delivery,
    mark_task_delivery_turn_started,
    prepare_agent_catchup_deliveries,
    prepare_task_deliveries,
    refresh_task_delivery_prompt,
    render_task_prompt,
    recover_task_deliveries,
    task_delivery_is_current,
    task_dispatch_target,
)
from .teamflow_tools import (
    MUTATING_TOOL_NAMES,
    RUNTIME_TOOL_NAMES,
    TOOL_NAMES,
    available_task_actions,
    block_task,
    cancel_task,
    claim_task,
    create_task,
    get_task,
    list_available_tasks,
    prepare_runtime_action,
    review_task,
    route_task,
    runtime_action_error,
    submit_task,
    update_task,
    workflow_contract,
)
from .workflow import load_workflow_definition


ROOT = Path(__file__).resolve().parents[1]
MAX_IPC_MESSAGE_BYTES = 1024 * 1024
QUOTED_LOG_FIELDS = {"agent", "app", "board", "reason", "socket", "table", "title", "workspace"}


def _style(text: str, code: str) -> str:
    if not sys.stdout.isatty() or "NO_COLOR" in os.environ:
        return text
    return f"\033[{code}m{text}\033[0m"


def _local_timestamp(value: str | None = None) -> str:
    moment = datetime.fromisoformat(value.replace("Z", "+00:00")) if value else datetime.now().astimezone()
    return moment.astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]


def _event_source(brand: str, event_type: str) -> str:
    source = "LARK" if brand == "larksuite" else "FEISHU"
    if event_type == "drive.file.bitable_record_changed_v1":
        change = "RECORD CHANGE" if brand == "larksuite" else "记录变更"
    elif event_type == "drive.file.bitable_field_changed_v1":
        change = "FIELD CHANGE" if brand == "larksuite" else "字段变更"
    else:
        change = event_type
    return f"{source} WEBSOCKET {change}"


def _emit_log(
    label: str,
    *,
    context: LarkEventContext | None = None,
    timestamp: str | None = None,
    fields: dict[str, Any] | None = None,
) -> None:
    parts = [_style(_local_timestamp(timestamp), "2")]
    if context:
        namespace = f"[{context.workspace_name or Path(context.workspace_root).name} @{context.workflow_key or '-'}]"
        parts.append(_style(namespace, "1"))
    parts.append(label)
    for key, value in (fields or {}).items():
        if value is None or value == "":
            continue
        rendered = json.dumps(str(value), ensure_ascii=False) if key in QUOTED_LOG_FIELDS else str(value)
        parts.append(f"{key}={rendered}")
    print(" ".join(parts), flush=True)


def _task_change(event_types: list[str]) -> str:
    if "task_created" in event_types:
        return "created"
    if "task_deleted" in event_types:
        return "deleted"
    return "updated" if event_types else "unchanged"


def _styled_task_change(change: str | None) -> str | None:
    if not change:
        return None
    return _style(change, {
        "created": "1;32",
        "updated": "1;33",
        "deleted": "1;31",
        "unchanged": "2",
    }.get(change, "2"))


def daemon_socket_path() -> Path:
    return teamflow_home() / "daemon.sock"


def _tool_input_hash(value: Any) -> str:
    payload = json.dumps(value if isinstance(value, dict) else {}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def _optional_text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


class TeamFlowDaemon:
    def __init__(self) -> None:
        self.mp = multiprocessing.get_context("spawn")
        self.event_queue = self.mp.Queue()
        self.workers: dict[str, dict[str, Any]] = {}
        self.routes: dict[str, LarkEventContext] = {}
        self.probe_records: dict[str, float] = {}
        self.verifying_workspaces: set[str] = set()
        self.recent: deque[tuple[int, str, dict[str, Any]]] = deque(maxlen=1000)
        self.sequence = 0
        self.condition = threading.Condition()
        self.sync_lock = threading.RLock()
        self.stopping = threading.Event()
        self.routes_ready = threading.Event()
        self.delivery_wakeup = threading.Event()
        self.active_sessions: set[str] = set()
        self.delivery_workers: dict[str, threading.Thread] = {}
        self.tool_grants: dict[str, dict[str, Any]] = {}
        self.tool_results: dict[str, dict[str, Any]] = {}
        self.task_locks: dict[str, threading.Lock] = {}
        self.runtime_locks: dict[tuple[str, str], threading.Lock] = {}
        self.last_cleanup = 0.0
        self.event_thread = threading.Thread(target=self._consume_events, name="teamflow-lark-events", daemon=True)
        self.delivery_thread = threading.Thread(target=self._consume_deliveries, name="teamflow-deliveries", daemon=True)
        self.event_thread.start()
        self.delivery_thread.start()

    @staticmethod
    def app_key(context: LarkEventContext) -> str:
        return f"{context.brand}:{context.app_id}"

    def sync_workspace(
        self,
        workspace: str | None,
        *,
        identity_id: str | None = None,
        reconcile: bool = True,
    ) -> dict[str, Any]:
        with self.sync_lock:
            context = lark_event_context(workspace, identity_id=identity_id)
            previous = self.routes.get(context.workspace_root)
            try:
                self._ensure_app(context)
                context = resolve_lark_event_table(context)
                already_subscribed = ensure_lark_board_subscription(context)
                route_changed = previous is None or (
                    self.app_key(previous), previous.file_token, previous.table_id
                ) != (
                    self.app_key(context), context.file_token, context.table_id
                )
                if (
                    previous
                    and not route_changed
                    and previous.board_name not in {"", previous.file_token}
                    and previous.table_name not in {"", previous.table_id}
                ):
                    context = replace(
                        context,
                        board_name=previous.board_name,
                        table_name=previous.table_name,
                    )
                elif not context.board_name or not context.table_name:
                    try:
                        details = lark_listener_details(context)
                        context = replace(
                            context,
                            board_name=str(details["board"]["name"]),
                            table_name=str(details["board"]["table_name"]),
                        )
                    except Exception:
                        context = replace(
                            context,
                            board_name=context.file_token,
                            table_name=context.table_id,
                        )
                reconciliation = self._reconcile_workspace(context) if reconcile and route_changed else None
            except Exception:
                if previous is None or self.app_key(previous) != self.app_key(context):
                    self._stop_unused_app(context)
                raise
            self.routes[context.workspace_root] = context
            if previous and self.app_key(previous) != self.app_key(context):
                self._stop_unused_app(previous)
            dispatch = None
            if workspace_enabled(context.workspace_root):
                try:
                    if previous is None:
                        recover_task_deliveries(context)
                    dispatch = self._consume_workspace_task_events(context)
                except Exception as error:
                    dispatch = {"error": str(error)}
                    self._log_dispatch(
                        context,
                        "failed",
                        event_id=None,
                        task={},
                        reason=str(error),
                    )
            return {
                "ok": True,
                "already_subscribed": already_subscribed,
                "daemon_pid": os.getpid(),
                "reconciliation": reconciliation,
                "dispatch": dispatch,
                **context.public(),
            }

    def enable_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        root = register_workspace(workspace, enabled=True)
        return {"enabled": True, **self.sync_workspace(root, identity_id=identity_id)}

    def disable_workspace(self, workspace: str | None) -> dict[str, Any]:
        root = register_workspace(workspace, enabled=False)
        with self.sync_lock:
            task_lock = self.task_locks.setdefault(root, threading.Lock())
        with task_lock:
            with self.sync_lock:
                context = self.routes.pop(root, None)
                if context:
                    self._stop_unused_app(context)
        return {"ok": True, "enabled": False, "workspace_root": root, "daemon_pid": os.getpid()}

    def finish_startup(self) -> None:
        _emit_log("DAEMON LISTENING", fields={"apps": len(self.workers), "workspaces": len(self.routes)})
        self.routes_ready.set()
        self.delivery_wakeup.set()

    def release_ephemeral_workspace(self, workspace: str | None) -> None:
        root = str(resolve_workspace_paths(workspace).root)
        if workspace_enabled(root):
            return
        with self.sync_lock:
            context = self.routes.pop(root, None)
            if context:
                self._stop_unused_app(context)

    def verify_workspace(self, workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
        context = None
        already_subscribed = False
        client = None
        record_id = ""
        cleaned = False
        probe_record_ids: set[str] = set()
        error = None
        try:
            synced = self.sync_workspace(workspace, identity_id=identity_id, reconcile=False)
            context = self.routes[synced["workspace_root"]]
            with self.sync_lock:
                self.verifying_workspaces.add(context.workspace_root)
            already_subscribed = bool(synced["already_subscribed"])
            client = context_client(context)
            cursor = self.cursor()
            for _ in range(LISTENER_PROBE_ATTEMPTS):
                cleaned = False
                created = client.upsert_record(context.table_id, {})
                record_id = str(created.get("record_id") or created.get("id") or "")
                if not record_id:
                    raise ValueError("Lark did not return the listener verification record ID")
                probe_record_ids.add(record_id)
                with self.sync_lock:
                    self.probe_records[record_id] = time.monotonic() + 300
                client.delete_record(context.table_id, record_id)
                cleaned = True
                record_id = ""
                if self.wait_for_records(context, probe_record_ids, cursor):
                    break
            else:
                raise ValueError("the app did not receive the Bitable record change event")
        except Exception as caught:
            error = caught
        finally:
            if context:
                with self.sync_lock:
                    self.verifying_workspaces.discard(context.workspace_root)
            if record_id and client and not cleaned:
                try:
                    client.delete_record(context.table_id, record_id)
                except Exception as cleanup_error:
                    error = ValueError(f"listener probe record cleanup failed: {cleanup_error}")

        timestamp = now()
        if error:
            result = {
                "ok": False,
                "status": "failed",
                "last_verified_at": timestamp,
                "already_subscribed": already_subscribed,
                **listener_failure(error, context),
            }
        else:
            result = {
                "ok": True,
                "status": "verified",
                "failure_kind": None,
                "last_error": None,
                "last_verified_at": timestamp,
                "repair_url": None,
                "already_subscribed": already_subscribed,
                **context.public(),
            }
        if context and not result.get("workspace_root"):
            result.update(context.public())
        try:
            save_listener_result(workspace, context.identity_id if context else identity_id, result)
        finally:
            self.release_ephemeral_workspace(workspace)
        return result

    def listener_details(self, workspace: str | None) -> dict[str, Any]:
        root = str(resolve_workspace_paths(workspace).root)
        context = self.routes.get(root)
        if context is None:
            self.sync_workspace(root, reconcile=False)
            context = self.routes[root]
        return lark_listener_details(context)

    def cursor(self) -> int:
        with self.condition:
            return self.sequence

    def wait_for_records(self, context: LarkEventContext, record_ids: set[str], cursor: int) -> bool:
        deadline = time.monotonic() + LISTENER_EVENT_TIMEOUT
        app_key = self.app_key(context)
        with self.condition:
            while True:
                for sequence, event_app_key, payload in self.recent:
                    if (
                        sequence > cursor
                        and event_app_key == app_key
                        and event_matches_board(payload, context.public())
                        and record_ids.intersection(event_record_ids(payload))
                    ):
                        return True
                worker = self.workers.get(app_key)
                if not worker or not worker["process"].is_alive():
                    raise ValueError("the TeamFlow Lark listener stopped before receiving the test event")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self.condition.wait(min(remaining, 0.5))

    def wait_for_workspace_event(self, workspace: str, cursor: int, timeout: float = 1.0) -> tuple[int, dict[str, Any]] | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                context = self.routes.get(workspace)
                if context is None:
                    raise ValueError("workspace is not synchronized with the TeamFlow daemon")
                app_key = self.app_key(context)
                for sequence, event_app_key, payload in self.recent:
                    if sequence > cursor and event_app_key == app_key and event_matches_board(payload, context.public()):
                        return sequence, payload
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self.stopping.is_set():
                    return None
                self.condition.wait(remaining)

    def publish(self, app_key: str, payload: dict[str, Any]) -> None:
        with self.condition:
            self.sequence += 1
            self.recent.append((self.sequence, app_key, payload))
            self.condition.notify_all()

    def status(self) -> dict[str, Any]:
        with self.sync_lock:
            apps = [
                {
                    "app_id": worker["context"].app_id,
                    "app_name": worker["context"].app_name,
                    "brand": worker["context"].brand,
                    "connected": worker["process"].is_alive() and worker["ready"].is_set(),
                }
                for worker in self.workers.values()
            ]
            routes = [
                {
                    "workspace_root": root,
                    "app_id": context.app_id,
                    "file_token": context.file_token,
                    "table_id": context.table_id,
                }
                for root, context in self.routes.items()
            ]
        return {
            "running": True,
            "pid": os.getpid(),
            "apps": apps,
            "workspaces": routes,
            "inbox": lark_event_counts(),
            "active_sessions": sorted(self.active_sessions),
        }

    def assignment_context(
        self,
        *,
        session_id: str,
        cwd: str | None,
        consume: bool,
        refresh: bool = False,
    ) -> dict[str, Any]:
        with self.sync_lock:
            workspaces = [
                root
                for root in self.routes
                if workspace_enabled(root)
            ]
        result = find_agent_assignment(
            workspaces,
            session_id=session_id,
            cwd=cwd,
            consume=consume,
            refresh=refresh,
        )
        return result or {"assignment": None, "additional_context": None}

    def confirm_assignment_context(
        self,
        *,
        workspace: str,
        agent_id: str,
        session_id: str,
        assignment_revision: int,
        context_fingerprint: str,
        context_kind: str | None,
    ) -> dict[str, Any]:
        result = confirm_agent_context(
            workspace,
            agent_id=agent_id,
            session_id=session_id,
            assignment_revision=assignment_revision,
            context_fingerprint=context_fingerprint,
        )
        assignment = result.get("assignment")
        if result["confirmed"] and assignment:
            _emit_log(
                _style(
                    "AGENT CONTEXT RESTORED" if context_kind == "recovery" else "AGENT ONBOARDED",
                    "1;32",
                ),
                fields={
                    "workspace": assignment["workspace_name"],
                    "workflow": assignment["workflow_key"],
                    "role": assignment["role_key"],
                    "agent": assignment["agent_id"],
                    "session": assignment["session_id"],
                    "revision": assignment["assignment_revision"],
                },
            )
        return result

    def compact_assignment_context(
        self,
        *,
        session_id: str,
        cwd: str | None,
    ) -> dict[str, Any]:
        context = self.assignment_context(
            session_id=session_id,
            cwd=cwd,
            consume=False,
        )
        assignment = context.get("assignment")
        if not assignment:
            return {"marked": False}
        result = mark_agent_context_recovery_pending(
            assignment["workspace_root"],
            agent_id=assignment["agent_id"],
            session_id=session_id,
            assignment_revision=assignment["assignment_revision"],
        )
        if result["marked"]:
            _emit_log(
                _style("AGENT CONTEXT RECOVERY PENDING", "1;33"),
                fields={
                    "workspace": assignment["workspace_name"],
                    "workflow": assignment["workflow_key"],
                    "role": assignment["role_key"],
                    "agent": assignment["agent_id"],
                    "session": assignment["session_id"],
                    "revision": assignment["assignment_revision"],
                },
            )
        return result

    def authorize_tool(
        self,
        *,
        invocation_id: str,
        session_id: str,
        cwd: str | None,
        turn_id: str | None,
        tool_name: str,
        tool_input: Any,
    ) -> dict[str, Any]:
        if not invocation_id:
            raise ValueError("TeamFlow MCP invocation ID is required")
        short_name = tool_name.rsplit("__", 1)[-1]
        if short_name not in TOOL_NAMES:
            raise ValueError(f"unsupported TeamFlow tool: {short_name}")
        context = self.assignment_context(session_id=session_id, cwd=cwd, consume=False)
        assignment = context.get("assignment")
        if not assignment:
            raise ValueError("this Codex session is not a registered agent in an enabled TeamFlow workspace")
        token = secrets.token_urlsafe(32)
        timestamp = time.monotonic()
        with self.sync_lock:
            self.tool_grants = {
                key: value
                for key, value in self.tool_grants.items()
                if float(value["expires_at"]) > timestamp
            }
            self.tool_results = {
                key: value
                for key, value in self.tool_results.items()
                if float(value["expires_at"]) > timestamp
            }
            self.tool_grants[token] = {
                "invocation_id": invocation_id,
                "session_id": session_id,
                "turn_id": turn_id,
                "tool_name": short_name,
                "input_hash": _tool_input_hash(tool_input),
                "assignment": assignment,
                "expires_at": timestamp + 60,
            }
        return {"grant": token, "expires_in": 60}

    def invoke_tool(
        self,
        *,
        invocation_id: str,
        grant: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        with self.sync_lock:
            authorized = self.tool_grants.pop(grant, None)
        if not authorized or float(authorized["expires_at"]) <= time.monotonic():
            raise ValueError("TeamFlow tool authorization is missing or expired")
        if authorized["invocation_id"] != invocation_id:
            raise ValueError("TeamFlow tool authorization does not match this invocation")
        if authorized["tool_name"] != tool_name:
            raise ValueError("TeamFlow tool authorization does not match this tool")
        if authorized["input_hash"] != _tool_input_hash(arguments):
            raise ValueError("TeamFlow tool arguments changed after authorization")
        with self.sync_lock:
            cached = self.tool_results.get(invocation_id)
        if cached:
            if (
                cached["session_id"] != authorized["session_id"]
                or cached["tool_name"] != tool_name
                or cached["input_hash"] != authorized["input_hash"]
            ):
                raise ValueError("TeamFlow invocation result does not match this tool call")
            return cached["result"]
        assignment = authorized["assignment"]
        workspace_root = str(assignment["workspace_root"])
        with self.sync_lock:
            workspace_active = workspace_root in self.routes
        if not workspace_active or not workspace_enabled(workspace_root):
            raise ValueError("the TeamFlow workspace was disabled before the tool ran")
        current = find_agent_assignment(
            [workspace_root],
            session_id=str(authorized["session_id"]),
        )
        if not current or current["assignment"]["agent_id"] != assignment["agent_id"]:
            raise ValueError("the TeamFlow agent assignment changed before the tool ran")
        if tool_name in RUNTIME_TOOL_NAMES:
            runtime_key = (workspace_root, str(arguments.get("record_id") or ""))
            with self.sync_lock:
                runtime_lock = self.runtime_locks.setdefault(runtime_key, threading.Lock())
            with runtime_lock:
                with self.sync_lock:
                    cached = self.tool_results.get(invocation_id)
                    workspace_active = workspace_root in self.routes
                if cached:
                    if (
                        cached["session_id"] != authorized["session_id"]
                        or cached["tool_name"] != tool_name
                        or cached["input_hash"] != authorized["input_hash"]
                    ):
                        raise ValueError("TeamFlow invocation result does not match this tool call")
                    return cached["result"]
                current = find_agent_assignment(
                    [workspace_root],
                    session_id=str(authorized["session_id"]),
                )
                if (
                    not workspace_active
                    or not workspace_enabled(workspace_root)
                    or not current
                    or current["assignment"]["agent_id"] != assignment["agent_id"]
                ):
                    raise ValueError("the TeamFlow assignment or workspace changed before the tool ran")
                result = self._invoke_teamflow_tool(
                    current["assignment"],
                    tool_name,
                    arguments,
                    invocation_id=invocation_id,
                )
                self._cache_tool_result(
                    invocation_id,
                    authorized=authorized,
                    tool_name=tool_name,
                    result=result,
                )
                return result
        if tool_name in MUTATING_TOOL_NAMES:
            with self.sync_lock:
                task_lock = self.task_locks.setdefault(workspace_root, threading.Lock())
            with task_lock:
                with self.sync_lock:
                    cached = self.tool_results.get(invocation_id)
                    workspace_active = workspace_root in self.routes
                if cached:
                    if (
                        cached["session_id"] != authorized["session_id"]
                        or cached["tool_name"] != tool_name
                        or cached["input_hash"] != authorized["input_hash"]
                    ):
                        raise ValueError("TeamFlow invocation result does not match this tool call")
                    return cached["result"]
                if not workspace_active or not workspace_enabled(workspace_root):
                    raise ValueError("the TeamFlow workspace was disabled before the tool ran")
                current = find_agent_assignment(
                    [workspace_root],
                    session_id=str(authorized["session_id"]),
                )
                if not current or current["assignment"]["agent_id"] != assignment["agent_id"]:
                    raise ValueError("the TeamFlow agent assignment changed before the tool ran")
                result = self._invoke_teamflow_tool(
                    current["assignment"],
                    tool_name,
                    arguments,
                    invocation_id=invocation_id,
                )
                self._sync_task_execution_activity(
                    current["assignment"],
                    tool_name=tool_name,
                    result=result,
                    session_id=str(authorized["session_id"]),
                    turn_id=str(authorized.get("turn_id") or "") or None,
                )
                self._cache_tool_result(
                    invocation_id,
                    authorized=authorized,
                    tool_name=tool_name,
                    result=result,
                )
                return result
        else:
            result = self._invoke_teamflow_tool(
                current["assignment"],
                tool_name,
                arguments,
                invocation_id=invocation_id,
            )
            self._sync_task_execution_activity(
                current["assignment"],
                tool_name=tool_name,
                result=result,
                session_id=str(authorized["session_id"]),
                turn_id=str(authorized.get("turn_id") or "") or None,
            )
        self._cache_tool_result(
            invocation_id,
            authorized=authorized,
            tool_name=tool_name,
            result=result,
        )
        return result

    def _cache_tool_result(
        self,
        invocation_id: str,
        *,
        authorized: dict[str, Any],
        tool_name: str,
        result: dict[str, Any],
    ) -> None:
        with self.sync_lock:
            self.tool_results[invocation_id] = {
                "session_id": authorized["session_id"],
                "tool_name": tool_name,
                "input_hash": authorized["input_hash"],
                "result": result,
                "expires_at": time.monotonic() + 300,
            }

    def _sync_task_execution_activity(
        self,
        assignment: dict[str, Any],
        *,
        tool_name: str,
        result: dict[str, Any],
        session_id: str,
        turn_id: str | None,
    ) -> None:
        if tool_name == "stop_task_execution" or not result.get("ok"):
            return
        task = result.get("task")
        if not isinstance(task, dict) or not task.get("record_id"):
            return
        definition = load_workflow_definition(str(assignment["workflow_key"]))
        execution_states = set(
            definition.get("runtime_actions", {})
            .get("stop_execution", {})
            .get("states", [])
        )
        paths = resolve_workspace_paths(assignment["workspace_root"])
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            if (
                task.get("status") in execution_states
                and task.get("agent_id") == assignment["agent_id"]
            ):
                conn.execute(
                    """
                    INSERT INTO task_executions (
                      record_id, agent_id, session_id, turn_id, state,
                      stop_status, stopped_by_agent_id, stop_reason,
                      updated_at, stopped_at
                    ) VALUES (?, ?, ?, ?, 'active', NULL, NULL, NULL, ?, NULL)
                    ON CONFLICT(record_id) DO UPDATE SET
                      agent_id = excluded.agent_id,
                      session_id = excluded.session_id,
                      turn_id = COALESCE(excluded.turn_id, task_executions.turn_id),
                      state = 'active',
                      stop_status = NULL,
                      stopped_by_agent_id = NULL,
                      stop_reason = NULL,
                      updated_at = excluded.updated_at,
                      stopped_at = NULL
                    """,
                    (
                        task["record_id"],
                        assignment["agent_id"],
                        session_id,
                        turn_id,
                        now(),
                    ),
                )
            elif task.get("status") not in execution_states:
                conn.execute(
                    "DELETE FROM task_executions WHERE record_id = ?",
                    (task["record_id"],),
                )

    def _invoke_teamflow_tool(
        self,
        assignment: dict[str, Any],
        tool_name: str,
        arguments: dict[str, Any],
        *,
        invocation_id: str,
    ) -> dict[str, Any]:
        record_id = str(arguments.get("record_id") or "")
        if tool_name == "get_assignment":
            return {
                "ok": True,
                "assignment": assignment,
                "workflow": workflow_contract(assignment),
            }
        if tool_name == "list_available_tasks":
            return list_available_tasks(assignment)
        if tool_name == "get_task":
            return get_task(assignment, record_id=record_id)
        if tool_name == "create_task":
            return create_task(
                assignment,
                title=str(arguments.get("title") or ""),
                task_type=_optional_text(arguments.get("task_type")),
                priority=_optional_text(arguments.get("priority")),
                role=_optional_text(arguments.get("role")),
                description=_optional_text(arguments.get("description")),
                context=_optional_text(arguments.get("context")),
                acceptance_criteria=_optional_text(arguments.get("acceptance_criteria")),
                dependencies=_optional_text(arguments.get("dependencies")),
                invocation_id=invocation_id,
            )
        if tool_name == "update_task":
            fields = arguments.get("fields")
            return update_task(
                assignment,
                record_id=record_id,
                fields=fields if isinstance(fields, dict) else {},
                invocation_id=invocation_id,
            )
        if tool_name == "route_task":
            task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
            return route_task(
                assignment,
                record_id=record_id,
                role=str(arguments.get("role") or ""),
                runtime_facts=self._task_runtime_facts(assignment, task),
                current_task=task,
                invocation_id=invocation_id,
            )
        if tool_name == "claim_task":
            return claim_task(
                assignment,
                record_id=record_id,
                invocation_id=invocation_id,
            )
        if tool_name == "submit_task":
            return submit_task(
                assignment,
                record_id=record_id,
                outcome=str(arguments.get("outcome") or ""),
                result_evidence=str(arguments.get("result_evidence") or ""),
                progress=_optional_text(arguments.get("progress")),
                next_action=_optional_text(arguments.get("next_action")),
                invocation_id=invocation_id,
            )
        if tool_name == "block_task":
            return block_task(
                assignment,
                record_id=record_id,
                waiting_on=str(arguments.get("waiting_on") or ""),
                blocked_reason=str(arguments.get("blocked_reason") or ""),
                next_action=str(arguments.get("next_action") or ""),
                progress=_optional_text(arguments.get("progress")),
                invocation_id=invocation_id,
            )
        if tool_name == "review_task":
            return review_task(
                assignment,
                record_id=record_id,
                decision=str(arguments.get("decision") or ""),
                result_evidence=str(arguments.get("result_evidence") or ""),
                role=_optional_text(arguments.get("role")),
                next_action=_optional_text(arguments.get("next_action")),
                invocation_id=invocation_id,
            )
        if tool_name == "stop_task_execution":
            task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
            prepared = prepare_runtime_action(
                assignment,
                action_key="stop_execution",
                task=task,
                payload={"reason": str(arguments.get("reason") or "")},
                confirmed=bool(arguments.get("confirmed")),
            )
            if not prepared["ok"]:
                return prepared
            agent_id = str(task["agent_id"])
            paths = resolve_workspace_paths(assignment["workspace_root"])
            with connect(paths.db_path) as conn:
                bootstrap_workspace(conn)
                agent = conn.execute(
                    "SELECT harness_type, session_id FROM agents WHERE id = ?",
                    (agent_id,),
                ).fetchone()
            if agent is None:
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=task,
                    category="business_rule",
                    code="executor_unavailable",
                    message=(
                        f"任务 {task.get('task_id') or record_id} 的原执行 Agent 已不存在，"
                        "无需发送停止请求；请直接按恢复或取消规则处理。"
                    ),
                )
            harness_type = str(agent["harness_type"])
            if harness_type != "codex":
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=task,
                    category="runtime",
                    code="unsupported_harness",
                    message=f"当前尚不能停止 {harness_type} Session；本次未记录停止事实。",
                )
            session_id = str(agent["session_id"])
            with connect(paths.db_path) as conn:
                execution = conn.execute(
                    """
                    SELECT *
                    FROM task_executions
                    WHERE record_id = ? AND agent_id = ? AND session_id = ?
                    """,
                    (record_id, agent_id, session_id),
                ).fetchone()
            if execution is None or not str(execution["turn_id"] or "").strip():
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=task,
                    category="runtime",
                    code="execution_turn_unknown",
                    message=(
                        f"无法确认任务 {task.get('task_id') or record_id} 当前对应的 Codex turn，"
                        "因此不会冒险中断该 Session 的其他工作。"
                        "请让执行 Agent 先通过 TeamFlow 工具读取该任务后再重试。"
                    ),
                    retryable=True,
                )
            expected_turn_id = str(execution["turn_id"])
            if execution["state"] == "stopped":
                return {
                    **prepared,
                    "message": (
                        f"任务 {task.get('task_id') or record_id} 的执行已处于停止状态，"
                        "无需重复中断。完成或安排好收尾后，可调用 cancel_task 并显式确认取消。"
                    ),
                    "already_applied": True,
                    "stop": {
                        "thread_id": session_id,
                        "turn_id": expected_turn_id,
                        "status": execution["stop_status"],
                        "already_stopped": True,
                    },
                    "available_actions": available_task_actions(assignment, task),
                }
            try:
                stopped = stop_codex_turn(
                    session_id,
                    expected_turn_id=expected_turn_id,
                )
            except Exception as error:
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=task,
                    category="runtime",
                    code="stop_failed",
                    message=(
                        f"未能确认任务 {task.get('task_id') or record_id} 的执行已停止：{error}。"
                        "请保持 Codex 客户端可用后重试。"
                    ),
                    retryable=True,
                )
            latest = get_lark_task(
                assignment["workspace_root"],
                record_id=record_id,
            )["task"]
            if (
                latest.get("status") != task.get("status")
                or latest.get("agent_id") != task.get("agent_id")
            ):
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=latest,
                    category="conflict",
                    code="task_changed_after_stop",
                    message=(
                        "执行已停止，但卡片的状态或执行 Agent 同时发生了变化，"
                        "本次未记录停止事实。请先读取最新卡片再决定下一步。"
                    ),
                    details={
                        "previous_state": task.get("status"),
                        "current_state": latest.get("status"),
                        "previous_agent_id": task.get("agent_id"),
                        "current_agent_id": latest.get("agent_id"),
                        "stop": stopped,
                    },
                    retryable=True,
                )
            with connect(paths.db_path) as conn:
                bootstrap_workspace(conn)
                stopped_at = now()
                cursor = conn.execute(
                    """
                    UPDATE task_executions
                    SET state = 'stopped',
                        stop_status = ?,
                        stopped_by_agent_id = ?,
                        stop_reason = ?,
                        updated_at = ?,
                        stopped_at = ?
                    WHERE record_id = ? AND agent_id = ? AND session_id = ?
                      AND turn_id = ? AND state = 'active'
                    """,
                    (
                        str(stopped.get("status") or "stopped"),
                        assignment["agent_id"],
                        str(arguments.get("reason") or "").strip(),
                        stopped_at,
                        stopped_at,
                        record_id,
                        agent_id,
                        session_id,
                        expected_turn_id,
                    ),
                )
            if cursor.rowcount != 1:
                return runtime_action_error(
                    assignment,
                    action_key="stop_execution",
                    task=latest,
                    category="conflict",
                    code="execution_changed_after_stop",
                    message=(
                        "目标 turn 已停止，但任务执行上下文同时发生了变化，"
                        "本次未记录停止事实。请先读取最新任务再决定下一步。"
                    ),
                    details={
                        "expected_turn_id": expected_turn_id,
                        "stop": stopped,
                    },
                    retryable=True,
                )
            return {
                **prepared,
                "message": (
                    f"任务 {task.get('task_id') or record_id} 的执行已确认停止。"
                    "完成或安排好收尾后，可调用 cancel_task 并显式确认取消。"
                ),
                "stop": stopped,
                "available_actions": available_task_actions(assignment, task),
            }
        if tool_name == "cancel_task":
            task = get_lark_task(assignment["workspace_root"], record_id=record_id)["task"]
            return cancel_task(
                assignment,
                record_id=record_id,
                result_evidence=str(arguments.get("result_evidence") or ""),
                confirmed=bool(arguments.get("confirmed")),
                runtime_facts=self._task_runtime_facts(assignment, task),
                current_task=task,
                invocation_id=invocation_id,
            )
        raise ValueError(f"unsupported TeamFlow tool: {tool_name}")

    def _task_runtime_facts(
        self,
        assignment: dict[str, Any],
        task: dict[str, Any],
    ) -> set[str]:
        definition = load_workflow_definition(str(assignment["workflow_key"]))
        guarded_states = {
            state
            for action in definition["lifecycle"]["actions"].values()
            for rule in action["rules"]
            if rule.get("guards")
            for state in rule.get("from", [])
        }
        if task.get("status") not in guarded_states:
            return set()
        agent_id = str(task.get("agent_id") or "")
        if not agent_id:
            return set()
        paths = resolve_workspace_paths(assignment["workspace_root"])
        with connect(paths.db_path) as conn:
            bootstrap_workspace(conn)
            agent = conn.execute(
                "SELECT harness_type, session_id FROM agents WHERE id = ?",
                (agent_id,),
            ).fetchone()
        if agent is None:
            return {"executor_unavailable", "execution_stopped"}
        if str(agent["harness_type"]) != "codex":
            return set()
        session_id = str(agent["session_id"] or "")
        with connect(paths.db_path) as conn:
            receipt = conn.execute(
                """
                SELECT 1 FROM task_executions
                WHERE record_id = ? AND agent_id = ? AND session_id = ?
                  AND state = 'stopped' AND turn_id IS NOT NULL
                """,
                (task.get("record_id"), agent_id, session_id),
            ).fetchone()
        if receipt:
            return {"execution_stopped"}
        if session_id in self.active_sessions:
            return set()
        try:
            thread = read_codex_thread(session_id)
        except Exception as error:
            if codex_thread_is_permanently_unavailable(error):
                return {"executor_unavailable", "execution_stopped"}
            return set()
        return set()

    def close(self) -> None:
        self.stopping.set()
        self.delivery_wakeup.set()
        with self.condition:
            self.condition.notify_all()
        with self.sync_lock:
            for worker in self.workers.values():
                self._stop_worker(worker)
            self.workers.clear()
        self.event_queue.put(None)
        self.event_thread.join(timeout=2)
        self.delivery_thread.join(timeout=2)
        for worker in list(self.delivery_workers.values()):
            worker.join(timeout=2)
        self.event_queue.close()

    def _ensure_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        credentials = (context.app_id, context.app_secret, context.brand)
        worker = self.workers.get(app_key)
        if worker and worker["process"].is_alive() and worker["credentials"] == credentials:
            return
        if worker:
            self._stop_worker(worker)

        ready = self.mp.Event()
        errors = self.mp.Queue()
        process = self.mp.Process(
            target=_lark_app_worker,
            args=(context, app_key, self.event_queue, ready, errors),
            daemon=True,
        )
        process.start()
        worker = {
            "context": context,
            "credentials": credentials,
            "process": process,
            "ready": ready,
            "errors": errors,
        }
        self.workers[app_key] = worker
        if not ready.wait(LISTENER_CONNECT_TIMEOUT):
            self._stop_worker(worker)
            self.workers.pop(app_key, None)
            raise ValueError("timed out while connecting to the Lark event stream")
        try:
            worker_error = errors.get_nowait()
        except queue.Empty:
            worker_error = None
        if worker_error or not process.is_alive():
            self._stop_worker(worker)
            self.workers.pop(app_key, None)
            raise ValueError(worker_error or "the Lark event stream stopped before synchronization")

    def _stop_worker(self, worker: dict[str, Any]) -> None:
        process = worker["process"]
        if process.is_alive():
            process.terminate()
        process.join(timeout=2)
        worker["errors"].close()

    def _stop_unused_app(self, context: LarkEventContext) -> None:
        app_key = self.app_key(context)
        if any(self.app_key(route) == app_key for route in self.routes.values()):
            return
        worker = self.workers.pop(app_key, None)
        if worker:
            self._stop_worker(worker)

    def _consume_events(self) -> None:
        while True:
            try:
                message = self.event_queue.get(timeout=1)
            except queue.Empty:
                message = None
            if message is None:
                if self.stopping.is_set():
                    return
            elif isinstance(message, dict) and isinstance(message.get("payload"), dict):
                self.publish(str(message.get("app_key") or ""), message["payload"])
                if self.routes_ready.is_set() and message.get("event_id"):
                    self._process_event(str(message["event_id"]))
            if not self.routes_ready.is_set():
                continue
            for event_id in due_lark_event_ids():
                self._process_event(event_id)
            if time.monotonic() - self.last_cleanup >= 86400:
                cleanup_lark_events()
                self.last_cleanup = time.monotonic()

    def _consume_deliveries(self) -> None:
        while not self.stopping.is_set():
            if not self.routes_ready.wait(0.5):
                continue
            with self.sync_lock:
                contexts = [
                    context
                    for context in self.routes.values()
                    if workspace_enabled(context.workspace_root)
                ]
            for context in contexts:
                if self.stopping.is_set():
                    return
                try:
                    self._reconcile_task_deliveries(context)
                    self._schedule_task_deliveries(context)
                except Exception as error:
                    self._log_dispatch(context, "failed", event_id=None, task={}, reason=str(error))
            self.delivery_wakeup.wait(0.5)
            self.delivery_wakeup.clear()

    def _schedule_task_deliveries(self, context: LarkEventContext) -> None:
        with self.sync_lock:
            deliveries = claim_task_deliveries(
                context,
                exclude_session_ids=set(self.active_sessions),
            )
            for delivery in deliveries:
                session_id = str(delivery["session_id"])
                self.active_sessions.add(session_id)
                worker = threading.Thread(
                    target=self._execute_task_delivery,
                    args=(context, delivery),
                    name=f"teamflow-delivery-{session_id[:8]}",
                    daemon=True,
                )
                self.delivery_workers[session_id] = worker
                worker.start()

    def _execute_task_delivery(self, context: LarkEventContext, delivery: dict[str, Any]) -> None:
        session_id = str(delivery["session_id"])
        turn_started = False
        started_turn_id = None
        canceled = False
        cancellation_reason = None
        task = json.loads(delivery["after_json"] or delivery["before_json"] or "{}")

        def save_turn(turn_id: str) -> None:
            nonlocal started_turn_id, turn_started
            mark_task_delivery_turn_started(
                context,
                delivery_id=int(delivery["id"]),
                turn_id=turn_id,
            )
            turn_started = True
            started_turn_id = turn_id
            self._log_dispatch(
                context,
                "started",
                event_id=delivery["source_event_id"],
                task=task,
                record_id=delivery["record_id"],
                target=str(delivery["role_key"]),
                agent=str(delivery["display_name"] or delivery["agent_id"]),
                session=session_id,
                turn=turn_id,
            )

        result = None
        error = None
        retry = False
        try:
            if delivery["harness_type"] != "codex":
                raise ValueError(f"unsupported task delivery harness: {delivery['harness_type']}")
            task = get_lark_task(
                context.workspace_root,
                record_id=str(delivery["record_id"]),
            )["task"]
            inserted_events = save_task_snapshot(
                context,
                record_id=str(delivery["record_id"]),
                task=task,
                source_event_id=f"teamflow-live-read:{delivery['id']}",
                source_revision=None,
            )
            if inserted_events:
                prepare_task_deliveries(context)
            if not task_delivery_is_current(
                context,
                delivery_id=int(delivery["id"]),
            ):
                canceled = True
                cancellation_reason = "卡片已有更新的状态事件，本次旧派发已取消"
                cancel_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=cancellation_reason,
                )
                return
            current_target = task_dispatch_target(context.workflow_key, task)
            if current_target != delivery["role_key"]:
                canceled = True
                cancellation_reason = (
                    f"卡片当前不再派发给 {delivery['role_key']}，"
                    f"最新目标为 {current_target or '无'}"
                )
                cancel_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    reason=cancellation_reason,
                )
                return
            prompt = render_task_prompt(
                context,
                event_type=str(delivery["event_type"]),
                event_key=str(delivery["event_key"]),
                workflow_key=context.workflow_key,
                role_name=str(delivery["role_name"] or delivery["role_key"]),
                task=task,
            )
            refresh_task_delivery_prompt(
                context,
                delivery_id=int(delivery["id"]),
                prompt=prompt,
            )
            result = run_codex_turn(
                session_id,
                prompt,
                on_started=save_turn,
                stop_event=self.stopping,
            )
            if not isinstance(result, dict):
                raise ValueError("Codex delivery returned an invalid result")
        except Exception as caught:
            error = caught
            retry = (
                not turn_started
                and delivery["harness_type"] == "codex"
                and not codex_thread_is_permanently_unavailable(caught)
            )
        finally:
            if canceled:
                pass
            elif turn_started and error:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=error,
                )
            else:
                finish_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    result=result,
                    error=error,
                    retry=retry,
                )
            if canceled:
                log_result = "not-required"
            elif turn_started and error:
                log_result = "reconciling"
            elif retry:
                log_result = "retry"
            else:
                log_result = "succeeded" if (result or {}).get("ok") else "failed"
            self._log_dispatch(
                context,
                log_result,
                event_id=delivery["source_event_id"],
                task=task,
                record_id=delivery["record_id"],
                target=str(delivery["role_key"]),
                agent=str(delivery["display_name"] or delivery["agent_id"]),
                session=session_id,
                turn=(result or {}).get("turn_id") or started_turn_id,
                transport=(result or {}).get("transport"),
                reason=(
                    cancellation_reason
                    if canceled
                    else str(error) if error else (result or {}).get("error")
                ),
                attempt=int(delivery["attempts"]) if int(delivery["attempts"]) > 1 or log_result != "succeeded" else None,
            )
            with self.sync_lock:
                self.active_sessions.discard(session_id)
                self.delivery_workers.pop(session_id, None)
            self.delivery_wakeup.set()

    def _reconcile_task_deliveries(self, context: LarkEventContext) -> None:
        for delivery in due_processing_task_deliveries(context):
            session_id = str(delivery["session_id"])
            task = json.loads(delivery["after_json"] or delivery["before_json"] or "{}")
            target = str(delivery.get("role_key") or task.get("role") or "")
            agent = str(delivery.get("display_name") or delivery["agent_id"])
            with self.sync_lock:
                if session_id in self.active_sessions:
                    continue
            try:
                thread = read_codex_thread(session_id, include_turns=True)
                turn = codex_turn(thread, str(delivery["turn_id"]))
            except Exception as error:
                if codex_thread_is_permanently_unavailable(error):
                    finish_task_delivery(
                        context,
                        delivery_id=int(delivery["id"]),
                        error=error,
                    )
                    self._log_dispatch(
                        context,
                        "failed",
                        event_id=delivery["source_event_id"],
                        task=task,
                        record_id=delivery["record_id"],
                        target=target,
                        agent=agent,
                        session=session_id,
                        turn=str(delivery["turn_id"]),
                        reason=str(error),
                    )
                else:
                    defer_task_delivery_reconciliation(
                        context,
                        delivery_id=int(delivery["id"]),
                        error=error,
                    )
                continue
            if turn is None:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                    error=ValueError("Codex turn is not visible yet"),
                )
                continue
            status_value = turn.get("status")
            status = str(status_value.get("type") if isinstance(status_value, dict) else status_value or "")
            if status in {"completed", "success"}:
                interrupted_tools = codex_turn_unresolved_teamflow_mcp_failures(turn)
                if interrupted_tools:
                    tool_names = ", ".join(
                        sorted({str(invocation["tool"]) for invocation in interrupted_tools})
                    )
                    error = ValueError(
                        f"TeamFlow MCP calls failed while the daemon was unavailable: {tool_names}"
                    )
                    finish_task_delivery(
                        context,
                        delivery_id=int(delivery["id"]),
                        result={"ok": False, "status": status},
                        error=error,
                        retry=True,
                    )
                    self._log_dispatch(
                        context,
                        "retry",
                        event_id=delivery["source_event_id"],
                        task=task,
                        record_id=delivery["record_id"],
                        target=target,
                        agent=agent,
                        session=session_id,
                        turn=str(delivery["turn_id"]),
                        reason=str(error),
                        attempt=int(delivery["attempts"]),
                    )
                    continue
                finish_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    result={"ok": True, "status": status},
                )
                self._log_dispatch(
                    context,
                    "recovered",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=session_id,
                    turn=str(delivery["turn_id"]),
                )
            elif status in {"failed", "cancelled", "canceled", "interrupted"}:
                error_data = turn.get("error") or {}
                error = ValueError(str(error_data.get("message") or error_data.get("additionalDetails") or status))
                finish_task_delivery(
                    context,
                    delivery_id=int(delivery["id"]),
                    result={"ok": False, "status": status},
                    error=error,
                )
                self._log_dispatch(
                    context,
                    "failed",
                    event_id=delivery["source_event_id"],
                    task=task,
                    record_id=delivery["record_id"],
                    target=target,
                    agent=agent,
                    session=session_id,
                    turn=str(delivery["turn_id"]),
                    reason=str(error),
                )
            else:
                defer_task_delivery_reconciliation(
                    context,
                    delivery_id=int(delivery["id"]),
                )

    def _process_event(self, event_id: str) -> None:
        item = claim_lark_event(event_id)
        if item is None:
            return
        routes = []
        summaries: dict[str, list[dict[str, Any]]] = {}
        try:
            app_key = f"{item['brand']}:{item['app_id']}"
            with self.sync_lock:
                routes = [
                    context
                    for context in self.routes.values()
                    if self.app_key(context) == app_key and event_matches_board(item["payload"], context.public())
                ]
                verifying = any(context.workspace_root in self.verifying_workspaces for context in routes)
            if not routes:
                finish_lark_event(event_id, status="ignored")
                worker = self.workers.get(app_key)
                app_name = worker["context"].app_name if worker else item["app_id"]
                _emit_log(
                    _style(f"{_event_source(str(item['brand']), str(item['event_type']))} UNROUTED", "1;31"),
                    timestamp=str(item["received_at"]),
                    fields={
                        "app": app_name,
                        "app_id": item["app_id"],
                        "file": item["file_token"],
                        "table": item["table_id"],
                        "event": event_id,
                        "reason": "未匹配到 workspace",
                    },
                )
                return
            if verifying:
                raise ValueError("listener verification is still cleaning up its probe record")
            for context in routes:
                summaries[context.workspace_root] = self._process_workspace_event(context, item["payload"])
                for summary in summaries[context.workspace_root]:
                    self._log_received(context, item, summary)
            finish_lark_event(event_id)
        except Exception as error:
            status = retry_lark_event(event_id, error)
            context = routes[0] if len(routes) == 1 else None
            fields = {
                "event": event_id,
                "attempt": item["attempts"],
                "reason": str(error),
            }
            if context is None:
                fields = {
                    "app_id": item["app_id"],
                    "file": item["file_token"],
                    "table": item["table_id"],
                    **fields,
                }
            _emit_log(
                _style(
                    f"{_event_source(str(item['brand']), str(item['event_type']))} {status.upper()}",
                    "1;31" if status == "failed" else "1;33",
                ),
                context=context,
                fields=fields,
            )
            return
        for context in routes:
            if not workspace_enabled(context.workspace_root):
                entries = summaries.get(context.workspace_root) or [{}]
                for summary in entries:
                    self._log_dispatch(
                        context,
                        "not-required",
                        event_id=event_id,
                        task=summary.get("task") or {},
                        record_id=summary.get("record_id"),
                        reason="workspace 已停用",
                    )
                continue
            try:
                self._consume_workspace_task_events(context, trigger_event_id=event_id)
            except Exception as error:
                self._log_dispatch(
                    context,
                    "failed",
                    event_id=event_id,
                    task={},
                    reason=str(error),
                )

    def _log_received(
        self,
        context: LarkEventContext,
        item: dict[str, Any],
        summary: dict[str, Any],
    ) -> None:
        task = summary.get("task") or {}
        task_id = task.get("task_id")
        _emit_log(
            f"{_style(_event_source(context.brand, str(item['event_type'])), '1;36')} RECEIVED",
            context=context,
            timestamp=str(item["received_at"]),
            fields={
                "event": item["event_id"],
                "board": context.board_name or context.file_token,
                "table": context.table_name or context.table_id,
                "task": task_id,
                "record": None if task_id else summary.get("record_id"),
                "title": task.get("title"),
                "change": _styled_task_change(summary.get("change")),
                "status": task.get("status"),
            },
        )

    def _log_dispatch(
        self,
        context: LarkEventContext,
        result: str,
        *,
        event_id: str | None,
        task: dict[str, Any],
        record_id: str | None = None,
        target: str | None = None,
        agent: str | None = None,
        session: str | None = None,
        reason: str | None = None,
        attempt: int | None = None,
        turn: str | None = None,
        transport: str | None = None,
    ) -> None:
        styles = {
            "not-required": "2",
            "waiting": "1;33",
            "started": "1;34",
            "retry": "1;33",
            "reconciling": "1;33",
            "recovered": "1;32",
            "succeeded": "1;32",
            "failed": "1;31",
        }
        task_id = task.get("task_id")
        _emit_log(
            _style(f"DISPATCH {result.upper()}", styles[result]),
            context=context,
            fields={
                "event": event_id,
                "task": task_id,
                "record": None if task_id else (record_id or task.get("record_id")),
                "target": target,
                "agent": agent,
                "session": session,
                "turn": turn,
                "transport": transport,
                "attempt": attempt,
                "reason": reason,
            },
        )

    def _process_workspace_event(
        self,
        context: LarkEventContext,
        payload: dict[str, Any],
    ) -> list[dict[str, Any]]:
        metadata = lark_event_metadata(payload)
        if metadata["event_type"] == "drive.file.bitable_field_changed_v1":
            created = save_board_schema_event(
                context,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            self._reconcile_workspace(
                context,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            return [{
                "record_id": None,
                "task": None,
                "change": None,
                "event_types": ["board_schema_changed"] if created else [],
            }]

        actions = event_record_actions(payload)
        summaries = []
        current_time = time.monotonic()
        with self.sync_lock:
            self.probe_records = {
                record_id: expires_at
                for record_id, expires_at in self.probe_records.items()
                if expires_at > current_time
            }
            probe_records = set(self.probe_records)
        for record_id in sorted(event_record_ids(payload)):
            if record_id in probe_records:
                continue
            action = actions.get(record_id, "").lower()
            deleted = "deleted" in action
            task = saved_task_snapshot(context, record_id) if deleted else get_lark_task(
                context.workspace_root, record_id=record_id
            )["task"]
            event_types = save_task_snapshot(
                context,
                record_id=record_id,
                task=None if deleted else task,
                source_event_id=str(metadata["event_id"]),
                source_revision=metadata["source_revision"],
            )
            summaries.append({
                "record_id": record_id,
                "task": task,
                "change": _task_change(event_types),
                "event_types": event_types,
            })
        return summaries

    def _reconcile_workspace(
        self,
        context: LarkEventContext,
        *,
        source_event_id: str | None = None,
        source_revision: str | None = None,
    ) -> dict[str, int]:
        if source_revision is None:
            table = next(
                (item for item in context_client(context).list_tables() if item["table_id"] == context.table_id),
                None,
            )
            revision = (table or {}).get("revision")
            source_revision = str(revision) if revision is not None else None

        tasks = []
        offset = 0
        while True:
            page = list_lark_tasks(context.workspace_root, limit=200, offset=offset)
            batch = page["tasks"]
            tasks.extend(batch)
            if not page["has_more"]:
                break
            if not batch:
                raise ValueError("Lark returned an empty task page with has_more=true")
            offset += len(batch)

        current_ids = {str(task["record_id"]) for task in tasks if task.get("record_id")}
        created_events = 0
        for task in tasks:
            record_id = str(task.get("record_id") or "")
            if not record_id:
                continue
            created_events += len(save_task_snapshot(
                context,
                record_id=record_id,
                task=task,
                source_event_id=source_event_id,
                source_revision=source_revision,
            ))
        deleted = saved_task_record_ids(context) - current_ids
        for record_id in deleted:
            created_events += len(save_task_snapshot(
                context,
                record_id=record_id,
                task=None,
                source_event_id=source_event_id,
                source_revision=source_revision,
            ))
        return {"tasks": len(tasks), "removed": len(deleted), "events": created_events}

    def _consume_workspace_task_events(
        self,
        context: LarkEventContext,
        *,
        trigger_event_id: str | None = None,
    ) -> dict[str, int]:
        result = prepare_task_deliveries(context)
        outcomes = result.pop("outcomes")
        grouped: dict[str, list[dict[str, Any]]] = {}
        if trigger_event_id:
            for outcome in outcomes:
                if outcome["source_event_id"] == trigger_event_id:
                    grouped.setdefault(str(outcome["record_id"] or "board"), []).append(outcome)
        for entries in grouped.values():
            selected = next((item for item in entries if item["result"] == "waiting"), None)
            if selected:
                target = str(selected["target"])
                reason = f"未注册 {target.upper()} Agent" if context.brand == "feishu" else f"no {target} agent is registered"
                self._log_dispatch(
                    context,
                    "waiting",
                    event_id=trigger_event_id,
                    task=selected["task"],
                    record_id=selected["record_id"],
                    target=target,
                    reason=reason,
                )
            elif not any(item["result"] == "routed" for item in entries):
                selected = entries[0]
                reason = "当前变更不通知 Agent" if context.brand == "feishu" else "this change does not notify an agent"
                self._log_dispatch(
                    context,
                    "not-required",
                    event_id=trigger_event_id,
                    task=selected["task"],
                    record_id=selected["record_id"],
                    reason=reason,
                )

        catchup = prepare_agent_catchup_deliveries(context)
        self.delivery_wakeup.set()
        return {**result, "catchup_deliveries": catchup}


def _lark_app_worker(context: LarkEventContext, app_key: str, events: Any, ready: Any, errors: Any) -> None:
    try:
        def checkpoint(payload: dict[str, Any]) -> None:
            metadata = lark_event_metadata(payload)
            record_lark_event(
                event_id=str(metadata["event_id"]),
                brand=context.brand,
                app_id=context.app_id,
                event_type=str(metadata["event_type"]),
                file_token=metadata["file_token"],
                table_id=metadata["table_id"],
                source_revision=metadata["source_revision"],
                payload=payload,
            )
            events.put({
                "app_key": app_key,
                "event_id": metadata["event_id"],
                "payload": payload,
            })

        run_lark_app_worker(
            context,
            emit=checkpoint,
            ready=ready.set,
        )
    except Exception as error:
        errors.put(str(error))
        ready.set()


class DaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, runtime: TeamFlowDaemon):
        self.runtime = runtime
        super().__init__(path, DaemonRequestHandler)


class DaemonRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(MAX_IPC_MESSAGE_BYTES + 1)
            if not line or len(line) > MAX_IPC_MESSAGE_BYTES:
                self._write({"ok": False, "error": "invalid TeamFlow daemon request"})
                return
            request = json.loads(line)
            action = request.get("action")
            if action == "listen":
                self._listen(request)
                return
            if action == "ping":
                result = self.server.runtime.status()
            elif action == "enable_workspace":
                result = self.server.runtime.enable_workspace(
                    request.get("workspace"), identity_id=request.get("identity_id")
                )
            elif action == "disable_workspace":
                result = self.server.runtime.disable_workspace(request.get("workspace"))
            elif action == "sync_workspace":
                result = self.server.runtime.sync_workspace(request.get("workspace"), identity_id=request.get("identity_id"))
            elif action == "verify_listener":
                result = self.server.runtime.verify_workspace(request.get("workspace"), identity_id=request.get("identity_id"))
            elif action == "assignment_context":
                result = self.server.runtime.assignment_context(
                    session_id=str(request.get("session_id") or ""),
                    cwd=request.get("cwd"),
                    consume=bool(request.get("consume")),
                    refresh=bool(request.get("refresh")),
                )
            elif action == "confirm_assignment_context":
                result = self.server.runtime.confirm_assignment_context(
                    workspace=str(request.get("workspace") or ""),
                    agent_id=str(request.get("agent_id") or ""),
                    session_id=str(request.get("session_id") or ""),
                    assignment_revision=int(request.get("assignment_revision") or 0),
                    context_fingerprint=str(request.get("context_fingerprint") or ""),
                    context_kind=request.get("context_kind"),
                )
            elif action == "compact_assignment_context":
                result = self.server.runtime.compact_assignment_context(
                    session_id=str(request.get("session_id") or ""),
                    cwd=request.get("cwd"),
                )
            elif action == "authorize_tool":
                result = self.server.runtime.authorize_tool(
                    invocation_id=str(request.get("invocation_id") or ""),
                    session_id=str(request.get("session_id") or ""),
                    cwd=request.get("cwd"),
                    turn_id=request.get("turn_id"),
                    tool_name=str(request.get("tool_name") or ""),
                    tool_input=request.get("tool_input"),
                )
            elif action == "invoke_tool":
                result = self.server.runtime.invoke_tool(
                    invocation_id=str(request.get("invocation_id") or ""),
                    grant=str(request.get("grant") or ""),
                    tool_name=str(request.get("tool_name") or ""),
                    arguments=request.get("arguments") if isinstance(request.get("arguments"), dict) else {},
                )
            elif action == "shutdown":
                result = {"stopping": True, **self.server.runtime.status()}
                self._write({"ok": True, "result": result})
                threading.Thread(target=self.server.shutdown, name="teamflow-daemon-stop", daemon=True).start()
                return
            else:
                raise ValueError(f"unknown TeamFlow daemon action: {action}")
            self._write({"ok": True, "result": result})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as error:
            try:
                self._write({"ok": False, "error": str(error)})
            except (BrokenPipeError, ConnectionResetError):
                return

    def _listen(self, request: dict[str, Any]) -> None:
        workspace = str(resolve_workspace_paths(request.get("workspace")).root)
        details = self.server.runtime.listener_details(workspace)
        self._write({"ok": True, "result": {"details": details}})
        cursor = self.server.runtime.cursor()
        try:
            while not self.server.runtime.stopping.is_set():
                item = self.server.runtime.wait_for_workspace_event(workspace, cursor)
                if item is None:
                    continue
                cursor, payload = item
                self._write({"ok": True, "event": payload})
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.server.runtime.release_ephemeral_workspace(workspace)

    def _write(self, payload: dict[str, Any]) -> None:
        self.wfile.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode() + b"\n")
        self.wfile.flush()


def run_daemon() -> int:
    home = _ensure_home()
    lock_path = home / "daemon.lock"
    socket_path = daemon_socket_path()
    pid_path = home / "daemon.pid"
    with lock_path.open("a+", encoding="utf-8") as lock:
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError("TeamFlow daemon is already running") from error
        recover_lark_events()
        cleanup_lark_events()
        if socket_path.exists():
            socket_path.unlink()
        runtime = TeamFlowDaemon()
        server = DaemonServer(str(socket_path), runtime)
        os.chmod(socket_path, 0o600)
        pid_path.write_text(str(os.getpid()), encoding="utf-8")
        os.chmod(pid_path, 0o600)
        _emit_log("DAEMON RUNNING", fields={"pid": os.getpid(), "socket": socket_path})
        threading.Thread(target=_sync_registered_workspaces, args=(runtime,), name="teamflow-workspace-sync", daemon=True).start()
        interrupted = False
        try:
            server.serve_forever(poll_interval=0.25)
        except KeyboardInterrupt:
            interrupted = True
            _emit_log("DAEMON STOPPING")
        finally:
            runtime.close()
            server.server_close()
            if socket_path.exists():
                socket_path.unlink()
            if pid_path.exists():
                pid_path.unlink()
    return 130 if interrupted else 0


def ensure_daemon() -> dict[str, Any]:
    status = daemon_status()
    if status["running"]:
        return status
    home = _ensure_home()
    log_path = home / "daemon.log"
    with log_path.open("ab", buffering=0) as log:
        subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "teamflow.py"), "daemon", "run"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
            env=os.environ.copy(),
        )
    os.chmod(log_path, 0o600)
    deadline = time.monotonic() + LISTENER_CONNECT_TIMEOUT
    while time.monotonic() < deadline:
        status = daemon_status()
        if status["running"]:
            return status
        time.sleep(0.1)
    raise ValueError(f"TeamFlow daemon did not start; see {log_path}")


def daemon_status() -> dict[str, Any]:
    try:
        return _daemon_request({"action": "ping"}, timeout=1)
    except (OSError, ValueError, TimeoutError):
        return {
            "running": False,
            "pid": None,
            "apps": [],
            "workspaces": [],
            "inbox": lark_event_counts(),
        }


def stop_daemon() -> dict[str, Any]:
    if not daemon_status()["running"]:
        return {"running": False, "stopping": False}
    return _daemon_request({"action": "shutdown"}, timeout=2)


def sync_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    ensure_daemon()
    root = register_workspace(workspace)
    return _daemon_request({"action": "sync_workspace", "workspace": root, "identity_id": identity_id}, timeout=30)


def enable_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    root = register_workspace(workspace, enabled=True)
    try:
        ensure_daemon()
        return _daemon_request(
            {"action": "enable_workspace", "workspace": root, "identity_id": identity_id},
            timeout=60,
        )
    except Exception:
        register_workspace(root, enabled=False)
        raise


def disable_daemon_workspace(workspace: str | None) -> dict[str, Any]:
    root = register_workspace(workspace, enabled=False)
    if not daemon_status()["running"]:
        return {"ok": True, "running": False, "enabled": False, "workspace_root": root}
    return _daemon_request({"action": "disable_workspace", "workspace": root}, timeout=10)


def verify_daemon_workspace(workspace: str | None, *, identity_id: str | None = None) -> dict[str, Any]:
    was_running = daemon_status()["running"]
    ensure_daemon()
    root = register_workspace(workspace)
    try:
        return _daemon_request(
            {"action": "verify_listener", "workspace": root, "identity_id": identity_id},
            timeout=90,
        )
    finally:
        if not was_running and not registered_workspaces(enabled_only=True):
            stop_daemon()


def stream_daemon_events(
    workspace: str | None,
    *,
    emit: Callable[[dict[str, Any]], None],
    ready: Callable[[dict[str, Any]], None] | None = None,
) -> None:
    synced = sync_daemon_workspace(workspace)
    request = {"action": "listen", "workspace": synced["workspace_root"]}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(str(daemon_socket_path()))
        stream = client.makefile("rwb")
        stream.write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        first = _read_response(stream.readline())
        if ready:
            ready(first["result"]["details"])
        for line in stream:
            response = _read_response(line)
            emit(response["event"])


def _daemon_request(payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(daemon_socket_path()))
        stream = client.makefile("rwb")
        stream.write(json.dumps(payload, separators=(",", ":")).encode() + b"\n")
        stream.flush()
        return _read_response(stream.readline())["result"]


def _read_response(line: bytes) -> dict[str, Any]:
    if not line:
        raise ValueError("TeamFlow daemon closed the connection without a response")
    response = json.loads(line)
    if not response.get("ok"):
        raise ValueError(response.get("error") or "TeamFlow daemon request failed")
    return response


def _sync_registered_workspaces(runtime: TeamFlowDaemon) -> None:
    try:
        for workspace in registered_workspaces(enabled_only=True):
            try:
                result = runtime.sync_workspace(workspace)
                _emit_log(
                    "WORKSPACE ENABLED",
                    context=runtime.routes.get(str(result["workspace_root"])),
                    fields={"board": result.get("board_name"), "table": result.get("table_name")},
                )
            except Exception as error:
                _emit_log(
                    _style("WORKSPACE SKIPPED", "1;31"),
                    fields={"workspace": workspace, "reason": str(error)},
                )
    finally:
        runtime.finish_startup()


def _ensure_home() -> Path:
    home = teamflow_home()
    home.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    return home
