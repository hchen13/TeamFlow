from __future__ import annotations

import json
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any

from .config import resolve_workspace_paths


MAX_IPC_MESSAGE_BYTES = 1024 * 1024


class DaemonServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True

    def __init__(self, path: str, runtime: Any):
        self.runtime = runtime
        super().__init__(path, DaemonRequestHandler)


class DaemonRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline(MAX_IPC_MESSAGE_BYTES + 1)
            if not line or len(line) > MAX_IPC_MESSAGE_BYTES:
                self._write({
                    "ok": False,
                    "error": "invalid TeamFlow daemon request",
                })
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
                    request.get("workspace"),
                    identity_id=request.get("identity_id"),
                )
            elif action == "disable_workspace":
                result = self.server.runtime.disable_workspace(
                    request.get("workspace")
                )
            elif action == "sync_workspace":
                result = self.server.runtime.sync_workspace(
                    request.get("workspace"),
                    identity_id=request.get("identity_id"),
                )
            elif action == "verify_listener":
                result = self.server.runtime.verify_workspace(
                    request.get("workspace"),
                    identity_id=request.get("identity_id"),
                )
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
                    assignment_revision=int(
                        request.get("assignment_revision") or 0
                    ),
                    context_fingerprint=str(
                        request.get("context_fingerprint") or ""
                    ),
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
                arguments = request.get("arguments")
                result = self.server.runtime.invoke_tool(
                    invocation_id=str(request.get("invocation_id") or ""),
                    grant=str(request.get("grant") or ""),
                    tool_name=str(request.get("tool_name") or ""),
                    arguments=arguments if isinstance(arguments, dict) else {},
                )
            elif action == "shutdown":
                # The status in this reply, and any status a concurrent caller reads next, has to
                # already show the daemon leaving. Otherwise it looks reusable right up to the
                # moment its socket closes.
                self.server.runtime.begin_shutdown()
                result = {
                    "stopping": True,
                    **self.server.runtime.status(),
                }
                self._write({"ok": True, "result": result})
                threading.Thread(
                    target=self.server.shutdown,
                    name="teamflow-daemon-stop",
                    daemon=True,
                ).start()
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
                item = self.server.runtime.wait_for_workspace_event(
                    workspace,
                    cursor,
                )
                if item is None:
                    continue
                cursor, payload = item
                self._write({"ok": True, "event": payload})
        except (BrokenPipeError, ConnectionResetError):
            return
        finally:
            self.server.runtime.release_ephemeral_workspace(workspace)

    def _write(self, payload: dict[str, Any]) -> None:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        self.wfile.write(encoded + b"\n")
        self.wfile.flush()


def request(
    socket_path: Path,
    payload: dict[str, Any],
    *,
    timeout: float,
) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(socket_path))
        client.sendall(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode()
            + b"\n"
        )
        return read_response(client.makefile("rb").readline())["result"]


def read_response(line: bytes) -> dict[str, Any]:
    if not line:
        raise ValueError("TeamFlow daemon closed the connection without a response")
    response = json.loads(line)
    if not response.get("ok"):
        raise ValueError(response.get("error") or "TeamFlow daemon request failed")
    return response
