from __future__ import annotations

import json
import os
import select
import socket
import stat
import struct
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterator


_CODEX_IPC_FRAME_LIMIT = 256 * 1024 * 1024
_CODEX_IPC_STREAM_VERSION = 11
_CODEX_IPC_FOLLOWING_VERSION = 1
_CODEX_IPC_FOLLOWING_STATUS_VERSION = 1
_CODEX_IPC_START_TURN_VERSION = 1
_CODEX_IPC_READ_STATE_VERSION = 2
_TERMINAL_TURN_STATUSES = {"completed", "failed", "interrupted", "cancelled", "canceled"}


def read_codex_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
    process = _start_app_server()
    try:
        try:
            result = _call(
                process,
                2,
                "thread/read",
                {"threadId": thread_id, "includeTurns": include_turns},
            )
        except ValueError as error:
            if "thread not loaded" not in str(error).lower():
                raise
            result = _call(process, 3, "thread/resume", {"threadId": thread_id})
            if include_turns:
                result = _call(
                    process,
                    4,
                    "thread/read",
                    {"threadId": thread_id, "includeTurns": True},
                )
        thread = result.get("thread")
        if not isinstance(thread, dict):
            raise ValueError("Codex app-server did not return a thread")
        return thread
    finally:
        _stop_app_server(process)


def list_codex_threads(cwd: str, *, archived: bool = False) -> list[dict[str, Any]]:
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
        threads.extend(thread for thread in page if isinstance(thread, dict))
        cursor = result.get("nextCursor")
        if not cursor:
            return threads


def codex_thread_name(thread: dict[str, Any]) -> str | None:
    name = str(thread.get("name") or "").strip()
    if name:
        return name
    preview = str(thread.get("preview") or "").strip()
    return preview.splitlines()[0] if preview else None


def codex_thread_settings(thread: dict[str, Any]) -> dict[str, str]:
    path_value = str(thread.get("path") or "").strip()
    if not path_value:
        return {}
    fallback = {}
    try:
        lines = _reverse_lines(Path(path_value).expanduser())
        for raw_line in lines:
            if b'"thread_settings"' not in raw_line and b'"turn_context"' not in raw_line:
                continue
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            payload = record.get("payload")
            if not isinstance(payload, dict):
                continue
            settings = payload.get("thread_settings")
            if isinstance(settings, dict):
                return {
                    key: str(value)
                    for key, value in {
                        "model": settings.get("model"),
                        "effort": settings.get("reasoning_effort") or settings.get("effort"),
                        "service_tier": settings.get("service_tier"),
                    }.items()
                    if value is not None
                }
            if record.get("type") == "turn_context" and not fallback:
                fallback = {
                    key: str(value)
                    for key, value in {
                        "model": payload.get("model"),
                        "effort": payload.get("effort") or payload.get("reasoning_effort"),
                        "service_tier": payload.get("service_tier"),
                    }.items()
                    if value is not None
                }
    except OSError:
        return {}
    return fallback


def _reverse_lines(path: Path, *, chunk_size: int = 65536) -> Iterator[bytes]:
    with path.open("rb") as stream:
        stream.seek(0, os.SEEK_END)
        position = stream.tell()
        remainder = b""
        while position:
            size = min(chunk_size, position)
            position -= size
            stream.seek(position)
            parts = (stream.read(size) + remainder).split(b"\n")
            remainder = parts[0]
            for line in reversed(parts[1:]):
                if line:
                    yield line.rstrip(b"\r")
        if remainder:
            yield remainder.rstrip(b"\r")


def codex_thread_error(thread: dict[str, Any]) -> str | None:
    for turn in reversed(thread.get("turns") or []):
        error = turn.get("error") if isinstance(turn, dict) else None
        if not isinstance(error, dict):
            continue
        message = str(error.get("message") or "").strip()
        details = str(error.get("additionalDetails") or "").strip()
        if message and details and details != message:
            return f"{message}: {details}"
        if message or details:
            return message or details
    return None


