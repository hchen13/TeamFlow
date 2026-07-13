from __future__ import annotations

import json
import os
import select
import subprocess
import time
from typing import Any


def read_codex_thread(thread_id: str, *, include_turns: bool = False) -> dict[str, Any]:
    result = _request("thread/read", {"threadId": thread_id, "includeTurns": include_turns})
    thread = result.get("thread")
    if not isinstance(thread, dict):
        raise ValueError("Codex app-server did not return a thread")
    return thread


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


def run_codex_turn(thread_id: str, message: str) -> dict[str, Any]:
    thread = thread_id.strip()
    prompt = message.strip()
    if not thread:
        raise ValueError("thread_id is required")
    if not prompt:
        raise ValueError("message is required")

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
            {"threadId": thread, "input": [{"type": "text", "text": prompt}]},
            pending=pending,
        )
        turn = started.get("turn")
        if not isinstance(turn, dict) or not turn.get("id"):
            raise ValueError("Codex turn/start did not return a turn")
        turn_id = str(turn["id"])
        final_message = None
        last_message = None
        declined_requests: list[str] = []

        while True:
            payload = pending.pop(0) if pending else _read_payload(
                process,
                None,
                "Codex app-server connection ended unexpectedly",
            )
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
            return {
                "ok": status == "completed",
                "thread_id": thread,
                "turn_id": turn_id,
                "status": status,
                "response": final_message or last_message,
                "declined_requests": declined_requests,
                "error": error_message,
            }
    finally:
        _stop_app_server(process)


def _request(method: str, params: dict[str, Any]) -> dict[str, Any]:
    process = _start_app_server()
    try:
        return _call(process, 2, method, params)
    finally:
        _stop_app_server(process)


def _start_app_server() -> subprocess.Popen[bytes]:
    try:
        process = subprocess.Popen(
            [_codex_executable(), "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
    except OSError as error:
        raise ValueError(f"failed to start Codex app-server: {error}") from error
    try:
        _send(process, {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "teamflow", "version": "0.1"}}})
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
        payload = _read_payload(process, deadline, "Codex app-server timed out")
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
    raise ValueError(timeout_message)
