from __future__ import annotations

import os
import socket
import stat
import subprocess
import threading
import uuid
from typing import Any, Callable, Iterable

from .codex_app_server import CodexAppServerRuntime
from .codex_app_server_protocol import (
    call as _protocol_call,
    codex_executable as _codex_executable,
    read_payload as _read_payload,
    read_turn_payload as _protocol_read_turn_payload,
    response_for as _protocol_response_for,
    send as _send,
    start_app_server as _protocol_start_app_server,
    stop_app_server as _stop_app_server,
)
from .codex_ipc import (
    CodexIpcConnection as _CodexIpcConnection,
    CodexIpcNoOwner as _CodexIpcNoOwner,
    CodexThreadStream as _CodexThreadStream,
    notify_codex_clients_thread_changed,
    run_codex_ipc_turn,
    stop_codex_ipc_turn,
)
from .codex_permissions import require_teamflow_mcp_authorization
from .codex_rollout import (
    codex_background_turn_permissions,
    codex_delivery_error_is_terminal,
    codex_developer_context_evidence,
    codex_thread_error,
    codex_thread_is_permanently_unavailable,
    codex_thread_name,
    codex_thread_settings,
    codex_turn,
    codex_turn_by_client_message_id,
    codex_turn_unresolved_teamflow_mcp_failures,
)


_APP_SERVER_RUNTIME = CodexAppServerRuntime(
    resolve=lambda name: globals()[name],
)
_ACTIVE_APP_SERVER_TURNS_LOCK = _APP_SERVER_RUNTIME.active_lock
_ACTIVE_APP_SERVER_TURNS = _APP_SERVER_RUNTIME.active_turns


def read_codex_thread(
    thread_id: str,
    *,
    include_turns: bool = False,
) -> dict[str, Any]:
    process = _start_app_server()
    try:
        try:
            result = _call(
                process,
                2,
                "thread/read",
                {
                    "threadId": thread_id,
                    "includeTurns": include_turns,
                },
            )
        except ValueError as error:
            if "thread not loaded" not in str(error).lower():
                raise
            result = _call(
                process,
                3,
                "thread/resume",
                {"threadId": thread_id},
            )
            if include_turns:
                result = _call(
                    process,
                    4,
                    "thread/read",
                    {
                        "threadId": thread_id,
                        "includeTurns": True,
                    },
                )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ValueError("Codex app-server did not return a thread")
        return thread
    finally:
        _stop_app_server(process)


def list_codex_threads(
    cwd: str,
    *,
    archived: bool = False,
) -> list[dict[str, Any]]:
    threads: list[dict[str, Any]] = []
    cursor = None
    while True:
        params: dict[str, Any] = {
            "archived": archived,
            "cwd": cwd,
            "limit": 100,
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "useStateDbOnly": True,
        }
        if cursor:
            params["cursor"] = cursor
        result = _request("thread/list", params)
        page = result.get("data")
        if not isinstance(page, list):
            raise ValueError("Codex app-server did not return a thread list")
        threads.extend(
            thread
            for thread in page
            if isinstance(thread, dict)
        )
        cursor = result.get("nextCursor")
        if not cursor:
            return threads


def run_codex_turn(
    thread_id: str,
    message: str,
    *,
    client_message_id: str | None = None,
    on_started: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
    required_mcp_tools: Iterable[str] = (),
) -> dict[str, Any]:
    thread = thread_id.strip()
    prompt = message.strip()
    if not thread:
        raise ValueError("thread_id is required")
    if not prompt:
        raise ValueError("message is required")

    message_id = client_message_id or str(uuid.uuid4())
    required_tools = tuple(required_mcp_tools)
    if required_tools:
        require_teamflow_mcp_authorization(required_tools)

    try:
        return _run_codex_ipc_turn(
            thread,
            prompt,
            client_message_id=message_id,
            on_started=on_started,
            stop_event=stop_event,
        )
    except _CodexIpcNoOwner:
        return _run_codex_app_server_turn(
            thread,
            prompt,
            client_message_id=message_id,
            on_started=on_started,
            stop_event=stop_event,
        )


