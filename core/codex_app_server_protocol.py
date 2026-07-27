from __future__ import annotations

import json
import os
import select
import subprocess
import threading
import time
from typing import Any, Callable


def request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    process = start_app_server()
    try:
        return call(process, 2, method, params)
    finally:
        stop_app_server(process)


def start_app_server(
    *,
    executable: Callable[[], str] | None = None,
    send_payload: Callable[[subprocess.Popen[bytes], dict[str, Any]], None] | None = None,
    response_reader: Callable[..., dict[str, Any]] | None = None,
    stop_process: Callable[[subprocess.Popen[bytes]], None] | None = None,
) -> subprocess.Popen[bytes]:
    executable = executable or codex_executable
    send_payload = send_payload or send
    response_reader = response_reader or response_for
    stop_process = stop_process or stop_app_server
    try:
        process = subprocess.Popen(
            [
                executable(),
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
        send_payload(process, {
            "id": 1,
            "method": "initialize",
            "params": {
                "clientInfo": {
                    "name": "teamflow",
                    "title": "TeamFlow",
                    "version": "0.1",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "mcpServerOpenaiFormElicitation": True,
                    "requestAttestation": False,
                },
            },
        })
        initialized = response_reader(process, 1)
        if initialized.get("error"):
            raise ValueError(
                initialized["error"].get("message")
                or "Codex app-server initialization failed"
            )
        send_payload(process, {"method": "initialized", "params": {}})
        return process
    except Exception:
        stop_process(process)
        raise


def codex_executable() -> str:
    override = os.environ.get("CODEX_CLI")
    if override:
        return override
    for app in ("ChatGPT.app", "Codex.app"):
        for applications in ("/Applications", os.path.expanduser("~/Applications")):
            candidate = os.path.join(
                applications,
                app,
                "Contents",
                "Resources",
                "codex",
            )
            if os.access(candidate, os.X_OK):
                return candidate
    return "codex"


def stop_app_server(process: subprocess.Popen[bytes]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
    finally:
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()


def call(
    process: subprocess.Popen[bytes],
    request_id: int,
    method: str,
    params: dict[str, Any],
    *,
    pending: list[dict[str, Any]] | None = None,
    send_payload: Callable[[subprocess.Popen[bytes], dict[str, Any]], None] | None = None,
    response_reader: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    send_payload = send_payload or send
    response_reader = response_reader or response_for
    send_payload(
        process,
        {"id": request_id, "method": method, "params": params},
    )
    response = response_reader(process, request_id, pending=pending)
    if response.get("error"):
        raise ValueError(
            response["error"].get("message") or f"Codex {method} failed"
        )
    result = response.get("result")
    if not isinstance(result, dict):
        raise ValueError(f"Codex {method} returned an invalid result")
    return result


def send(process: subprocess.Popen[bytes], payload: dict[str, Any]) -> None:
    if process.stdin is None:
        raise ValueError("Codex app-server stdin is unavailable")
    try:
        process.stdin.write((json.dumps(payload) + "\n").encode())
        process.stdin.flush()
    except OSError as error:
        raise ValueError(f"Codex app-server connection failed: {error}") from error


def response_for(
    process: subprocess.Popen[bytes],
    request_id: int,
    *,
    pending: list[dict[str, Any]] | None = None,
    read: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    read = read or read_payload
    deadline = time.monotonic() + 10
    while True:
        try:
            payload = read(
                process,
                deadline,
                "Codex app-server timed out",
            )
        except TimeoutError as error:
            raise ValueError(str(error)) from error
        if payload.get("id") == request_id and not payload.get("method"):
            return payload
        if pending is not None:
            pending.append(payload)


def read_payload(
    process: subprocess.Popen[bytes],
    deadline: float | None,
    timeout_message: str,
) -> dict[str, Any]:
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
                raise ValueError(
                    "Codex app-server returned invalid JSON"
                ) from error
            if isinstance(payload, dict):
                return payload
            raise ValueError("Codex app-server returned invalid JSON")
    if process.poll() is not None:
        stderr = (
            process.stderr.read().decode(errors="replace").strip()
            if process.stderr
            else ""
        )
        raise ValueError(stderr or "Codex app-server exited unexpectedly")
    raise TimeoutError(timeout_message)


def read_turn_payload(
    process: subprocess.Popen[bytes],
    stop_event: threading.Event | None,
    *,
    wake_event: threading.Event | None = None,
    read: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    read = read or read_payload
    if stop_event is None and wake_event is None:
        return read(
            process,
            None,
            "Codex app-server connection ended unexpectedly",
        )
    while stop_event is None or not stop_event.is_set():
        try:
            return read(
                process,
                time.monotonic() + 0.5,
                "Codex turn is still running",
            )
        except TimeoutError:
            if wake_event is not None and wake_event.is_set():
                return {}
            continue
    raise InterruptedError(
        "TeamFlow daemon stopped while the Codex turn was running"
    )
