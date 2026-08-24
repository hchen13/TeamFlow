from __future__ import annotations

import json
import os
import select
import socket
import stat
import struct
import threading
import time
import uuid
from typing import Any, Callable

from .codex_ipc_stream import CodexThreadStream


_CODEX_IPC_FRAME_LIMIT = 256 * 1024 * 1024
_CODEX_IPC_STREAM_VERSION = 11
_CODEX_IPC_FOLLOWING_VERSION = 1
_CODEX_IPC_START_TURN_VERSION = 2
_CODEX_IPC_INTERRUPT_TURN_VERSION = 4
_CODEX_IPC_READ_STATE_VERSION = 2
_CODEX_IPC_OWNER_DISCOVERY_VERSION = 1
# Match the Desktop client's own IPC request window. This is a response deadline,
# not a cached Session-state timeout; callers run this probe off the daemon loop.
_CODEX_IPC_OWNER_DISCOVERY_TIMEOUT = 5.0


class CodexIpcUnavailable(ValueError):
    pass


class CodexIpcNoOwner(CodexIpcUnavailable):
    pass


class CodexIpcClientUnavailable(CodexIpcNoOwner):
    pass


class CodexIpcSessionNotLoaded(CodexIpcNoOwner):
    pass


class CodexIpcOwnerUnconfirmed(CodexIpcNoOwner):
    pass


class CodexIpcEmptyTurn(CodexIpcUnavailable):
    pass


class CodexTurnAcceptanceUnknown(ValueError):
    pass