def stop_codex_turn(
    thread_id: str,
    *,
    expected_turn_id: str,
) -> dict[str, Any]:
    thread = thread_id.strip()
    expected_turn = expected_turn_id.strip()
    if not thread:
        raise ValueError("thread_id is required")
    if not expected_turn:
        raise ValueError("expected_turn_id is required")
    with _ACTIVE_APP_SERVER_TURNS_LOCK:
        app_server_owned = thread in _ACTIVE_APP_SERVER_TURNS
    if app_server_owned:
        return _APP_SERVER_RUNTIME.stop_turn(
            thread,
            expected_turn_id=expected_turn,
        )
    try:
        return _stop_codex_ipc_turn(
            thread,
            expected_turn_id=expected_turn,
        )
    except _CodexIpcNoOwner:
        return _APP_SERVER_RUNTIME.stop_turn(
            thread,
            expected_turn_id=expected_turn,
        )


def _run_codex_app_server_turn(
    thread: str,
    prompt: str,
    *,
    client_message_id: str,
    on_started: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    permissions = codex_background_turn_permissions(thread)
    return _APP_SERVER_RUNTIME.run_turn(
        thread,
        prompt,
        client_message_id=client_message_id,
        on_started=on_started,
        stop_event=stop_event,
        approval_policy=str(permissions["approvalPolicy"]),
        permission_profile=str(permissions["permissions"]),
    )


def _interrupt_codex_app_server_turn(
    thread: str,
    *,
    expected_turn_id: str,
) -> dict[str, Any]:
    return _APP_SERVER_RUNTIME.interrupt_turn(
        thread,
        expected_turn_id=expected_turn_id,
    )


def _interrupt_competing_codex_turn(
    thread: str,
    turn_id: str,
) -> dict[str, Any]:
    return _APP_SERVER_RUNTIME.interrupt_exact_turn(
        thread,
        turn_id=turn_id,
    )


def _run_codex_ipc_turn(
    thread: str,
    prompt: str,
    *,
    client_message_id: str,
    on_started: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    return run_codex_ipc_turn(
        thread,
        prompt,
        connection_type=_CodexIpcConnection,
        client_message_id=client_message_id,
        interrupt_competing_turn=_interrupt_competing_codex_turn,
        on_started=on_started,
        stop_event=stop_event,
    )


def _stop_codex_ipc_turn(
    thread: str,
    *,
    expected_turn_id: str,
) -> dict[str, Any]:
    return stop_codex_ipc_turn(
        thread,
        expected_turn_id=expected_turn_id,
        connection_type=_CodexIpcConnection,
    )


def _notify_codex_clients_thread_changed(thread_id: str) -> None:
    notify_codex_clients_thread_changed(
        thread_id,
        connection_type=_CodexIpcConnection,
    )


def _request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    process = _start_app_server()
    try:
        return _call(process, 2, method, params)
    finally:
        _stop_app_server(process)


def _start_app_server() -> subprocess.Popen[bytes]:
    return _protocol_start_app_server(
        executable=_codex_executable,
        send_payload=_send,
        response_reader=_response_for,
        stop_process=_stop_app_server,
    )


def _call(
    process: subprocess.Popen[bytes],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _protocol_call(
        process,
        request_id,
        method,
        params,
        pending=pending,
        send_payload=_send,
        response_reader=_response_for,
    )


def _response_for(
    process: subprocess.Popen[bytes],
    request_id: int,
    *,
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return _protocol_response_for(
        process,
        request_id,
        pending=pending,
        read=_read_payload,
    )


def _read_turn_payload(
    process: subprocess.Popen[bytes],
    stop_event: threading.Event | None,
    *,
    wake_event: threading.Event | None = None,
) -> dict[str, Any]:
    return _protocol_read_turn_payload(
        process,
        stop_event,
        wake_event=wake_event,
        read=_read_payload,
    )