def codex_thread_is_permanently_unavailable(error: Exception) -> bool:
    message = str(error).lower()
    return any(fragment in message for fragment in (
        "no rollout found for thread id",
        "thread not found",
        "thread has been deleted",
        "thread is archived",
    ))


def run_codex_turn(
    thread_id: str,
    message: str,
    *,
    on_started: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> dict[str, Any]:
    thread = thread_id.strip()
    prompt = message.strip()
    if not thread:
        raise ValueError("thread_id is required")
    if not prompt:
        raise ValueError("message is required")

    try:
        return _run_codex_ipc_turn(
            thread,
            prompt,
            on_started=on_started,
            stop_event=stop_event,
        )
    except _CodexIpcUnavailable:
        return _run_codex_app_server_turn(
            thread,
            prompt,
            on_started=on_started,
            stop_event=stop_event,
        )


def _run_codex_app_server_turn(
    thread: str,
    prompt: str,
    *,
    on_started: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:

    process = _start_app_server()
    pending: list[dict[str, Any]] = []
    try:
        resumed = _call(process, 2, "thread/resume", {"threadId": thread}, pending=pending)
        runtime_status = ((resumed.get("thread") or {}).get("status") or {}).get("type")
        if runtime_status == "active":
            raise ValueError("Codex agent is busy")
        started = _call(
            process,
            3,
            "turn/start",
            {
                "threadId": thread,
                "clientUserMessageId": str(uuid.uuid4()),
                "input": [{"type": "text", "text": prompt, "text_elements": []}],
            },
            pending=pending,
        )
        turn = started.get("turn")
        if not isinstance(turn, dict) or not turn.get("id"):
            raise ValueError("Codex turn/start did not return a turn")
        turn_id = str(turn["id"])
        if on_started:
            on_started(turn_id)
        _notify_codex_clients_thread_changed(thread)
        final_message = None
        last_message = None
        declined_requests: list[str] = []

        while True:
            payload = pending.pop(0) if pending else _read_turn_payload(process, stop_event)
            method = payload.get("method")
            if payload.get("id") is not None and method:
                declined_requests.append(str(method))
                if method in {"item/commandExecution/requestApproval", "item/fileChange/requestApproval"}:
                    _send(process, {"id": payload["id"], "result": {"decision": "decline"}})
                else:
                    _send(process, {
                        "id": payload["id"],
                        "error": {"code": -32601, "message": "TeamFlow cannot answer interactive requests"},
                    })
                continue

            params = payload.get("params") or {}
            if method == "item/completed":
                item = params.get("item") or {}
                if item.get("type") == "agentMessage" and item.get("text"):
                    last_message = str(item["text"])
                    if item.get("phase") == "final_answer":
                        final_message = last_message
                continue
            if method != "turn/completed":
                continue
            completed = params.get("turn") or {}
            if str(completed.get("id") or "") != turn_id:
                continue
            status = str(completed.get("status") or "failed")
            error = completed.get("error") or {}
            error_message = str(error.get("message") or error.get("additionalDetails") or "").strip() or None
            _notify_codex_clients_thread_changed(thread)
            return {
                "ok": status == "completed",
                "thread_id": thread,
                "turn_id": turn_id,
                "status": status,
                "response": final_message or last_message,
                "declined_requests": declined_requests,
                "error": error_message,
                "transport": "app-server",
            }
    finally:
        _stop_app_server(process)


def _run_codex_ipc_turn(
    thread: str,
    prompt: str,
    *,
    on_started: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    connection = _CodexIpcConnection.connect()
    try:
        connection.follow(thread)
        turn_id = connection.start_turn(thread, prompt, stop_event=stop_event)
        if on_started:
            on_started(turn_id)
        completed = connection.wait_for_turn(thread, turn_id, stop_event=stop_event)
        return {
            "ok": completed["status"] == "completed",
            "thread_id": thread,
            "turn_id": turn_id,
            "status": completed["status"],
            "response": completed.get("response"),
            "declined_requests": [],
            "error": completed.get("error"),
            "transport": "codex-ipc",
        }
    finally:
        connection.unfollow(thread)
        connection.close()


class _CodexIpcUnavailable(ValueError):
    pass


class _CodexThreadStream:
    def __init__(self) -> None:
        self.entries: dict[str, dict[str, Any]] = {}

    def apply(self, change: dict[str, Any]) -> None:
        if change.get("type") == "snapshot":
            entries = (
                change.get("conversationState", {})
                .get("turnHistory", {})
                .get("history", {})
                .get("entitiesByKey", {})
            )
            self.entries = {
                str(key): self._entry(value)
                for key, value in entries.items()
                if isinstance(value, dict)
            }
            return
        if change.get("type") != "patches":
            return
        for patch in change.get("patches") or []:
            if not isinstance(patch, dict):
                continue
            path = patch.get("path")
            if not isinstance(path, list) or path[:3] != ["turnHistory", "history", "entitiesByKey"]:
                continue
            if len(path) < 4:
                continue
            key = str(path[3])
            operation = patch.get("op")
            if len(path) == 4:
                if operation == "remove":
                    self.entries.pop(key, None)
                elif isinstance(patch.get("value"), dict):
                    self.entries[key] = self._entry(patch["value"])
                continue
            entry = self.entries.setdefault(key, {"items": {}})
            field = path[4]
            if operation == "remove":
                if field == "items" and len(path) >= 6:
                    entry.setdefault("items", {}).pop(int(path[5]), None)
                else:
                    entry.pop(str(field), None)
                continue
            value = patch.get("value")
            if field != "items":
                entry[str(field)] = value
                continue
            if len(path) < 6:
                if isinstance(value, list):
                    entry["items"] = {
                        index: item for index, item in enumerate(value) if isinstance(item, dict)
                    }
                continue
            try:
                index = int(path[5])
            except (TypeError, ValueError):
                continue
            items = entry.setdefault("items", {})
            if len(path) == 6:
                if isinstance(value, dict):
                    items[index] = dict(value)
                continue
            item = items.setdefault(index, {})
            item[str(path[6])] = value

    def result(self, turn_id: str) -> dict[str, Any] | None:
        entry = next(
            (item for item in self.entries.values() if str(item.get("turnId") or "") == turn_id),
            None,
        )
        if entry is None:
            return None
        status = str(entry.get("status") or "")
        if status not in _TERMINAL_TURN_STATUSES:
            return None
        messages = [
            item.get("text")
            for _, item in sorted(entry.get("items", {}).items())
            if item.get("type") == "agentMessage" and item.get("text")
        ]
        error = entry.get("error")
        if isinstance(error, dict):
            error = str(error.get("message") or error.get("additionalDetails") or "").strip() or None
        elif error is not None:
            error = str(error)
        return {
            "status": status,
            "response": str(messages[-1]) if messages else None,
            "error": error,
        }

    @staticmethod
    def _entry(value: dict[str, Any]) -> dict[str, Any]:
        entry = {key: item for key, item in value.items() if key != "items"}
        entry["items"] = {
            index: item
            for index, item in enumerate(value.get("items") or [])
            if isinstance(item, dict)
        }
        return entry


class _CodexIpcConnection:
    def __init__(self, connection: socket.socket, client_id: str) -> None:
        self.connection = connection
        self.client_id = client_id
        self.responses: dict[str, dict[str, Any]] = {}
        self.followers: dict[str, set[str]] = {}
        self.disconnected_clients: set[str] = set()
        self.streams: dict[str, _CodexThreadStream] = {}
        self.owner_client_id: str | None = None

    @classmethod
    def connect(cls) -> _CodexIpcConnection:
        path = _codex_ipc_path()
        try:
            metadata = os.stat(path)
        except OSError as error:
            raise _CodexIpcUnavailable("Codex client IPC is unavailable") from error
        if not stat.S_ISSOCK(metadata.st_mode):
            raise _CodexIpcUnavailable("Codex client IPC path is not a socket")
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise _CodexIpcUnavailable("Codex client IPC socket belongs to another user")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(path)
        except OSError as error:
            connection.close()
            raise _CodexIpcUnavailable("Codex client IPC is unavailable") from error
        try:
            client = cls(connection, "initializing-client")
            request_id = str(uuid.uuid4())
            client._send({
                "type": "request",
                "requestId": request_id,
                "sourceClientId": client.client_id,
                "version": 0,
                "method": "initialize",
                "params": {"clientType": "teamflow"},
                "timeoutMs": 5000,
            })
            response = client._wait_for_response(request_id, timeout=5, stop_event=None)
            if response.get("resultType") != "success":
                raise _CodexIpcUnavailable(str(response.get("error") or "Codex IPC initialization failed"))
            client.client_id = str(response.get("result", {}).get("clientId") or "")
            if not client.client_id:
                raise _CodexIpcUnavailable("Codex IPC did not return a client ID")
            return client
        except Exception:
            connection.close()
            raise

    def follow(self, thread_id: str) -> None:
        self.streams.setdefault(thread_id, _CodexThreadStream())
        self._send({
            "type": "broadcast",
            "method": "thread-stream-following-changed",
            "sourceClientId": self.client_id,
            "params": {"conversationId": thread_id, "hostId": "local", "following": True},
            "version": _CODEX_IPC_FOLLOWING_VERSION,
        })

    def unfollow(self, thread_id: str) -> None:
        try:
            self._send({
                "type": "broadcast",
                "method": "thread-stream-following-changed",
                "sourceClientId": self.client_id,
                "params": {"conversationId": thread_id, "hostId": "local", "following": False},
                "version": _CODEX_IPC_FOLLOWING_VERSION,
            })
        except (OSError, ValueError):
            pass

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        stop_event: threading.Event | None,
    ) -> str:
        self.request_following_status(thread_id)
        self._collect_followers(thread_id, stop_event=stop_event)
        if not self.followers.get(thread_id):
            raise _CodexIpcUnavailable("No Codex client is currently viewing this session")
        request_id = str(uuid.uuid4())
        self._send({
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "version": _CODEX_IPC_START_TURN_VERSION,
            "method": "thread-follower-start-turn",
            "params": {
                "conversationId": thread_id,
                "turnStartParams": {
                    "input": [{"type": "text", "text": prompt, "text_elements": []}],
                    "clientUserMessageId": str(uuid.uuid4()),
                },
            },
            "timeoutMs": 10000,
        })
        response = self._wait_for_response(request_id, timeout=11, stop_event=stop_event)
        if response.get("resultType") != "success":
            message = str(response.get("error") or "Codex owner client rejected the turn")
            if message == "no-client-found":
                raise _CodexIpcUnavailable("No Codex client currently owns this session")
            raise ValueError(message)
        self.owner_client_id = str(response.get("handledByClientId") or "") or None
        turn = response.get("result", {}).get("result", {}).get("turn")
        if not isinstance(turn, dict) or not turn.get("id"):
            raise ValueError("Codex owner client did not return a turn")
        return str(turn["id"])

    def request_following_status(self, thread_id: str) -> None:
        self._send({
            "type": "broadcast",
            "method": "thread-stream-following-status-requested",
            "sourceClientId": self.client_id,
            "params": {"conversationId": thread_id, "hostId": "local"},
            "version": _CODEX_IPC_FOLLOWING_STATUS_VERSION,
        })

    def _collect_followers(
        self,
        thread_id: str,
        *,
        stop_event: threading.Event | None,
    ) -> None:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if self.followers.get(thread_id):
                return
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("TeamFlow daemon stopped before Codex accepted the turn")
            self._receive_once(min(0.05, deadline - time.monotonic()))

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        stop_event: threading.Event | None,
    ) -> dict[str, Any]:
        stream = self.streams.setdefault(thread_id, _CodexThreadStream())
        while True:
            result = stream.result(turn_id)
            if result is not None:
                return result
            if self.owner_client_id in self.disconnected_clients:
                raise ValueError("Codex session owner disconnected while the turn was running")
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("TeamFlow daemon stopped while the Codex turn was running")
            self._receive_once(0.5)

    def close(self) -> None:
        self.connection.close()

    def _wait_for_response(
        self,
        request_id: str,
        *,
        timeout: float,
        stop_event: threading.Event | None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            response = self.responses.pop(request_id, None)
            if response is not None:
                return response
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError("TeamFlow daemon stopped before Codex accepted the turn")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _CodexIpcUnavailable("Codex client IPC request timed out")
            self._receive_once(min(0.5, remaining))

    def _receive_once(self, timeout: float) -> None:
        ready, _, _ = select.select([self.connection], [], [], timeout)
        if not ready:
            return
        message = self._receive()
        message_type = message.get("type")
        if message_type == "response":
            self.responses[str(message.get("requestId") or "")] = message
            return
        if message_type == "client-discovery-request":
            self._send({
                "type": "client-discovery-response",
                "requestId": message.get("requestId"),
                "response": {"canHandle": False},
            })
            return
        if message_type != "broadcast":
            return
        method = message.get("method")
        params = message.get("params") or {}
        source_client_id = str(message.get("sourceClientId") or "")
        if method == "thread-stream-following-changed":
            thread_id = str(params.get("conversationId") or "")
            if source_client_id == self.client_id:
                return
            if params.get("following"):
                self.followers.setdefault(thread_id, set()).add(source_client_id)
            else:
                self.followers.setdefault(thread_id, set()).discard(source_client_id)
            return
        if method == "client-status-changed" and params.get("status") == "disconnected":
            disconnected = str(params.get("clientId") or source_client_id)
            self.disconnected_clients.add(disconnected)
            for followers in self.followers.values():
                followers.discard(disconnected)
            return
        if method != "thread-stream-state-changed":
            return
        if message.get("version") != _CODEX_IPC_STREAM_VERSION:
            raise ValueError(
                f"unsupported Codex IPC stream version: {message.get('version')}"
            )
        thread_id = str(params.get("conversationId") or "")
        change = params.get("change")
        if isinstance(change, dict):
            self.streams.setdefault(thread_id, _CodexThreadStream()).apply(change)

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > _CODEX_IPC_FRAME_LIMIT:
            raise ValueError("Codex IPC message is too large")
        try:
            self.connection.sendall(struct.pack("<I", len(body)) + body)
        except OSError as error:
            raise _CodexIpcUnavailable("Codex client IPC connection failed") from error

    def _receive(self) -> dict[str, Any]:
        header = self._read_exact(4)
        size = struct.unpack("<I", header)[0]
        if size <= 0 or size > _CODEX_IPC_FRAME_LIMIT:
            raise ValueError(f"invalid Codex IPC frame size: {size}")
        try:
            payload = json.loads(self._read_exact(size))
        except json.JSONDecodeError as error:
            raise ValueError("Codex IPC returned invalid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("Codex IPC returned an invalid message")
        return payload

    def _read_exact(self, size: int) -> bytes:
        result = bytearray()
        while len(result) < size:
            try:
                chunk = self.connection.recv(size - len(result))
            except OSError as error:
                raise _CodexIpcUnavailable("Codex client IPC connection failed") from error
            if not chunk:
                raise _CodexIpcUnavailable("Codex client IPC connection closed")
            result.extend(chunk)
        return bytes(result)


def _codex_ipc_path() -> str:
    if os.name == "nt":
        raise _CodexIpcUnavailable("Codex client IPC is not supported on Windows yet")
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "ipc", "ipc.sock")


def _notify_codex_clients_thread_changed(thread_id: str) -> None:
    try:
        connection = _CodexIpcConnection.connect()
    except (OSError, ValueError):
        return
    try:
        connection._send({
            "type": "broadcast",
            "method": "thread-read-state-changed",
            "sourceClientId": connection.client_id,
            "params": {
                "conversationId": thread_id,
                "hostId": "local",
                "hasUnreadTurn": True,
            },
            "version": _CODEX_IPC_READ_STATE_VERSION,
        })
        connection._send({
            "type": "broadcast",
            "method": "query-cache-invalidate",
            "sourceClientId": connection.client_id,
            "params": {"queryKey": ["tasks"]},
            "version": 0,
        })
    except (OSError, ValueError):
        pass
    finally:
        connection.close()


def _request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    process = _start_app_server()
    try:
        return _call(process, 2, method, params)
    finally:
        _stop_app_server(process)


def _start_app_server() -> subprocess.Popen[bytes]:
    try:
        process = subprocess.Popen(
            [
                _codex_executable(),
                "-c",
                "features.code_mode_host=true",
                "app-server",
                "--analytics-default-enabled",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as error:
        raise ValueError(f"failed to start Codex app-server: {error}") from error
    try:
        _send(process, {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {"name": "teamflow", "title": "TeamFlow", "version": "0.1"},
                "capabilities": {
                    "experimentalApi": True,
                    "mcpServerOpenaiFormElicitation": True,
                    "requestAttestation": False,
                },
            },
        })
        initialized = _response_for(process, 1)
        if initialized.get("error"):
            raise ValueError(initialized["error"].get("message") or "Codex app-server initialization failed")
        _send(process, {"method": "initialized", "params": {}})
        return process
    except Exception:
        _stop_app_server(process)
        raise


def _codex_executable() -> str:
    override = os.environ.get("CODEX_CLI")
    if override:
        return override
    for app in ("ChatGPT.app", "Codex.app"):
        for applications in ("/Applications", os.path.expanduser("~/Applications")):
            candidate = os.path.join(applications, app, "Contents", "Resources", "codex")
            if os.access(candidate, os.X_OK):
                return candidate
    return "codex"


def _stop_app_server(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()


def _call(
    process: subprocess.Popen[bytes],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    _send(process, {"id": request_id, "method": method, "params": params})
    response = _response_for(process, request_id, pending=pending)
    if response.get("error"):
        raise ValueError(response["error"].get("message") or f"Codex {method} failed")
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Codex {method} returned an invalid result")
    return result


def _send(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise ValueError("Codex app-server stdin is unavailable")
    try:
        process.stdin.write((json.dumps(payload) + "\n").encode())
        process.stdin.flush()
    except OSError as error:
        raise ValueError(f"Codex app-server connection failed: {error}") from error


def _response_for(
    process: subprocess.Popen[bytes],
    request_id: int,
    *,
    pending: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    while True:
        try:
            payload = _read_payload(process, deadline, "Codex app-server timed out")
        except TimeoutError as error:
            raise ValueError(str(error)) from error
        if payload.get("id") == request_id and not payload.get("method"):
            return payload
        if pending is not None:
            pending.append(payload)


def _read_payload(process: subprocess.Popen[bytes], deadline: float | None, timeout_message: str) -> dict[str, Any]:
    if process.stdout is None:
        raise ValueError("Codex app-server stdout is unavailable")
    wait = None if deadline is None else max(0, deadline - time.monotonic())
    ready, _, _ = select.select([process.stdout], [], [], wait)
    if ready:
        line = process.stdout.readline()
        if line:
            try:
                payload = json.loads(line.decode())
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                raise ValueError("Codex app-server returned invalid JSON") from error
            if isinstance(payload, dict):
                return payload
            raise ValueError("Codex app-server returned invalid JSON")
    if process.poll() is not None:
        stderr = process.stderr.read().decode(errors="replace").strip() if process.stderr else ""
        raise ValueError(stderr or "Codex app-server exited unexpectedly")
    raise TimeoutError(timeout_message)


def _read_turn_payload(
    process: subprocess.Popen[bytes],
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    if stop_event is None:
        return _read_payload(process, None, "Codex app-server connection ended unexpectedly")
    while not stop_event.is_set():
        try:
            return _read_payload(process, time.monotonic() + 0.5, "Codex turn is still running")
        except TimeoutError:
            continue
    raise InterruptedError("TeamFlow daemon stopped while the Codex turn was running")


def codex_turn(thread: dict[str, Any], turn_id: str) -> dict[str, Any] | None:
    for item in thread.get("turns") or []:
        if isinstance(item, dict) and str(item.get("id") or "") == turn_id:
            return item
    return None