class CodexIpcConnection:
    def __init__(
        self,
        connection: socket.socket,
        client_id: str,
        *,
        lightweight: bool = False,
    ) -> None:
        self.connection = connection
        self.client_id = client_id
        # A lightweight connection only holds follows; it never reads stream content, so it
        # keeps no per-thread history and cannot grow with the number of turns.
        self.lightweight = lightweight
        self.responses: dict[str, dict[str, Any]] = {}
        self.followers: dict[str, set[str]] = {}
        self.disconnected_clients: set[str] = set()
        self.streams: dict[str, CodexThreadStream] = {}
        self.following_threads: set[str] = set()
        self.owner_client_id: str | None = None

    @classmethod
    def connect(cls, *, lightweight: bool = False) -> CodexIpcConnection:
        path = codex_ipc_path()
        try:
            metadata = os.stat(path)
        except OSError as error:
            raise CodexIpcClientUnavailable(
                "No Codex client IPC is available; start Codex Desktop or another "
                "Codex client and TeamFlow will retry automatically"
            ) from error
        if not stat.S_ISSOCK(metadata.st_mode):
            raise CodexIpcClientUnavailable(
                "Codex client IPC path is not a socket"
            )
        if hasattr(os, "getuid") and metadata.st_uid != os.getuid():
            raise CodexIpcUnavailable("Codex client IPC socket belongs to another user")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5)
        try:
            connection.connect(path)
        except OSError as error:
            connection.close()
            raise CodexIpcClientUnavailable(
                "No Codex client IPC is available; start Codex Desktop or another "
                "Codex client and TeamFlow will retry automatically"
            ) from error
        try:
            client = cls(connection, "initializing-client", lightweight=lightweight)
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
            response = client._wait_for_response(
                request_id,
                timeout=5,
                stop_event=None,
            )
            if response.get("resultType") != "success":
                raise CodexIpcUnavailable(
                    str(response.get("error") or "Codex IPC initialization failed")
                )
            client.client_id = str(response.get("result", {}).get("clientId") or "")
            if not client.client_id:
                raise CodexIpcUnavailable("Codex IPC did not return a client ID")
            return client
        except Exception:
            connection.close()
            raise

    def follow(
        self,
        thread_id: str,
        *,
        force: bool = False,
        target_client_ids: list[str] | None = None,
    ) -> None:
        if thread_id in self.following_threads and not force:
            return
        if not self.lightweight:
            self.streams.setdefault(thread_id, CodexThreadStream())
        payload: dict[str, Any] = {
            "type": "broadcast",
            "method": "thread-stream-following-changed",
            "sourceClientId": self.client_id,
            "params": {
                "conversationId": thread_id,
                "hostId": "local",
                "following": True,
            },
            "version": _CODEX_IPC_FOLLOWING_VERSION,
        }
        if target_client_ids:
            payload["targetClientIds"] = list(target_client_ids)
        self._send(payload)
        self.following_threads.add(thread_id)

    def unfollow(self, thread_id: str) -> None:
        if thread_id not in self.following_threads:
            return
        try:
            self._send({
                "type": "broadcast",
                "method": "thread-stream-following-changed",
                "sourceClientId": self.client_id,
                "params": {
                    "conversationId": thread_id,
                    "hostId": "local",
                    "following": False,
                },
                "version": _CODEX_IPC_FOLLOWING_VERSION,
            })
        except (OSError, ValueError):
            pass
        finally:
            self.following_threads.discard(thread_id)

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        *,
        client_message_id: str,
        stop_event: threading.Event | None,
    ) -> str:
        owner_client_id = self.discover_owner(
            thread_id,
            stop_event=stop_event,
        )
        # The owner is authoritative for turn admission. Preloading its complete
        # history makes dispatch latency grow with the thread transcript.
        request_id = str(uuid.uuid4())
        try:
            self._send({
                "type": "request",
                "requestId": request_id,
                "sourceClientId": self.client_id,
                "targetClientId": owner_client_id,
                "version": _CODEX_IPC_START_TURN_VERSION,
                "method": "thread-follower-start-turn",
                "params": {
                    "conversationId": thread_id,
                    "turnStart": {
                        "request": {
                            "threadId": thread_id,
                            "input": [
                                {
                                    "type": "text",
                                    "text": prompt,
                                    "text_elements": [],
                                }
                            ],
                            "clientUserMessageId": client_message_id,
                        },
                        "context": {
                            "inheritThreadSettings": True,
                        },
                    },
                },
                "timeoutMs": 10000,
            })
            response = self._wait_for_response(
                request_id,
                timeout=11,
                stop_event=stop_event,
            )
        except (CodexIpcUnavailable, InterruptedError) as error:
            raise CodexTurnAcceptanceUnknown(
                "Codex turn acceptance could not be confirmed"
            ) from error
        if response.get("resultType") != "success":
            message = str(response.get("error") or "Codex owner client rejected the turn")
            if message == "no-client-found":
                raise ValueError("Codex session owner disconnected before accepting the turn")
            raise ValueError(message)
        turn = _nested_result(response).get("turn")
        if not isinstance(turn, dict) or not turn.get("id"):
            raise CodexTurnAcceptanceUnknown(
                "Codex owner accepted the request without returning a turn ID"
            )
        return str(turn["id"])

    def discover_owner(
        self,
        thread_id: str,
        *,
        stop_event: threading.Event | None,
        timeout: float | None = None,
    ) -> str:
        if timeout is None:
            timeout = _CODEX_IPC_OWNER_DISCOVERY_TIMEOUT
        self.follow(thread_id)
        stream = self.streams.setdefault(thread_id, CodexThreadStream())
        if (
            stream.initialized
            and self.owner_client_id
            and self.owner_client_id not in self.disconnected_clients
        ):
            return self.owner_client_id
        request_id = str(uuid.uuid4())
        try:
            self._send({
                "type": "request",
                "requestId": request_id,
                "sourceClientId": self.client_id,
                "version": _CODEX_IPC_OWNER_DISCOVERY_VERSION,
                "method": "thread-owner-discovery",
                "params": {
                    "conversationId": thread_id,
                    "hostId": "local",
                },
                "timeoutMs": max(1, int(timeout * 1000)),
            })
            response = self._wait_for_response(
                request_id,
                timeout=timeout + 1,
                stop_event=stop_event,
            )
        except CodexIpcUnavailable as error:
            raise CodexIpcOwnerUnconfirmed(
                "Codex IPC is running, but the Agent Session owner could not be "
                "confirmed; TeamFlow will retry automatically"
            ) from error
        if response.get("resultType") != "success":
            message = str(response.get("error") or "Codex session has no owner")
            if message == "no-client-found":
                raise CodexIpcSessionNotLoaded(
                    "Codex is running, but no active client has loaded this Agent "
                    "Session; open the Session and TeamFlow will retry automatically"
                )
            raise CodexIpcUnavailable(message)
        owner_client_id = str(response.get("handledByClientId") or "")
        if not owner_client_id:
            raise CodexIpcSessionNotLoaded(
                "Codex is running, but no active client has loaded this Agent "
                "Session; open the Session and TeamFlow will retry automatically"
            )
        self.owner_client_id = owner_client_id
        return owner_client_id

    def wait_for_turn_started(
        self,
        thread_id: str,
        turn_id: str,
        *,
        client_message_id: str,
        stop_event: threading.Event | None,
    ) -> None:
        stream = self.streams.setdefault(thread_id, CodexThreadStream())
        deadline = time.monotonic() + 5
        while not stream.contains_client_message(turn_id, client_message_id):
            if stream.is_terminal(turn_id):
                raise CodexIpcEmptyTurn(
                    "Codex owner completed a turn without the requested message"
                )
            if self.owner_client_id in self.disconnected_clients:
                raise ValueError(
                    "Codex session owner disconnected before the turn materialized"
                )
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError(
                    "TeamFlow daemon stopped before the Codex turn materialized"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError(
                    f"Codex turn {turn_id} did not materialize in session {thread_id}"
                )
            self._receive_once(min(0.5, remaining))

    def interrupt_turn(
        self,
        thread_id: str,
        *,
        expected_turn_id: str,
    ) -> dict[str, Any]:
        owner_client_id = self.discover_owner(thread_id, stop_event=None)
        request_id = str(uuid.uuid4())
        self._send({
            "type": "request",
            "requestId": request_id,
            "sourceClientId": self.client_id,
            "targetClientId": owner_client_id,
            "version": _CODEX_IPC_INTERRUPT_TURN_VERSION,
            "method": "thread-follower-interrupt-turn",
            "params": {
                "conversationId": thread_id,
                "mode": "user",
                "expectedTurnId": expected_turn_id,
            },
            "timeoutMs": 15000,
        })
        response = self._wait_for_response(
            request_id,
            timeout=16,
            stop_event=None,
        )
        if response.get("resultType") != "success":
            message = str(
                response.get("error") or "Codex owner client rejected the interrupt"
            )
            if message == "no-client-found":
                raise ValueError("Codex session owner disconnected before the interrupt")
            raise ValueError(message)
        payload = _nested_result(response)
        if not isinstance(payload, dict) or payload.get("ok") is False:
            raise ValueError("Codex owner client did not confirm the interrupt")
        return {
            "ok": True,
            "thread_id": thread_id,
            "turn_id": payload.get("interruptedTurnId"),
            "status": "interrupted",
            "already_stopped": not bool(payload.get("interruptedTurnId")),
            "transport": "codex-ipc",
        }

    def wait_for_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        interrupt_competing_turn: Callable[[str, str], Any] | None,
        stop_event: threading.Event | None,
    ) -> dict[str, Any]:
        stream = self.streams.setdefault(thread_id, CodexThreadStream())
        while True:
            self.ensure_exclusive_turn(
                thread_id,
                turn_id,
                interrupt_competing_turn=interrupt_competing_turn,
            )
            result = stream.result(turn_id)
            if result is not None:
                return result
            if self.owner_client_id in self.disconnected_clients:
                raise ValueError(
                    "Codex session owner disconnected while the turn was running"
                )
            if stop_event is not None and stop_event.is_set():
                raise InterruptedError(
                    "TeamFlow daemon stopped while the Codex turn was running"
                )
            self._receive_once(0.5)

    def ensure_exclusive_turn(
        self,
        thread_id: str,
        turn_id: str,
        *,
        interrupt_competing_turn: Callable[[str, str], Any] | None,
    ) -> None:
        stream = self.streams.setdefault(thread_id, CodexThreadStream())
        if not stream.has_competing_active_turn(turn_id):
            return
        if interrupt_competing_turn is not None:
            interrupt_competing_turn(thread_id, turn_id)
        raise ValueError(
            "Codex session started another turn concurrently; "
            "the TeamFlow turn was interrupted"
        )

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
                raise InterruptedError(
                    "TeamFlow daemon stopped before Codex accepted the turn"
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CodexIpcUnavailable("Codex client IPC request timed out")
            self._receive_once(min(0.5, remaining))

    def _receive_once(self, timeout: float) -> dict[str, Any] | None:
        ready, _, _ = select.select([self.connection], [], [], timeout)
        if not ready:
            return
        message = self._receive()
        message_type = message.get("type")
        if message_type == "response":
            self.responses[str(message.get("requestId") or "")] = message
            return message
        if message_type == "client-discovery-request":
            self._send({
                "type": "client-discovery-response",
                "requestId": message.get("requestId"),
                "response": {"canHandle": False},
            })
            return message
        if message_type != "broadcast":
            return message
        if self.lightweight:
            # The keeper reads nothing but the envelope, so no follower, disconnect or stream
            # bookkeeping happens here and the connection cannot grow with what it observes.
            return message
        method = message.get("method")
        params = message.get("params") or {}
        source_client_id = str(message.get("sourceClientId") or "")
        if method == "thread-stream-following-changed":
            thread_id = str(params.get("conversationId") or "")
            if source_client_id == self.client_id:
                return message
            if params.get("following"):
                self.followers.setdefault(thread_id, set()).add(source_client_id)
            else:
                self.followers.setdefault(thread_id, set()).discard(source_client_id)
            return message
        if method == "client-status-changed" and params.get("status") == "disconnected":
            disconnected = str(params.get("clientId") or source_client_id)
            self.disconnected_clients.add(disconnected)
            for followers in self.followers.values():
                followers.discard(disconnected)
            return message
        if method != "thread-stream-state-changed":
            return message
        if message.get("version") != _CODEX_IPC_STREAM_VERSION:
            raise ValueError(
                f"unsupported Codex IPC stream version: {message.get('version')}"
            )
        thread_id = str(params.get("conversationId") or "")
        change = params.get("change")
        if isinstance(change, dict):
            if change.get("type") == "snapshot" and source_client_id:
                self.owner_client_id = source_client_id
            self.streams.setdefault(thread_id, CodexThreadStream()).apply(change)
        return message

    def _send(self, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, separators=(",", ":")).encode()
        if len(body) > _CODEX_IPC_FRAME_LIMIT:
            raise ValueError("Codex IPC message is too large")
        try:
            self.connection.sendall(struct.pack("<I", len(body)) + body)
        except OSError as error:
            raise CodexIpcUnavailable("Codex client IPC connection failed") from error

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
                raise CodexIpcUnavailable("Codex client IPC connection failed") from error
            if not chunk:
                raise CodexIpcUnavailable("Codex client IPC connection closed")
            result.extend(chunk)
        return bytes(result)


def run_codex_ipc_turn(
    thread: str,
    prompt: str,
    *,
    connection_type: type[CodexIpcConnection] = CodexIpcConnection,
    client_message_id: str,
    interrupt_competing_turn: Callable[[str, str], Any] | None,
    on_started: Callable[[str], None] | None,
    stop_event: threading.Event | None,
) -> dict[str, Any]:
    connection = connection_type.connect()
    try:
        connection.follow(thread)
        turn_id = connection.start_turn(
            thread,
            prompt,
            client_message_id=client_message_id,
            stop_event=stop_event,
        )
        turn_recorded = False

        def record_started() -> None:
            nonlocal turn_recorded
            turn_recorded = True
            if on_started:
                on_started(turn_id)

        try:
            connection.wait_for_turn_started(
                thread,
                turn_id,
                client_message_id=client_message_id,
                stop_event=stop_event,
            )
            record_started()
            connection.ensure_exclusive_turn(
                thread,
                turn_id,
                interrupt_competing_turn=interrupt_competing_turn,
            )
            completed = connection.wait_for_turn(
                thread,
                turn_id,
                interrupt_competing_turn=interrupt_competing_turn,
                stop_event=stop_event,
            )
        except Exception as error:
            if not turn_recorded:
                record_started()
            if isinstance(error, CodexIpcUnavailable):
                raise CodexTurnAcceptanceUnknown(
                    "Codex turn acceptance could not be confirmed"
                ) from error
            raise
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


def stop_codex_ipc_turn(
    thread: str,
    *,
    expected_turn_id: str,
    connection_type: type[CodexIpcConnection] = CodexIpcConnection,
) -> dict[str, Any]:
    connection = connection_type.connect()
    try:
        return connection.interrupt_turn(
            thread,
            expected_turn_id=expected_turn_id,
        )
    finally:
        connection.unfollow(thread)
        connection.close()


def codex_ipc_session_has_owner(
    thread: str,
    *,
    timeout: float | None = None,
    stop_event: threading.Event | None = None,
    connection_type: type[CodexIpcConnection] = CodexIpcConnection,
) -> bool:
    try:
        connection = connection_type.connect()
    except CodexIpcNoOwner:
        return False
    try:
        connection.follow(thread)
        try:
            connection.discover_owner(
                thread,
                stop_event=stop_event,
                timeout=timeout,
            )
        except (CodexIpcUnavailable, InterruptedError):
            return False
        return True
    finally:
        connection.unfollow(thread)
        connection.close()


def notify_codex_clients_thread_changed(
    thread_id: str,
    *,
    connection_type: type[CodexIpcConnection] = CodexIpcConnection,
) -> None:
    try:
        connection = connection_type.connect()
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


def codex_ipc_path() -> str:
    if os.name == "nt":
        raise CodexIpcClientUnavailable(
            "Codex client IPC is not supported on Windows yet"
        )
    codex_home = os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")
    return os.path.join(codex_home, "ipc", "ipc.sock")


def _nested_result(response: dict[str, Any]) -> dict[str, Any]:
    payload = response.get("result")
    while isinstance(payload, dict) and isinstance(payload.get("result"), dict):
        payload = payload["result"]
    return payload if isinstance(payload, dict) else {}
